#!/usr/bin/env python3
import os, sys, re, asyncio, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

# =====================================================================================
# Config & tiny helpers
# =====================================================================================

BASE = Path(__file__).resolve().parent
OUT  = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"

def log(msg: str) -> None:
    print(msg, flush=True)

def today_ist_ddmmyyyy() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d-%m-%Y")

def today_ist_slashes() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d/%m/%Y")

# =====================================================================================
# Core: robust login and navigation
# =====================================================================================

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

    # Already logged in?
    if "/Authorities/" in page.url:
        log("Login complete (session cookie).")
        log(f"Current URL: {page.url}")
        return page

    # Pick user type if present (XEN per your usage)
    for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
        if await page.locator(sel).first.count():
            try:
                await page.select_option(sel, value="XEN")
                break
            except Exception:
                try:
                    await page.select_option(sel, label="XEN")
                    break
                except Exception:
                    pass

    # Fill username
    u_ok = False
    for sel in ["#username","input[name='username']","#loginid","input[name='loginid']",
                "input[placeholder*='Login' i]","input[placeholder*='Email' i]","input[placeholder*='Mobile' i]"]:
        if await page.locator(sel).first.count():
            try:
                await page.fill(sel, username, timeout=7000)
                u_ok = True
                break
            except Exception:
                pass

    # Fill password
    p_ok = False
    for sel in ["#password","input[name='password']","#pwd","input[name='pwd']","input[placeholder='Password']"]:
        if await page.locator(sel).first.count():
            try:
                await page.fill(sel, password, timeout=7000)
                p_ok = True
                break
            except Exception:
                pass

    # Click login
    clicked = False
    for sel in ["button:has-text('Login')","button:has-text('Sign in')","button[type='submit']",
                "[role='button']:has-text('Login')","input[type='submit'][value*='Login']"]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=5000)
                clicked = True
                break
            except Exception:
                pass

    if not clicked and p_ok:
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass

    def looks_like_dashboard(u: str, html: str) -> bool:
        return ("/Authorities/" in u) or ("Authorities Dashboard" in html) or ("MIS Reports" in html)

    html = await page.content()
    if not looks_like_dashboard(page.url, html):
        try:
            await page.wait_for_timeout(1500)
            html = await page.content()
        except Exception:
            pass

    if not looks_like_dashboard(page.url, html):
        log(f"[login] Still at {page.url}. Page did not transition to dashboard.")
        raise RuntimeError("Login did not reach dashboard.")

    log("Login complete.")
    log(f"Current URL: {page.url}")
    return page


async def goto_report_page(page):
    """
    Navigate directly to /Authorities/applicationwisereport.jsp.
    Consider ready when we see Show Report button, Status label or the heading.
    """
    async def ensure_not_logged_out():
        if "signup.jsp" in page.url:
            raise RuntimeError("Session bounced to signup.jsp; need to re-login.")

    m = re.match(r"^(https?://[^/]+)", page.url or "")
    root = m.group(1) if m else "https://esinchai.punjab.gov.in"

    if "/Authorities/" not in page.url:
        await page.goto(root + "/Authorities/authoritiesdashboard.jsp", wait_until="domcontentloaded")

    await ensure_not_logged_out()

    target = root + "/Authorities/applicationwisereport.jsp"
    await page.goto(target, wait_until="domcontentloaded")

    ready_sels = [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "label:has-text('Status')",
        "text=Application Wise Report"
    ]
    for _ in range(2):
        for sel in ready_sels:
            if await page.locator(sel).first.count():
                log("[nav] Application Wise Report panel ready.")
                return
        await page.wait_for_timeout(500)
        await ensure_not_logged_out()

    # menu fallback
    log("[nav] Opening via menu…")
    for sel in ["nav >> text=MIS Reports","a:has-text('MIS Reports')","button:has-text('MIS Reports')",
                "[role='menuitem']:has-text('MIS Reports')","li:has-text('MIS Reports')","text=MIS Reports"]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=4000)
                break
            except Exception:
                pass

    for sel in ["a:has-text('Application Wise Report')","[role='menuitem']:has-text('Application Wise Report')",
                "li:has-text('Application Wise Report')","text=Application Wise Report"]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=6000)
                break
            except Exception:
                pass

    await page.wait_for_load_state("domcontentloaded")
    await ensure_not_logged_out()

    for sel in ready_sels:
        if await page.locator(sel).first.count():
            log("[nav] Application Wise Report panel ready (menu path).")
            return

    raise RuntimeError("Could not detect the Application Wise Report panel (no Show Report/Status/heading).")

# =====================================================================================
# Filters (robust JS helpers)
# =====================================================================================

