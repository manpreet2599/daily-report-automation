#!/usr/bin/env python3
import os, sys, asyncio, traceback, re, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

# -------------------------- paths / flags --------------------------
BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"

# -------------------------- small utils ---------------------------
def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def today_str():
    # dd-mm-yyyy for filenames
    return ist_now().strftime("%d-%m-%Y")

def today_ddmmyyyy():
    # dd/mm/yyyy for form
    return ist_now().strftime("%d/%m/%Y")

def log(msg): 
    print(msg, flush=True)

async def snap(page, name, full=False):
    if not DEBUG: 
        return
    try:
        await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception:
        pass

# ===================== HTML → TABLE PICKER (SCORER) =====================

REPORT_HEADER_HINTS = [
    "sr", "sr.", "application", "farmer", "village", "outlet",
    "status", "date", "remarks", "justification", "mobile", "khasra"
]

def _score_table(html: str) -> int:
    """
    Score a <table> fragment:
      • rows/cols density
      • presence of report-like headers
      • numeric density
    Higher score → more likely to be the report grid.
    """
    low = html.lower()
    rows = len(re.findall(r"<tr\b", low))
    ths  = len(re.findall(r"<th\b", low))
    tds  = len(re.findall(r"<td\b", low))

    header_hits = 0
    head_slice = low[: min(len(low), 8000)]
    for kw in REPORT_HEADER_HINTS:
        if re.search(rf"\b{re.escape(kw)}", head_slice):
            header_hits += 1

    nums = len(re.findall(r">\s*\d[\d/\-]*\s*<", low))

    return rows*8 + ths*5 + tds*3 + header_hits*12 + min(nums, 120)

def extract_report_table(raw_html: str) -> str:
    """
    Pick the most report-like <table> from raw server HTML.
    Fallback: largest table by length.
    """
    if not raw_html:
        return ""

    table_pat = re.compile(r"<table\b[^>]*>.*?</table>", re.I | re.S)
    matches = list(table_pat.finditer(raw_html))
    if not matches:
        return ""

    scored = []
    for m in matches:
        frag = raw_html[m.start():m.end()]
        scored.append((_score_table(frag), m.start(), m.end(), frag))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, s, e, frag = scored[0]

    # If best is suspiciously tiny, use the largest table by length
    if (e - s) < 1500 and len(scored) > 1:
        frag = max(scored, key=lambda x: x[2]-x[1])[3]

    return frag

# ===================== RENDERER (nice, printable) =====================

def build_filters_header_html(status_text: str, from_str: str, to_str: str) -> str:
    # Simple summary above the table. You can tweak labels here freely.
    return f"""
    <h1>Application Wise Report</h1>
    <ul>
      <li><b>Status:</b> {status_text}</li>
      <li><b>Period:</b> From {from_str} to {to_str}</li>
    </ul>
    """

def wrap_report_html(raw_html: str, status_text: str, from_str: str, to_str: str) -> str:
    """
    Build a printable page with:
      - our explicit filter summary
      - only the best-scored data table (no filter UI)
    """
    head = build_filters_header_html(status_text, from_str, to_str)
    table_html = extract_report_table(raw_html)
    if not table_html:
        # last resort: dump everything
        table_html = raw_html

    return f"""<!doctype html><html><head><meta charset="utf-8"/>
    <style>
      @page {{ size: A4 landscape; margin: 10mm; }}
      body {{ font: 12px Arial, Helvetica, sans-serif; color:#111; }}
      h1 {{ font-size: 16px; margin: 0 0 6px 0; }}
      ul {{ margin:6px 0 12px 18px; padding:0; }}
      table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
      th,td {{ border:1px solid #999; padding:6px 8px; vertical-align:top; word-break:break-word; }}
      th {{ background:#f2f2f2; }}
      td {{ font-size: 11px; line-height: 1.25; }}
      thead {{ display: table-header-group; }}
      tfoot {{ display: table-footer-group; }}
    </style></head><body>
      {head}
      {table_html}
    </body></html>"""

async def render_dom_pdf(context, html: str, save_path: Path):
    """
    Open a fresh page with our HTML and print to PDF.
    """
    page = await context.new_page()
    await page.set_content(html, wait_until="load")
    await page.pdf(path=str(save_path), format="A4", landscape=True, margin={"top":"10mm","bottom":"10mm","left":"10mm","right":"10mm"})
    await page.close()

# ===================== REPORT POST (no flaky UI clicks) =====================

REPORT_URL = "/Authorities/applicationwisereport.jsp"

async def goto_report_page(page):
    """
    Open the Application Wise Report page (to ensure session + same origin).
    If the menu is brittle, direct-goto still works post-login.
    """
    base = re.match(r"^(https?://[^/]+)", page.url)
    if base:
        root = base.group(1)
    else:
        root = "https://esinchai.punjab.gov.in"
    url = root + REPORT_URL
    await page.goto(url, wait_until="domcontentloaded")

