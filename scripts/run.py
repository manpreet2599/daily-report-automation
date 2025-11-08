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
def now_ist(): return datetime.now(IST)
def today_ddmmyyyy(): return now_ist().strftime("%d/%m/%Y")
def today_fname(): return now_ist().strftime("%d-%m-%Y")
def log(m): print(m, flush=True)

# PDFs smaller than this were the blank server exports you saw
MIN_VALID_PDF_BYTES = 50000

# ------------ constants ------------
LOGIN_URL   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
BASE_ORIGIN = "https://esinchai.punjab.gov.in"

# Endpoints (from HAR + sane fallbacks)
SHOW_REPORT_ENDPOINTS = [
    "/Authorities/applicationwisereport.jsp",
    "/Authorities/applicationwisereport.do",
]
PDF_EXPORT_ENDPOINTS = [
    "/Authorities/applicationwisereportpdf.jsp",
    "/Authorities/applicationwisereport.pdf",
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

async def render_html_to_pdf(page, html: str, pdf_path: Path, landscape: bool = True):
    """Render provided HTML into a fresh page → PDF."""
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

def wrap_report_html(raw_html: str, status_text: str) -> str:
    """
    Take the HTML returned by the Show Report POST and wrap it into a clean,
    printable page with your chosen filters header.
    """
    head = build_filters_header_html(status_text)
    # Try to extract the main report table if present; otherwise keep full HTML.
    # We search for the first sizeable table.
    body = raw_html
    try:
        lower = raw_html.lower()
        start = lower.find("<table")
        if start != -1:
            end = lower.find("</table>", start)
            if end != -1:
                end += len("</table>")
                body = raw_html[start:end]
    except Exception:
        pass

    return f"""<!doctype html><html><head><meta charset="utf-8"/>
    <style>
      @page {{ size: A4 landscape; margin: 10mm; }}
      body {{ font: 12px Arial, Helvetica, sans-serif; color:#111; }}
      table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
      th,td {{ border:1px solid #999; padding:6px 8px; vertical-align:top; word-break:break-word; }}
      th {{ background:#f2f2f2; }}
    </style></head><body>
      {head}
      {body}
    </body></html>"""

# ------------ login helpers ------------
USERNAME_CANDS = [
    "#username","input#username","input[name='username']","input[name='loginid']",
    "input[name='userid']","input[name='login']",
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

async def click_any(page, cands, timeout=8000) -> bool:
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
async def post_show_report(page, status_text: str):
    """
    Replays the Show Report form submit using the logged-in request context.
    Returns (ok: bool, html: str | None).
    """
    params_variants = []
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
                resp = await page.request.post(url, form=form, timeout=60000)
                ok = resp.ok
                ctype = resp.headers.get("content-type","").lower()
                text = await resp.text()
                contains_table = ("<table" in text.lower()) and ("tbody" in text.lower())
                log(f"[show] POST {path} → {resp.status} {ctype}; table={contains_table}")
                if ok:
                    return True, text
            except Exception as e:
                log(f"[show] error@{path}: {e}")
    return False, None

async def fetch_pdf(page, status_text: str, save_path: Path) -> bool:
    """
    Calls the PDF export endpoint directly with the same parameters.
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
        params_sets.append({**base, nature_key: NATURE_ALL})

    # 1) GET
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

    # 2) POST
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

async def render_from_show_html(page, show_html: str, status_text: str, pdf_path: Path):
    """Guaranteed non-empty: render the HTML returned by Show Report POST."""
    printable = wrap_report_html(show_html, status_text)
    await render_html_to_pdf(page, printable, pdf_path, landscape=True)

# ------------ Telegram ------------
async def send_via_telegram(paths):
    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        log("[tg] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping Telegram.")
        return
    import requests
    for p in paths:
        name = Path(p).name
        try:
            with open(p, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{bot}/sendDocument",
                    data={"chat_id": chat},
                    files={"document": (name, f, "application/pdf")},
                    timeout=60
                )
            if r.status_code != 200:
                log(f"[tg] send failed for {name}: {r.text}")
            else:
                log(f"[tg] sent {name}")
        except Exception as e:
            log(f"[tg] error sending {name}: {e}")

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
            # 1) Prepare server state + capture the full HTML of the report
            ok, show_html = await post_show_report(page, status_text)

            pdf_path = OUT / f"{base_name} {today_fname()}.pdf"

            # 2) Try the server's PDF export
            got = await fetch_pdf(page, status_text, pdf_path)
            if got:
                log(f"Saved {pdf_path.name}")
                return str(pdf_path)

            # 3) Guaranteed fallback using the HTML returned by Show Report
            if not ok or not show_html:
                log("[warn] Show Report HTML not captured; using white-page screenshot fallback.")
                # last-resort: screenshot → PDF
                png = await page.screenshot(type="png", full_page=True)
                b64 = base64.b64encode(png).decode("ascii")
                html = f"""<!doctype html><html><head><meta charset="utf-8">
                <style>html,body{{margin:0}}.wrap{{padding:8mm}}img{{width:100%}}</style></head>
                <body><div class="wrap"><img src="data:image/png;base64,{b64}"/></div></body></html>"""
                await render_html_to_pdf(page, html, pdf_path, landscape=False)
                log(f"Saved {pdf_path.name}")
                return str(pdf_path)

            log("[warn] Direct server PDF still small/blank. Falling back to DOM render from POST HTML.")
            await render_from_show_html(page, show_html, status_text, pdf_path)
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
    # Telegram send (best-effort; does not abort on error)
    try:
        await send_via_telegram(files)
    except Exception as e:
        log(f"[tg] send error (continuing): {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
