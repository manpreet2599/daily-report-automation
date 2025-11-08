#!/usr/bin/env python3
import os, sys, asyncio, traceback, base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

from dotenv import load_dotenv
load_dotenv()

# ------------ paths / settings ------------
BASE = Path(__file__).resolve().parent
OUT  = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))
def today_ist(): return datetime.now(IST)
def today_ddmmyyyy(): return today_ist().strftime("%d/%m/%Y")
def today_fname(): return today_ist().strftime("%d-%m-%Y")
def log(m): print(m, flush=True)

# “small/blank” PDFs from the server were ~29–30 KB in your logs
MIN_VALID_PDF_BYTES = 50000

# ------------ constants from your report ------------
LOGIN_URL   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
BASE_ORIGIN = "https://esinchai.punjab.gov.in"

# Endpoints observed in your HAR (and common fallbacks)
SHOW_REPORT_ENDPOINTS = [
    "/Authorities/applicationwisereport.jsp",
    "/Authorities/applicationwisereport.do",   # fallback guess
]
PDF_EXPORT_ENDPOINTS = [
    "/Authorities/applicationwisereportpdf.jsp",
    "/Authorities/applicationwisereport.pdf",  # fallback guess
]

# Your selections
CIRCLE   = "LUDHIANA CANAL CIRCLE"
DIVISION = "FARIDKOT CANAL AND GROUND WATER DIVISION"
NATURE_ALL = [
    "Amendment in Warabandi",
    "Change of alignment of watercourse",
    "Conversion of U.C.A. to C.C.A.",
    "Demand of new watercourse",
    "Inclusion of out of chak area",
    "New Warabandi",
    "Restoration of running watercourse that has been dismantled",
    "Sanction of new outlet",
    "Shifting of head of outlet",
    "Splitting of existing outlet",
    "Transfer of area from one outlet to another",
]

FROM_DATE = "26/07/2024"
TO_DATE   = today_ddmmyyyy()

# ------------ utilities ------------
async def save_response_to_file(resp, dest: Path) -> int:
    body = await resp.body()
    dest.write_bytes(body)
    size = dest.stat().st_size
    log(f"[net] saved → {dest} ({size} bytes)")
    return size

async def render_simple_dom_pdf(page, html: str, pdf_path: Path, landscape: bool = True):
    ctx = page.context
    tmp = await ctx.new_page()
    await tmp.set_content(html, wait_until="load")
    await tmp.emulate_media(media="print")
    await tmp.pdf(path=str(pdf_path), format="A4", print_background=True, landscape=landscape)
    await tmp.close()
    log(f"[pdf:dom] rendered → {pdf_path}")

def build_filters_header_html(status_text: str):
    def li(name, val):
        if isinstance(val, list): val = ", ".join(val)
        return f"<li><b>{name}:</b> {val}</li>"
    return f"""
    <h1 style="margin:0 0 6px 0;font:600 16px Arial">E-SINSCHAI: APPLICATION WISE DETAILS REPORT</h1>
    <div style="font:13px Arial;margin:6px 0 10px 0;"><b>Period:</b> From {FROM_DATE} to {TO_DATE}</div>
    <ul style="margin:6px 0 12px 18px;font:13px Arial">
      {li("Circle Office", CIRCLE)}
      {li("Division Office", DIVISION)}
      {li("Nature Of Application", NATURE_ALL)}
      {li("Status", status_text)}
    </ul>
    """

# ------------ login helpers (robust selectors) ------------
USERNAME_CANDS = [
    "#username","input#username","input[name='username']","input[name='loginid']","input[name='userid']","input[name='login']",
    "input[placeholder*='email' i]","input[placeholder*='mobile' i]","input[placeholder*='login' i]"
]
PASSWORD_CANDS = ["#password","input#password","input[name='password']","input[name='pwd']","input[placeholder='Password']"]
USERTYPE_CANDS = ["select#usertype","select#userType","select[name='userType']","select#user_type"]
LOGIN_BUTTON_CANDS = [
    "button:has-text('Login')","button:has-text('Sign in')","button[type='submit']","[role='button']:has-text('Login')"
]

async def fill_any(page, cands, value) -> bool:
    for sel in cands:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.fill(value)
                return True
        except Exception:
            pass
    return False

async def click_any(page, cands, timeout=6000) -> bool:
    for sel in cands:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False

# ------------ core: replay the requests ------------
async def post_show_report(page, status_text: str) -> bool:
    """
    Replays the Show Report form submit using the logged-in request context.
    Some JSPs cache criteria in session; doing this first makes the subsequent
    PDF export deterministic.
    """
    params_variants = []
    # Try common key shapes for multi-select
    for nature_key in ("natureOfApplication", "natureOfApplication[]", "nature", "nature[]"):
        params_variants.append({
            "circleOffice": CIRCLE,
            "divisionOffice": DIVISION,
            nature_key: NATURE_ALL,
            "status": status_text,
            "fromDate": FROM_DATE,
            "toDate": TO_DATE
        })

    for path in SHOW_REPORT_ENDPOINTS:
        url = BASE_ORIGIN + path
        for form in params_variants:
            try:
                # use application/x-www-form-urlencoded with repeated keys for arrays
                # Playwright encodes lists as repeated keys when using 'form' argument.
                resp = await page.request.post(url, form=form, timeout=60000)
                ok = resp.ok
                ctype = resp.headers.get("content-type","").lower()
                text = await resp.text() if "text" in ctype or "html" in ctype else ""
                contains_table = ("<table" in text.lower()) and ("tbody" in text.lower())
                log(f"[show] POST {path} → {resp.status} {ctype}; table={contains_table}")
                if ok:
                    return True
            except Exception as e:
                log(f"[show] error@{path}: {e}")
    return False

