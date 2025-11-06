#!/usr/bin/env python3
import os, sys, asyncio, traceback, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"

def today_str():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d-%m-%Y")

def log(msg): print(msg, flush=True)

async def snap(page, name, full=False):
    if not DEBUG: return
    try: await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception: pass

# ---------- Strong selectors for date inputs ----------
FROM_DATE_CANDIDATES = [
    "#fromDate", "input#fromDate", "input[name='fromDate']",
    "input[name*='fromdate' i]", "input[name*='from' i][type='text']",
    "xpath=//label[contains(.,'From Date')]/following::input[1]"
]
TO_DATE_CANDIDATES = [
    "#toDate", "input#toDate", "input[name='toDate']",
    "input[name*='todate' i]", "input[name*='to' i][type='text']",
    "xpath=//label[contains(.,'To Date')]/following::input[1]"
]

async def get_first_input_value(p, candidates) -> str:
    for sel in candidates:
        try:
            loc = p.locator(sel).first
            if await loc.count():
                v = (await loc.input_value()).strip()
                if v: return v
        except Exception: pass
    return ""

async def set_first_input_value(p, candidates, value: str) -> bool:
    for sel in candidates:
        try:
            loc = p.locator(sel).first
            if await loc.count():
                await loc.fill(value)
                # fire events in case site listens
                await p.evaluate("""
                (sel)=>{
                  const el = document.querySelector(sel);
                  if(!el) return;
                  el.dispatchEvent(new Event('input',{bubbles:true}));
                  el.dispatchEvent(new Event('change',{bubbles:true}));
                }""", sel.replace("xpath=",""))  # fine for css/xpath; harmless if css
                return True
        except Exception: pass
    return False

# ---------- Data-row readiness checks ----------
async def _has_data_rows(page):
    try:
        return await page.evaluate("""
            () => {
                const tbodies = Array.from(document.querySelectorAll('table tbody'));
                for (const tb of tbodies) {
                    const rows = Array.from(tb.querySelectorAll('tr'));
                    const dataRows = rows.filter(r => {
                        const tds = Array.from(r.querySelectorAll('td')).map(td => (td.innerText||'').trim());
                        const nonEmpty = tds.filter(x => x && x !== '\\u00A0');
                        return nonEmpty.length >= 2;
                    });
                    if (dataRows.length > 0) return true;
                }
                return false;
            }
        """)
    except Exception:
        return False

async def _wait_for_data_rows(page, timeout_ms=30000):
    end = asyncio.get_event_loop().time() + timeout_ms/1000.0
    try: await page.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
    except Exception: pass
    while asyncio.get_event_loop().time() < end:
        if await _has_data_rows(page): return True
        await asyncio.sleep(0.3)
    return False

# ---------- Download helpers (unchanged) ----------
async def click_and_wait_download(p, click_pdf, save_as_path, timeout_ms=35000):
    log("[pdf] trying direct download…")
    try:
        async with p.expect_download(timeout=timeout_ms) as dl_info:
            await click_pdf()
        dl = await dl_info.value
        await dl.save_as(save_as_path)
        log(f"[pdf] saved → {save_as_path}")
        return True
    except Exception as e:
        log(f"[pdf] no direct download ({e}); trying popup…")
        try:
            async with p.expect_popup(timeout=5000) as pop_info:
                await click_pdf()
            pop = await pop_info.value
            await pop.wait_for_load_state("load")
            link = pop.locator("a[href$='.pdf'], a[download], a[href*='application/pdf']").first
            if await link.count():
                async with pop.expect_download(timeout=timeout_ms) as dl_info2:
                    await link.click()
                dl2 = await dl_info2.value
                await dl2.save_as(save_as_path)
                await pop.close()
                log(f"[pdf] popup → saved → {save_as_path}")
                return True
            await pop.close()
            return False
        except Exception as e2:
            log(f"[pdf] popup fallback failed: {e2}")
            return False

