#!/usr/bin/env python3
import os, sys, re, asyncio, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

# ----------------------------------------------------------------------------------------------------------------------
# Paths & settings
# ----------------------------------------------------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
OUT  = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))

def today_str_ddmmyyyy():
    return datetime.now(IST).strftime("%d/%m/%Y")

def today_str_filename():
    return datetime.now(IST).strftime("%d-%m-%Y")

def log(msg: str):
    print(msg, flush=True)

# ----------------------------------------------------------------------------------------------------------------------
# PDF helpers
# ----------------------------------------------------------------------------------------------------------------------
async def html_to_pdf(context, html: str, save_path: Path, title: str):
    """
    Render returned HTML (server POST response) to a clean PDF using a temporary page.
    We wrap the server HTML in a minimal container so fonts render and the table fits page width.
    """
    page = await context.new_page()
    wrapper = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <title>{title}</title>
        <style>
          @page {{ size: A4; margin: 16mm; }}
          body {{ font-family: Arial, Helvetica, sans-serif; font-size: 12px; color: #111; }}
          .container {{ max-width: 100%; }}
          table {{ border-collapse: collapse; width: 100%; }}
          th, td {{ border: 1px solid #999; padding: 6px 8px; vertical-align: top; }}
          th {{ background: #f2f2f2; }}
          .muted {{ color: #666; font-size: 11px; margin: 0 0 8px; }}
          .headline {{ font-size: 14px; font-weight: 700; margin-bottom: 8px; }}
        </style>
      </head>
      <body>
        <div class="container">
          {html}
        </div>
      </body>
    </html>
    """
    await page.set_content(wrapper, wait_until="domcontentloaded")
    # Let layout settle
    await page.wait_for_timeout(400)
    # Chromium supports PDF generation:
    try:
        await page.pdf(path=str(save_path), format="A4", margin={"top":"16mm","right":"16mm","bottom":"16mm","left":"16mm"})
    except Exception:
        # Fallback: print-to-pdf via Chromium emulate (same API)
        await page.pdf(path=str(save_path))
    await page.close()
    log(f"[pdf:dom] rendered → {save_path}")

# ----------------------------------------------------------------------------------------------------------------------
# Page navigation: ensure report form exists
# ----------------------------------------------------------------------------------------------------------------------
async def goto_report_page(page):
    """
    Ensure the 'Application Wise Report' page is actually open and has a <form>.
    Try direct navigation first; if no <form>, use the top menu clicks as fallback.
    """
    # Derive site root from current URL after login
    m = re.match(r"^(https?://[^/]+)", page.url)
    root = m.group(1) if m else "https://esinchai.punjab.gov.in"
    url = root + "/Authorities/applicationwisereport.jsp"

    # 1) Try direct navigation
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("form", timeout=7000)
        log("[nav] Application Wise Report form detected.")
        return
    except Exception:
        pass

    # 2) Fallback: open via menu (MIS Reports → Application Wise Report)
    log("[nav] Opening via menu…")
    for sel in [
        "nav >> text=MIS Reports",
        "a:has-text('MIS Reports')",
        "button:has-text('MIS Reports')",
        "[role='menuitem']:has-text('MIS Reports')",
        "li:has-text('MIS Reports')",
        "text=MIS Reports"
    ]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=4000)
                break
            except Exception:
                pass

    for sel in [
        "a:has-text('Application Wise Report')",
        "[role='menuitem']:has-text('Application Wise Report')",
        "li:has-text('Application Wise Report')",
        "text=Application Wise Report"
    ]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=6000)
                break
            except Exception:
                pass

    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_selector("form", timeout=10000)
    log("[nav] Application Wise Report panel ready (form present).")

# ----------------------------------------------------------------------------------------------------------------------
# POST "Show Report" robustly
# ----------------------------------------------------------------------------------------------------------------------
async def post_show_report(page, status_text: str, from_str: str, to_str: str, tag: str):
    """
    Try to POST using the real report <form>. If there is no form (or it’s JS-constructed),
    fall back to Playwright's request API with session cookies (context.request.post).
    Saves raw HTML to out/server_{tag}.html.
    Returns (ok, html_text).
    """
    # Attempt in-page fetch using the existing form and credentials
    js = """
    async ({statusText, fromStr, toStr}) => {
      const form = document.querySelector("form") || document.forms[0];
      if (!form) return { ok:false, reason: "form not found" };

      const action = form.getAttribute("action") || location.pathname;
      const fd = new FormData(form);

      const trySet = (names, val) => {
        for (const n of names) {
          if (fd.has(n)) { fd.set(n, val); return true; }
        }
        if (names.length) { fd.append(names[0], val); return true; }
        return false;
      };

      trySet(["status","statusId","appStatus","applicationStatus"], statusText);
      trySet(["fromDate","fromdate","from_date"], fromStr);
      trySet(["toDate","todate","to_date"], toStr);

      if (!fd.has("submit") && !fd.has("show") && !fd.has("Show Report")) {
        fd.append("submit", "Show Report");
      }

      try {
        const resp = await fetch(action, { method:"POST", body:fd, credentials:"same-origin" });
        const ct = resp.headers.get("content-type") || "";
        const text = await resp.text();
        return { ok:true, status:resp.status, ct, text };
      } catch (e) {
        return { ok:false, reason:String(e) };
      }
    }
    """
    res = await page.evaluate(js, {"statusText": status_text, "fromStr": from_str, "toStr": to_str})

    # If the page doesn't have a form (observed in some sessions), use context.request with cookies
    if not res or not res.get("ok"):
        m = re.match(r"^(https?://[^/]+)", page.url)
        root = m.group(1) if m else "https://esinchai.punjab.gov.in"
        post_url = root + "/Authorities/applicationwisereport.jsp"

        fields = {
            "status": status_text,
            "fromDate": from_str,
            "toDate": to_str,
            "submit": "Show Report"
        }

        ctx = page.context
        resp = await ctx.request.post(post_url, form=fields)
        text = await resp.text()
        ct = resp.headers.get("content-type", "")
        has_table = bool(re.search(r"<table\\b.*?<tr\\b", text, re.I | re.S))
        log(f"[show] POST {post_url} via context.request → {resp.status} {ct}; table={has_table}")

        try:
            (OUT / f"server_{tag}.html").write_text(text, encoding="utf-8")
        except Exception:
            pass

        return True, text

    # Normal in-page path
    text = res.get("text") or ""
    ct   = (res.get("ct") or "").lower()
    status_code = res.get("status")
    has_table = bool(re.search(r"<table\\b.*?<tr\\b", text, re.I | re.S))
    log(f"[show] POST /Authorities/applicationwisereport.jsp → {status_code} {ct}; table={has_table}")

    try:
        (OUT / f"server_{tag}.html").write_text(text, encoding="utf-8")
    except Exception:
        pass

    return True, text

# ----------------------------------------------------------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------------------------------------------------------
async def login(context, login_url: str, username: str, password: str):
    page = await context.new_page()
    log(f"Opening login page: {login_url}")
    last_err = None
    for _ in range(2):
        try:
            await page.goto(login_url, wait_until="domcontentloaded")
            break
        except PWTimeout as e:
            last_err = e
    if last_err:
        raise last_err

    # Fill username/password using robust candidates
    u_cands = ["#username", "input[name='username']", "#loginid", "input[name='loginid']", "input[placeholder*='Login']"]
    p_cands = ["#password", "input[name='password']", "#pwd", "input[name='pwd']"]

    filled_u = False
    for sel in u_cands:
        if await page.locator(sel).first.count():
            try:
                await page.fill(sel, username, timeout=7000)
                filled_u = True
                break
            except Exception:
                pass

    filled_p = False
    for sel in p_cands:
        if await page.locator(sel).first.count():
            try:
                await page.fill(sel, password, timeout=7000)
                filled_p = True
                break
            except Exception:
                pass

    # Click login button
    clicked = False
    for sel in [
        "button:has-text('Login')",
        "button:has-text('Sign in')",
        "button[type='submit']",
        "[role='button']:has-text('Login')"
    ]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=5000)
                clicked = True
                break
            except Exception:
                pass

    # Wait for dashboard
    await page.wait_for_load_state("domcontentloaded")
    log("Login complete.")
    log(f"Current URL: {page.url}")
    return page

# ----------------------------------------------------------------------------------------------------------------------
# One report flow
# ----------------------------------------------------------------------------------------------------------------------
async def run_one(context, page, status_text: str, nice_name: str, from_str: str, to_str: str):
    # Ensure report page is ready
    await goto_report_page(page)

    tag = "delayed" if status_text.upper().startswith("DELAY") else "pending"

    ok, html = await post_show_report(page, status_text, from_str, to_str, tag)
    if not ok or not html:
        raise RuntimeError(f"Show Report POST failed for {status_text}")

    # Quick sanity check — in cases we *also* click site’s PDF, it often returns tiny/blank files.
    # We bypass that path and render the server HTML (which contains the table) to a clean PDF.
    fname = f"{nice_name} {today_str_filename()}.pdf"
    save_path = OUT / fname

    # If for some reason the returned HTML doesn’t contain a table, we still render the whole response,
    # so you can inspect the PDF and the saved server_{tag}.html.
    has_table = bool(re.search(r"<table\\b.*?<tr\\b", html, re.I | re.S))
    if not has_table:
        log("[warn] Returned HTML does not contain a table; rendering whole page for inspection.")

    await html_to_pdf(context, html, save_path, title=f"{nice_name} ({from_str} → {to_str})")
    log(f"Saved {fname}")
    return str(save_path)

# ----------------------------------------------------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------------------------------------------------
async def send_via_telegram(files):
    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        log("[tg] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping Telegram delivery.")
        return
    import requests
    for p in files:
        with open(p, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{bot}/sendDocument",
                data={"chat_id": chat},
                files={"document": (Path(p).name, f, "application/pdf")}
            )
        if r.status_code == 200:
            log(f"[tg] sent {Path(p).name}")
        else:
            log(f"[tg] failed for {p}: {r.text}")

# ----------------------------------------------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------------------------------------------
async def site_login_and_download():
    login_url = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username  = os.environ["USERNAME"]
    password  = os.environ["PASSWORD"]

    # Dates: 26/07/2024 → today (IST)
    from_str = "26/07/2024"
    to_str   = today_str_ddmmyyyy()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-breakpad",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--metrics-recording-only",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
            ]
        )
        context = await browser.new_context(accept_downloads=True)

        # Don't block CSS/images; some sites need them for layout. We can still block fonts.
        async def speed_filter(route, request):
            if request.resource_type == "font":
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(90000)

        page = await login(context, login_url, username, password)

        delayed_path = await run_one(context, page, "DELAYED", "Delayed Apps", from_str, to_str)
        pending_path = await run_one(context, page, "PENDING", "Pending Apps", from_str, to_str)

        await context.close(); await browser.close()

    return [delayed_path, pending_path]

async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join([Path(f).name for f in files]))
    try:
        await send_via_telegram(files)
    except Exception as e:
        log(f"Telegram send error (continuing): {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