async def fetch_pdf(page, status_text: str, save_path: Path) -> bool:
    """
    Calls the PDF export endpoint directly with the same parameters.
    Tries GET with different key shapes (array vs non-array). Falls back to POST if needed.
    """
    # parameter variants
    params_sets = []
    for nature_key in ("natureOfApplication", "natureOfApplication[]", "nature", "nature[]"):
        base = {
            "circleOffice": CIRCLE,
            "divisionOffice": DIVISION,
            "status": status_text,
            "fromDate": FROM_DATE,
            "toDate": TO_DATE
        }
        # duplicate arrays under different keys
        params_sets.append({**base, nature_key: NATURE_ALL})

    # 1) Try GET on known endpoints
    for path in PDF_EXPORT_ENDPOINTS:
        url = BASE_ORIGIN + path
        for params in params_sets:
            try:
                resp = await page.request.get(url, params=params, timeout=60000)
                ctype = resp.headers.get("content-type","").lower()
                if resp.ok and "pdf" in ctype:
                    size = await save_response_to_file(resp, save_path)
                    if size >= MIN_VALID_PDF_BYTES:
                        return True
                    log(f"[pdf] server PDF looks small ({size} bytes).")
            except Exception as e:
                log(f"[pdf] GET error@{path}: {e}")

    # 2) Try POST to the export endpoint (some servers require POST)
    for path in PDF_EXPORT_ENDPOINTS:
        url = BASE_ORIGIN + path
        for form in params_sets:
            try:
                resp = await page.request.post(url, form=form, timeout=60000)
                ctype = resp.headers.get("content-type","").lower()
                if resp.ok and "pdf" in ctype:
                    size = await save_response_to_file(resp, save_path)
                    if size >= MIN_VALID_PDF_BYTES:
                        return True
                    log(f"[pdf] server PDF looks small ({size} bytes).")
            except Exception as e:
                log(f"[pdf] POST error@{path}: {e}")

    return False

async def dom_fallback_from_grid(page, status_text: str, pdf_path: Path):
    """
    If direct export stays small/blank, we render a clean PDF from the live grid HTML.
    Assumes the grid is already present on the page (manual Show Report already done
    via post_show_report()).
    """
    # Pull just the main table under the green report header
    payload = await page.evaluate("""() => {
      const section = document.querySelector('div') || document.body;
      const tbl = document.querySelector('#myTable') || document.querySelector('table');
      return {
        tableHTML: tbl ? tbl.outerHTML : ''
      };
    }""")
    table_html = (payload or {}).get("tableHTML") or ""
    if not table_html:
        # Last-resort screenshot → PDF
        png = await page.screenshot(type="png", full_page=True)
        b64 = base64.b64encode(png).decode("ascii")
        html = f"""<!doctype html><html><head><meta charset="utf-8">
        <style>html,body{{margin:0}}.wrap{{padding:8mm}}img{{width:100%}}</style></head>
        <body><div class="wrap"><img src="data:image/png;base64,{b64}"/></div></body></html>"""
        await render_simple_dom_pdf(page, html, pdf_path, landscape=False)
        return

    head = build_filters_header_html(status_text)
    html = f"""<!doctype html><html><head><meta charset="utf-8"/>
    <style>
      @page {{ size: A4 landscape; margin: 10mm; }}
      body {{ font: 12px Arial, Helvetica, sans-serif; color:#111; }}
      table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
      th,td {{ border:1px solid #999; padding:6px 8px; vertical-align:top; word-break:break-word; }}
      th {{ background:#f2f2f2; }}
    </style></head><body>
      {head}
      {table_html}
    </body></html>"""
    await render_simple_dom_pdf(page, html, pdf_path, landscape=True)

# ------------ main flow ------------
async def site_login_and_download():
    username = os.environ["USERNAME"]
    password = os.environ["PASSWORD"]
    user_type = os.getenv("USER_TYPE", "").strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--no-first-run","--disable-popup-blocking"]
        )
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        log(f"Opening login page: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        if user_type:
            for sel in USERTYPE_CANDS:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=user_type); break
                    except Exception:
                        try: await page.select_option(sel, label=user_type); break
                        except Exception: pass

        if not await fill_any(page, USERNAME_CANDS, username):
            raise RuntimeError("Username input not found")
        if not await fill_any(page, PASSWORD_CANDS, password):
            raise RuntimeError("Password input not found")
        if not await click_any(page, LOGIN_BUTTON_CANDS, timeout=8000):
            raise RuntimeError("Could not click Login")

        await page.wait_for_load_state("domcontentloaded")
        log("Login complete.")
        log(f"Current URL: {page.url}")

        async def run_one(status_text: str, base_name: str):
            # 1) Session-prepare by replaying Show Report POST
            ok = await post_show_report(page, status_text)
            if not ok:
                log("[warn] Show Report POST did not return HTML 200, continuing to PDF fetch…")

            # 2) Export PDF directly
            pdf_path = OUT / f"{base_name} {today_fname()}.pdf"
            got = await fetch_pdf(page, status_text, pdf_path)
            if got:
                log(f"Saved {pdf_path.name}")
                return str(pdf_path)

            # 3) Fallback render from DOM (guaranteed non-empty)
            log("[warn] Direct server PDF still small/blank. Falling back to DOM render.")
            await dom_fallback_from_grid(page, status_text, pdf_path)
            log(f"Saved {pdf_path.name}")
            return str(pdf_path)

        a = await run_one("DELAYED", "Delayed Apps")
        b = await run_one("PENDING", "Pending Apps")

        await context.close(); await browser.close()
        return [a, b]

# ------------ entrypoint ------------
async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join(Path(f).name for f in files))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