# ---------- UI helpers (unchanged sections omitted for brevity in this comment) ----------
async def fill_first(page, candidates, value):
    for sel in candidates:
        try:
            if await page.locator(sel).count():
                await page.fill(sel, value); return True
        except Exception: pass
    return False

async def click_first(page, candidates, timeout=3000):
    for sel in candidates:
        try:
            if await page.locator(sel).count():
                await page.locator(sel).first.click(timeout=timeout); return True
        except Exception: pass
    return False

async def _dump_near(p, label_text: str, tag: str):
    if not DEBUG: return
    try:
        html = await p.evaluate(
            """(txt)=>{
              const L=[...document.querySelectorAll('label')].find(l=>(l.textContent||'').trim().toLowerCase().includes(txt.toLowerCase()));
              const el=L?.closest('div')||L?.parentElement||document.body;
              return (el?.outerHTML||'').slice(0,8000);
            }""", label_text
        )
        (OUT / f"debug_near_{tag}.html").write_text(html or "", encoding="utf-8")
    except Exception: pass

# (Dropdown helpers from your current script remain the same; omitted here to save space)

# ---------- After Show Report ----------
async def show_report_and_wait(page):
    await click_first(page, [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "text=Show Report"
    ], timeout=8000)

    ok = await _wait_for_data_rows(page, timeout_ms=30000)
    if not ok:
        try:
            await page.get_by_text("No record", exact=False).wait_for(timeout=3000)
            return
        except Exception:
            raise RuntimeError("Report table did not populate with data rows.")