async def post_show_report(page, status_text: str, from_str: str, to_str: str, tag: str):
    """
    Serialize the existing form on the report page, inject Status + Dates,
    and POST using page.evaluate(fetch). Returns (ok, html_text).
    Also saves the raw HTML to out/server_{tag}.html for inspection.
    """
    js = """
    async ({statusText, fromStr, toStr}) => {
      const form = document.querySelector("form") || document.forms[0];
      if (!form) return { ok:false, reason: "form not found" };

      const action = form.getAttribute("action") || location.pathname;
      const fd = new FormData(form);

      // Try common names
      const trySet = (names, val) => {
        for (const n of names) {
          if (fd.has(n)) { fd.set(n, val); return true; }
        }
        // If missing, add the first name as new field
        if (names.length) { fd.append(names[0], val); return true; }
        return false;
      };

      // Inject our filters:
      trySet(["status","statusId","appStatus","applicationStatus"], statusText);
      trySet(["fromDate","fromdate","from_date"], fromStr);
      trySet(["toDate","todate","to_date"], toStr);

      // Some servers require a submit button name/value
      if (!fd.has("submit") && !fd.has("show") && !fd.has("Show Report")) {
        fd.append("submit", "Show Report");
      }

      try {
        const resp = await fetch(action, {
          method: "POST",
          body: fd,
          credentials: "same-origin"
        });
        const ct = resp.headers.get("content-type") || "";
        const text = await resp.text();
        return { ok:true, status: resp.status, ct, text };
      } catch (e) {
        return { ok:false, reason: String(e) };
      }
    }
    """
    res = await page.evaluate(js, {"statusText": status_text, "fromStr": from_str, "toStr": to_str})
    if not res or not res.get("ok"):
        log(f"[show] POST failed: {res}")
        return False, ""

    text = res.get("text") or ""
    ct   = (res.get("ct") or "").lower()
    status = res.get("status")

    # Quick presence check of a grid/table
    has_table = bool(re.search(r"<table\b.*?<tr\b", text, re.I | re.S))
    log(f"[show] POST {REPORT_URL} → {status} {ct}; table={has_table}")

    # Save raw server HTML for troubleshooting
    try:
        (OUT / f"server_{tag}.html").write_text(text, encoding="utf-8")
    except Exception:
        pass

    return True, text

# ===================== TELEGRAM =====================

async def send_via_telegram(files):
    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not bot or not chat:
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
            log(f"[tg] failed {Path(p).name}: {r.text}")

# ===================== LOGIN =====================

async def safe_fill(page, selectors, value):
    for sel in selectors:
        try:
            if await page.locator(sel).count():
                await page.fill(sel, value); return True
        except Exception:
            pass
    return False

async def safe_click(page, selectors, timeout=5000):
    for sel in selectors:
        try:
            if await page.locator(sel).count():
                await page.locator(sel).first.click(timeout=timeout); return True
        except Exception:
            pass
    return False

async def do_login(context, login_url, username, password, user_type=""):
    page = await context.new_page()
    log(f"Opening login page: {login_url}")
    last_err = None
    for _ in range(2):
        try:
            await page.goto(login_url, wait_until="domcontentloaded"); break
        except PWTimeout as e:
            last_err = e
    if last_err: 
        raise last_err

    # Optional user type
    if user_type:
        for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
            try:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=user_type)
                        break
                    except Exception:
                        try:
                            await page.select_option(sel, label=user_type)
                            break
                        except Exception:
                            pass
            except Exception:
                pass

    await safe_fill(page,
        ["#username","input[name='username']","input[placeholder*='Login']","input[placeholder*='Email']","input[placeholder*='Mobile']"],
        username
    )
    await safe_fill(page,
        ["#password","input[name='password']","input[name='pwd']","input[placeholder='Password']"],
        password
    )
    await safe_click(page,
        ["button:has-text('Login')","button:has-text('Sign in')","button[type='submit']","[role='button']:has-text('Login')"],
        timeout=6000
    )

    await page.wait_for_load_state("domcontentloaded", timeout=60000)
    log("Login complete.")
    log(f"Current URL: {page.url}")
    return page

# ===================== MAIN FLOW =====================

async def run_one(context, base_page, status_text: str, title: str, from_str: str, to_str: str):
    """
    Ensures we're on the report page, posts with our filters, renders clean PDF.
    """
    # Ensure we're on the report page (same origin & cookies)
    await goto_report_page(base_page)

    tag = status_text.lower().replace(" ", "_")
    ok, server_html = await post_show_report(base_page, status_text, from_str, to_str, tag)
    if not ok or not server_html:
        raise RuntimeError(f"Show Report POST failed for {status_text}")

    # If the server's own export is blank, we always render DOM ourselves.
    fname = f"{title} {today_str()}.pdf"
    out_path = OUT / fname

    # Build clean HTML and render
    html = wrap_report_html(server_html, status_text, from_str, to_str)
    await render_dom_pdf(context, html, out_path)
    log(f"[pdf:dom] rendered → {out_path}")
    log(f"Saved {out_path.name}")
    return str(out_path)

async def site_login_and_download():
    login_url = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username  = os.environ["USERNAME"]
    password  = os.environ["PASSWORD"]
    user_type = os.getenv("USER_TYPE", "").strip()

    # Dates: your requested window
    from_str = "26/07/2024"
    to_str   = today_ddmmyyyy()

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

        # Keep CSS/JS/images; only block fonts to reduce flakiness
        async def speed_filter(route, request):
            if request.resource_type in ("font",):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(90000)

        # Login
        page = await do_login(context, login_url, username, password, user_type=user_type)

        # Run both reports
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
    except Exception:
        traceback.print_exc()
        sys.exit(1)