SET_SELECT_BY_LABEL_JS = """
(labelText, wanted, exact) => {
  const norm = s => (s||'').trim().toLowerCase();
  const L = Array.from(document.querySelectorAll('label'))
    .find(l => norm(l.textContent).includes(norm(labelText)));
  const findSelect = (root) => {
    if (!root) return null;
    // priority: sibling select, then nearest select in container
    let s = L && L.nextElementSibling && L.nextElementSibling.matches('select') ? L.nextElementSibling : null;
    if (!s) {
      const cands = Array.from((root.closest('div')||root).querySelectorAll('select'));
      s = cands.length ? cands[0] : null;
    }
    return s;
  };
  const sel = findSelect(L || document.body);
  if (!sel) return {ok:false, reason:'select not found'};
  const W = norm(wanted);
  let idx = -1;
  for (let i=0;i<sel.options.length;i++){
    const txt = norm(sel.options[i].textContent);
    if (exact ? (txt === W) : txt.includes(W)) { idx = i; break; }
  }
  if (idx === -1) return {ok:false, reason:'option not found'};
  if (sel.selectedIndex !== idx){
    sel.selectedIndex = idx;
    sel.dispatchEvent(new Event('input',{bubbles:true}));
    sel.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {ok:true};
}
"""

SELECT_ALL_IN_MULTI_JS = """
(labelText) => {
  const norm = s => (s||'').trim().toLowerCase();
  const L = Array.from(document.querySelectorAll('label'))
    .find(l => norm(l.textContent).includes(norm(labelText)));
  if (!L) return {ok:false, reason:'label not found'};
  const findSelect = (root) => {
    let s = L && L.nextElementSibling && L.nextElementSibling.matches('select') ? L.nextElementSibling : null;
    if (!s) {
      const cands = Array.from((root.closest('div')||root).querySelectorAll('select'));
      s = cands.length ? cands[0] : null;
    }
    return s;
  };
  const sel = findSelect(L || document.body);
  if (!sel) return {ok:false, reason:'select not found'};
  let changed = false;
  for (const o of sel.options) {
    if (!o.selected) { o.selected = true; changed = true; }
  }
  if (changed) {
    sel.dispatchEvent(new Event('input',{bubbles:true}));
    sel.dispatchEvent(new Event('change',{bubbles:true}));
  }
  const chosen = Array.from(sel.selectedOptions).map(o=>o.textContent.trim());
  return {ok:true, chosen};
}
"""

SET_INPUT_BY_LABEL_JS = """
(labelText, value) => {
  const norm = s => (s||'').trim().toLowerCase();
  const L = Array.from(document.querySelectorAll('label'))
    .find(l => norm(l.textContent).includes(norm(labelText)));
  if (!L) return {ok:false, reason:'label not found'};
  const root = L.closest('div') || L.parentElement || document.body;
  let inp = root.querySelector('input');
  if (!inp && L.nextElementSibling && L.nextElementSibling.matches('input')) {
    inp = L.nextElementSibling;
  }
  if (!inp) return {ok:false, reason:'input not found'};
  inp.value = value;
  inp.dispatchEvent(new Event('input',{bubbles:true}));
  inp.dispatchEvent(new Event('change',{bubbles:true}));
  return {ok:true, value:inp.value};
}
"""

async def set_dropdown_by_label(page, label_text: str, value_text: str, exact=True) -> bool:
    try:
        res = await page.evaluate(SET_SELECT_BY_LABEL_JS, label_text, value_text, bool(exact))
        ok = bool(res.get("ok"))
        if not ok and DEBUG:
            log(f"[filter] {label_text} failed: {res}")
        return ok
    except Exception:
        return False

async def select_all_multiselect(page, label_text: str) -> bool:
    try:
        res = await page.evaluate(SELECT_ALL_IN_MULTI_JS, label_text)
        ok = bool(res.get("ok"))
        if ok:
            chosen = res.get("chosen", [])
            log(f"[filter] {label_text} → Select All: {chosen if chosen else 'OK'}")
        return ok
    except Exception:
        return False

async def set_input_date(page, label_text: str, value_ddmmyyyy: str) -> bool:
    try:
        res = await page.evaluate(SET_INPUT_BY_LABEL_JS, label_text, value_ddmmyyyy)
        return bool(res.get("ok"))
    except Exception:
        return False

# =====================================================================================
# Show Report + capture POST HTML, then render DOM to PDF
# =====================================================================================

def has_table(html: str) -> bool:
    # very relaxed check
    return "<table" in html.lower() and "</table>" in html.lower()

async def click_show_report_and_capture(page) -> Optional[str]:
    """
    Clicks Show Report on the page and captures the POST HTML response
    from /Authorities/applicationwisereport.jsp. Returns HTML text or None.
    """
    # prepare waiter
    def _match(resp):
        try:
            return (resp.request.method == "POST") and ("/Authorities/applicationwisereport.jsp" in resp.url)
        except Exception:
            return False

    waiter = page.wait_for_response(_match, timeout=30000)

    # click Show Report
    clicked = False
    for sel in ["button:has-text('Show Report')","input[type='button'][value='Show Report']","text=Show Report"]:
        if await page.locator(sel).first.count():
            try:
                await page.locator(sel).first.click(timeout=8000)
                clicked = True
                break
            except Exception:
                pass
    if not clicked:
        raise RuntimeError("Show Report button not found")

    # await response
    try:
        resp = await waiter
        body = await resp.text()
        ctype = resp.headers.get("content-type","").lower()
        ok = ("text/html" in ctype) and has_table(body)
        log(f"[show] POST /Authorities/applicationwisereport.jsp → {resp.status} {ctype}; table={ok}")
        return body if ok else None
    except Exception:
        log("[show] POST capture failed or timed out.")
        return None