# ---------- Click red PDF icon ----------
async def click_report_pdf_icon(page):
    for sel in [
        "xpath=//div[contains(.,'Application Wise Report')]//img[contains(@src,'pdf') or contains(@alt,'PDF')]",
        "xpath=(//img[contains(@src,'pdf') or contains(@alt,'PDF')])[1]",
        "xpath=(//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]:
        if await page.locator(sel).count():
            ico = page.locator(sel).first
            await ico.scroll_into_view_if_needed()
            await ico.click(timeout=6000, force=True)
            return True
    return False

# ---------- Robust PDF download with settle + size check ----------
async def download_report_pdf_with_checks(page, save_path: Path, settle_ms: int = 5600, min_bytes: int = 7000):
    if not await _has_data_rows(page):
        log("[pdf] Warning: no data rows detected just before download; giving 1.5s grace.")
        await asyncio.sleep(1.5)
    if settle_ms > 0: await asyncio.sleep(settle_ms / 1000)

    async def do_pdf_click():
        if not await click_report_pdf_icon(page):
            raise RuntimeError("Red PDF icon not found")

    ok_dl = await click_and_wait_download(page, do_pdf_click, save_path, timeout_ms=35000)
    if not ok_dl: return False

    try:
        size = Path(save_path).stat().st_size
        log(f"[pdf] size: {size} bytes")
        if size < min_bytes:
            log("[pdf] File looks too small; retrying once after 3s…")
            await asyncio.sleep(3.0)
            ok_dl2 = await click_and_wait_download(page, do_pdf_click, save_path, timeout_ms=35000)
            if not ok_dl2: return False
            size2 = Path(save_path).stat().st_size
            log(f"[pdf] retry size: {size2} bytes")
            if size2 < min_bytes:
                log("[pdf] Still small after retry; likely headers-only.")
                return False
    except FileNotFoundError:
        log("[pdf] Save failed; retrying once after 3s…")
        await asyncio.sleep(3.0)
        if not await click_and_wait_download(page, do_pdf_click, save_path, timeout_ms=35000):
            return False
    return True

# ---------------- main flow ----------------
async def site_login_and_download():
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = today_str()

    # capture here for reuse + guard script
    captured_from = ""
    captured_to   = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-extensions",
                  "--disable-background-networking","--disable-background-timer-throttling",
                  "--disable-breakpad","--disable-client-side-phishing-detection","--disable-default-apps",
                  "--disable-hang-monitor","--disable-ipc-flooding-protection","--disable-popup-blocking",
                  "--disable-prompt-on-repost","--metrics-recording-only","--no-first-run",
                  "--safebrowsing-disable-auto-update"]
        )
        context = await browser.new_context(accept_downloads=True)

        async def speed_filter(route, request):
            rtype = request.resource_type
            if rtype in ("font",):  # keep images/css; datepicker might rely on them
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(90000)

        page = await context.new_page()

        # --- Guard that auto-restores dates if site JS clears them ---
        await page.add_init_script("""
        (function(){
          window.__keepDates = {from:'', to:''};
          const setBack = () => {
            const f = document.querySelector('#fromDate, input[name="fromDate"], input[name*="fromdate" i], input[name*="from" i][type="text"]');
            const t = document.querySelector('#toDate,   input[name="toDate"],   input[name*="todate" i],   input[name*="to"   i][type="text"]');
            if (f && window.__keepDates.from && !f.value) {
              f.value = window.__keepDates.from;
              f.dispatchEvent(new Event('input',{bubbles:true}));
              f.dispatchEvent(new Event('change',{bubbles:true}));
            }
            if (t && window.__keepDates.to && !t.value) {
              t.value = window.__keepDates.to;
              t.dispatchEvent(new Event('input',{bubbles:true}));
              t.dispatchEvent(new Event('change',{bubbles:true}));
            }
          };
          const mo = new MutationObserver(setBack);
          document.addEventListener('DOMContentLoaded', ()=>setBack());
          mo.observe(document.documentElement, {subtree:true, childList:true, attributes:true});
          setInterval(setBack, 500); // very light; ensures restoration even after async clears
        })();
        """)

        # --- Login ---
        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="domcontentloaded"); break
            except PWTimeout as e: last_err = e
        if last_err: raise last_err

        await snap(page, "step1_login_page.png")
        if user_type:
            for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
                if await page.locator(sel).count():
                    try: await page.select_option(sel, value=user_type); break
                    except Exception:
                        try: await page.select_option(sel, label=user_type); break
                        except Exception: pass
        await snap(page, "after_user_type.png")

        await fill_first(page,
            ["input[name='username']", "#username", "input[name='login']", "#login",
             "input[name='userid']", "#userid", "#loginid", "input[name='loginid']",
             "input[placeholder*='Email']", "input[placeholder*='Mobile']", "input[placeholder*='Login']"],
            os.environ["USERNAME"])
        await fill_first(page,
            ["input[name='password']", "#password", "input[name='pwd']", "#pwd", "input[placeholder='Password']"],
            os.environ["PASSWORD"])
        await snap(page, "step2_before_login.png")

        await click_first(page,
            ["button:has-text('Login')","button:has-text('Sign in')",
             "button[type='submit']","[role='button']:has-text('Login')"], timeout=5000)

        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        await snap(page, "step3_after_login.png")
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        # --- Navigate to Application Wise Report ---
        async def open_application_wise(p):
            log("[A] Opening 'MIS Reports'…")
            if not await click_first(p, [
                "nav >> text=MIS Reports","text=MIS Reports","a:has-text('MIS Reports')",
                "[role='menuitem']:has-text('MIS Reports')","button:has-text('MIS Reports')","li:has-text('MIS Reports')"
            ], timeout=3000):
                await p.get_by_text("MIS Reports", exact=False).first.click()
            await p.wait_for_timeout(150)
            await snap(p, "after_open_mis.png")

            log("[A] Clicking 'Application Wise Report'…")
            ok = await click_first(p, [
                "text=Application Wise Report","a:has-text('Application Wise Report')",
                "[role='menuitem']:has-text('Application Wise Report')","li:has-text('Application Wise Report')"
            ], timeout=6000)
            if not ok: raise RuntimeError("[A] Could not open 'Application Wise Report'")

            try:
                await p.wait_for_url(re.compile(r".*/Authorities/applicationwisereport\.jsp.*"), timeout=20000)
            except Exception:
                await p.get_by_text("Application Wise Report", exact=False).wait_for(timeout=12000)

            await p.wait_for_load_state("domcontentloaded")
            await snap(p, "after_open_app_wise.png")

            # Capture default dates precisely
            nonlocal captured_from, captured_to
            captured_from = await get_first_input_value(p, FROM_DATE_CANDIDATES)
            captured_to   = await get_first_input_value(p, TO_DATE_CANDIDATES)
            log(f"[dates] captured defaults: From='{captured_from}' To='{captured_to}'")

            # feed the guard values so any clears are auto-restored
            await p.evaluate("(f,t)=>{window.__keepDates.from=f; window.__keepDates.to=t;}", captured_from, captured_to)

        # (Your dropdown helpers are used here as-is)
        from_select_circle = select_circle
        from_select_division = select_division
        from_select_nature_all = select_nature_all
        from_select_status = select_status

        async def apply_common_filters(p):
            ok1 = await from_select_circle(p, "LUDHIANA CANAL CIRCLE")
            ok2 = await from_select_division(p, "FARIDKOT CANAL AND GROUND WATER DIVISION")
            ok3 = await from_select_nature_all(p)
            (OUT / "dropdown_warning.txt").write_text(f"Circle:{ok1} Division:{ok2} NatureAll:{ok3}\n", encoding="utf-8")

        async def set_status_and_download(p, status_text: str, save_path: Path):
            ok4 = await from_select_status(p, status_text)
            log(f"[A] Status set to '{status_text}' (ok={ok4})")

            # If either date is blank now, restore captured defaults
            cur_from = await get_first_input_value(p, FROM_DATE_CANDIDATES)
            cur_to   = await get_first_input_value(p, TO_DATE_CANDIDATES)
            if not cur_from and captured_from:
                await set_first_input_value(p, FROM_DATE_CANDIDATES, captured_from)
            if not cur_to and captured_to:
                await set_first_input_value(p, TO_DATE_CANDIDATES, captured_to)
            log(f"[dates] before Show Report: From='{await get_first_input_value(p, FROM_DATE_CANDIDATES)}' To='{await get_first_input_value(p, TO_DATE_CANDIDATES)}'")

            await show_report_and_wait(p)
            await snap(p, f"after_grid_shown_{status_text.lower()}.png")
            log(f"[A] Report grid is visible ({status_text}).")

            ok_dl = await download_report_pdf_with_checks(p, save_path=save_path, settle_ms=5600, min_bytes=7000)
            if not ok_dl:
                await snap(p, f"fail_pdf_click_{status_text.lower()}.png")
                raise RuntimeError(f"[A] Could not obtain a valid PDF ({status_text})")
            log(f"[A] PDF saved → {save_path}")

        await open_application_wise(page)
        await apply_common_filters(page)

        pathA = OUT / f"Delayed Apps {today_str()}.pdf"
        await set_status_and_download(page, "DELAYED", pathA)
        log(f"Saved {pathA.name}")

        pathB = OUT / f"Pending Apps {today_str()}.pdf"
        await set_status_and_download(page, "PENDING", pathB)
        log(f"Saved {pathB.name}")

        await context.close(); await browser.close()
    return [str(pathA), str(pathB)]

# --------------- Telegram (unchanged) ---------------
async def send_via_telegram(files):
    bot = os.getenv("TELEGRAM_BOT_TOKEN"); chat = os.getenv("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        log("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping Telegram delivery."); return
    import requests
    for p in files:
        with open(p, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{bot}/sendDocument",
                              data={"chat_id": chat},
                              files={"document": (Path(p).name, f, "application/pdf")})
        if r.status_code != 200:
            log(f"Telegram send failed for {p}: {r.text}")
            raise RuntimeError("Telegram send failed")

async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join([Path(f).name for f in files]))
    try: await send_via_telegram(files)
    except Exception as e: log(f"Telegram send error (continuing): {e}")

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e:
        traceback.print_exc(); sys.exit(1)