async def render_html_to_pdf(context, html: str, save_path: Path) -> None:
    """
    Spins up a fresh page, injects the server HTML into a minimal printable wrapper,
    and prints to PDF.
    """
    page = await context.new_page()
    # Basic wrapper to ensure fonts/backgrounds render
    wrapped = f"""
<!doctype html><html>
<head>
<meta charset="utf-8">
<title>Report</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; margin: 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #444; padding: 4px; font-size: 12px; }}
  thead th {{ background: #eee; }}
  .meta {{ margin-bottom: 10px; font-size: 12px; color: #333; }}
</style>
</head>
<body>
<div class="meta">Rendered from server HTML on {today_ist_ddmmyyyy()}</div>
<div id="report-root">{html}</div>
</body>
</html>
    """.strip()

    await page.set_content(wrapped, wait_until="load")
    # give dynamic images/fonts a brief chance (safe even if none)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    pdf_bytes = await page.pdf(print_background=True)
    Path(save_path).write_bytes(pdf_bytes)
    await page.close()
    log(f"[pdf:dom] rendered → {save_path}")

# =====================================================================================
# One run: set filters, dates, show, capture, render
# =====================================================================================

async def run_one(context, page, status_text: str, name_prefix: str, from_str: str, to_str: str) -> str:
    # Navigate to report page (idempotent)
    await goto_report_page(page)

    # Filters: Circle, Division, Nature(all), Status
    ok_c = await set_dropdown_by_label(page, "Circle Office", "LUDHIANA CANAL CIRCLE", exact=True)
    if not ok_c:
        log("[filter] Circle Office could not be set (will proceed anyway).")

    # Division list may load after Circle; give it a short moment
    await page.wait_for_timeout(700)
    ok_d = await set_dropdown_by_label(page, "Division Office", "FARIDKOT CANAL AND GROUND WATER DIVISION", exact=True)
    if not ok_d:
        log("[filter] Division Office could not be set (will proceed anyway).")

    # Nature of Application: select all
    ok_n = await select_all_multiselect(page, "Nature Of Application")
    if not ok_n:
        log("[filter] Nature Of Application select-all failed (will proceed anyway).")

    # Status
    ok_s = await set_dropdown_by_label(page, "Status", status_text, exact=False)
    if not ok_s:
        log(f"[filter] Status '{status_text}' could not be set (will proceed anyway).")

    # Dates (dd/mm/yyyy)
    ok_from = await set_input_date(page, "From Date", from_str)
    ok_to   = await set_input_date(page, "To Date", to_str)
    if not ok_from or not ok_to:
        log(f"[dates] Warning: failed to set one or both dates (From='{from_str}', To='{to_str}'). Proceeding.")

    # Click Show Report and capture server POST HTML
    html = await click_show_report_and_capture(page)
    if not html:
        log("[warn] Direct server PDF still small/blank or POST HTML not usable. Falling back to DOM render from current page HTML.")
        # take current page HTML as last resort
        try:
            html = await page.content()
        except Exception:
            html = "<div>No content captured</div>"

    # Render to PDF
    stamp = today_ist_ddmmyyyy()
    save_path = OUT / f"{name_prefix} {stamp}.pdf"
    await render_html_to_pdf(context, html, save_path)
    log(f"Saved {save_path.name}")
    return str(save_path)

# =====================================================================================
# Telegram
# =====================================================================================

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

# =====================================================================================
# Entry
# =====================================================================================

async def site_login_and_download():
    login_url = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username  = os.environ["USERNAME"]
    password  = os.environ["PASSWORD"]

    # Date range per your instruction
    from_str = "26/07/2024"
    to_str   = today_ist_slashes()  # dd/mm/yyyy

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-dev-shm-usage","--disable-extensions",
                "--disable-background-networking","--disable-background-timer-throttling",
                "--disable-breakpad","--disable-client-side-phishing-detection",
                "--disable-default-apps","--disable-hang-monitor","--disable-popup-blocking",
                "--metrics-recording-only","--no-first-run","--safebrowsing-disable-auto-update"
            ],
        )
        context = await browser.new_context(accept_downloads=True)
        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(90000)

        page = await login(context, login_url, username, password)

        delayed_path = await run_one(context, page, "DELAYED", "Delayed Apps", from_str, to_str)
        pending_path = await run_one(context, page, "PENDING", "Pending Apps", from_str, to_str)

        await context.close()
        await browser.close()
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
