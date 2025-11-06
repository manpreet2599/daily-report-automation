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

# ===================== PANEL FINDER =====================
def app_panel(page):
    # The unique wrapper that contains the report title + filters + buttons
    return page.locator("xpath=//div[.//text()[contains(.,'Application Wise Report')]]").first

# ===================== PANEL-SCOPED UTILITIES =====================

async def panel_click(panel, selectors, timeout=4000):
    for sel in selectors:
        try:
            loc = panel.locator(sel).first
            if await loc.count():
                await loc.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False

async def panel_fill(panel, selectors, value):
    for sel in selectors:
        try:
            loc = panel.locator(sel).first
            if await loc.count():
                await loc.fill(value)
                try:
                    await loc.evaluate("el => { el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }")
                except Exception: pass
                return True
        except Exception:
            pass
    return False

async def panel_input_value(panel, selectors) -> str:
    for sel in selectors:
        try:
            loc = panel.locator(sel).first
            if await loc.count():
                v = (await loc.input_value()).strip()
                if v: return v
        except Exception: pass
    return ""

async def panel_wait_data_rows(panel, timeout_ms=30000):
    end = asyncio.get_event_loop().time() + timeout_ms/1000.0
    try:
        await panel.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
    except Exception:
        pass
    while asyncio.get_event_loop().time() < end:
        try:
            has = await panel.evaluate("""
                (el) => {
                    const tbodies = Array.from(el.querySelectorAll('table tbody'));
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
            if has: return True
        except Exception: pass
        await asyncio.sleep(0.3)
    return False

async def panel_read_period(panel) -> str:
    try:
        for sel in [
            "xpath=.//*[contains(normalize-space(),'Period:')][1]",
            "xpath=.//*[contains(@id,'period')][1]",
            "xpath=.//div[contains(.,'Period:')][1]",
        ]:
            loc = panel.locator(sel).first
            if await loc.count():
                txt = (await loc.inner_text()).strip()
                if txt: return " ".join(txt.split())
    except Exception:
        pass
    return ""

def looks_like_period(text: str) -> bool:
    if not text or "period" not in text.lower(): return False
    dates = re.findall(r"\b(\d{2,4}[-/]\d{1,2}[-/]\d{2,4})\b", text)
    return len(dates) >= 2

# ===================== PANEL-SCOPED SELECT HELPERS =====================

async def panel_select_native(panel, label_text: str, option_text: str):
    # try select immediately following the label, then id/name fallbacks inside panel
    label_based = [
        f"xpath=.//label[contains(normalize-space(), '{label_text}')]/following::select[1]",
        f"css=label:has-text('{label_text}') + select"
    ]
    id_name = [
        "circle","circleId","circleOffice","circle_office",
        "division","divisionId","divisionOffice",
        "status","statusId","appStatus","applicationStatus"
    ]
    # label based
    for sel in label_based:
        sel_loc = panel.locator(sel).first
        if await sel_loc.count():
            try: await sel_loc.select_option(label=option_text); return True
            except Exception:
                try: await sel_loc.select_option(value=option_text); return True
                except Exception: pass
    # id/name inside panel
    for key in id_name:
        for css in (f"select#{key}", f"select[name='{key}']", f"select[name*='{key}']"):
            sel_loc = panel.locator(css).first
            if await sel_loc.count():
                try: await sel_loc.select_option(label=option_text); return True
                except Exception:
                    try: await sel_loc.select_option(value=option_text); return True
                    except Exception: pass
    return False

async def panel_select_bootstrap(panel, label_text: str, option_text: str):
    toggles = [
        f"xpath=.//label[contains(normalize-space(), '{label_text}')]/following::*[contains(@class,'bootstrap-select')][1]//button[contains(@class,'dropdown-toggle')]",
        f"xpath=.//button[contains(@class,'dropdown-toggle')][ancestor::div[label[contains(normalize-space(), '{label_text}')]]]"
    ]
    for tsel in toggles:
        btn = panel.locator(tsel).first
        if await btn.count():
            try:
                await btn.scroll_into_view_if_needed()
                await btn.click()
                menu = panel.locator(".dropdown-menu.show, .show .dropdown-menu").first
                await menu.wait_for(timeout=5000)
                for _ in range(40):
                    cand = menu.locator("li, a, span, .text").filter(has_text=option_text).first
                    if await cand.count():
                        await cand.scroll_into_view_if_needed()
                        await cand.click(timeout=5000, force=True)
                        try: await btn.press("Escape")
                        except Exception: pass
                        return True
                    try: await menu.evaluate("(m)=>m.scrollBy(0,250)")
                    except Exception: pass
                try: await btn.press("Escape")
                except Exception: pass
            except Exception:
                try: await btn.press("Escape")
                except Exception: pass
    return False

async def select_circle(panel, option_text: str):
    log(f"[filter] Circle Office → {option_text}")
    if await panel_select_native(panel, "Circle Office", option_text): return True
    return await panel_select_bootstrap(panel, "Circle Office", option_text)

async def select_division(panel, option_text: str):
    log(f"[filter] Division Office → {option_text}")
    if await panel_select_native(panel, "Division Office", option_text): return True
    return await panel_select_bootstrap(panel, "Division Office", option_text)

async def select_status(panel, option_text: str):
    log(f"[filter] Status → {option_text}")
    if await panel_select_native(panel, "Status", option_text): return True
    return await panel_select_bootstrap(panel, "Status", option_text)

async def select_nature_all(panel):
    log(f"[filter] Nature Of Application → Select all (force)")
    js = """
    (root) => {
      const norm = s => (s||'').trim().toLowerCase();
      const L = Array.from(root.querySelectorAll('label'))
        .find(l => norm(l.textContent).includes('nature of application'));
      if (!L) return { ok:false, reason:'label not found' };
      const box = L.closest('div') || root;
      let sel = box.querySelector('select');
      if (!sel) {
        const cands = Array.from(box.querySelectorAll('select'));
        sel = cands[0] || null;
      }
      if (!sel) return { ok:false, reason:'select not found' };
      let changed=false;
      for (const o of sel.options) { if (!o.selected){ o.selected=true; changed=true; } }
      if (changed) {
        sel.dispatchEvent(new Event('input',{bubbles:true}));
        sel.dispatchEvent(new Event('change',{bubbles:true}));
      }
      return { ok:true };
    }
    """
    try:
        res = await panel.evaluate(js)
        return bool(res and res.get("ok"))
    except Exception:
        return False

# ===================== SHOW REPORT (PANEL-SCOPED) =====================

async def show_report_and_wait(page, *, settle_ms=5600, network_timeout=30000):
    panel = app_panel(page)
    if not await panel.count():
        raise RuntimeError("Application Wise Report panel not found")

    # Click Show Report inside panel
    clicked = False
    try:
        clicked = await panel_click(panel, [
            "button:has-text('Show Report')",
            "input[type='button'][value='Show Report']",
            "input[type='submit'][value='Show Report']",
        ], timeout=8000)
        if not clicked:
            clicked = await panel.evaluate("""
            (root)=>{
              const norm = s => (s||'').trim().toLowerCase();
              const btns = Array.from(root.querySelectorAll('button,input[type=button],input[type=submit]'));
              const b = btns.find(x => {
                const t = norm(x.innerText||x.value||'');
                return t === 'show report' || t.includes('show report');
              });
              if (!b) return false;
              b.click();
              return true;
            }
            """)
    except Exception:
        clicked = False
    if not clicked:
        raise RuntimeError("Could not click Show Report")

    # Try to catch a report-like response (best-effort)
    try:
        await page.wait_for_response(
            lambda r: any(k in (r.url or '').lower() for k in [
                "applicationwisereport","appwisereport","getreport","reportdata","report"
            ]) and r.ok,
            timeout=network_timeout
        )
    except Exception:
        pass

    # Give it time to render (your requested delay)
    await asyncio.sleep(settle_ms/1000)

    # Panel-scoped checks
    period = await panel_read_period(panel)
    if looks_like_period(period):
        return True

    if await panel_wait_data_rows(panel, timeout_ms=12000):
        return True

    # Retry once (short)
    await asyncio.sleep(1.5)
    try:
        await panel_click(panel, [
            "button:has-text('Show Report')",
            "input[type='button'][value='Show Report']",
            "input[type='submit'][value='Show Report']",
        ], timeout=6000)
    except Exception:
        pass
    await asyncio.sleep(2.0)
    period = await panel_read_period(panel)
    if looks_like_period(period): return True
    if await panel_wait_data_rows(panel, timeout_ms=6000): return True
    return False

# ===================== PDF (PANEL-SCOPED) =====================

async def click_report_pdf_icon(page):
    panel = app_panel(page)
    if not await panel.count(): return False
    for sel in [
        "xpath=.//img[contains(@src,'pdf') or contains(@alt,'PDF')]",
        "xpath=(.//img[contains(@src,'pdf') or contains(@alt,'PDF')])[1]",
        "xpath=(.//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]:
        ico = panel.locator(sel).first
        if await ico.count():
            await ico.scroll_into_view_if_needed()
            await ico.click(timeout=6000, force=True)
            return True
    return False

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

async def download_report_pdf_with_checks(page, save_path: Path, settle_ms: int = 5600, min_bytes: int = 7000):
    panel = app_panel(page)
    try:
        has_rows = await panel_wait_data_rows(panel, timeout_ms=1000)
    except Exception:
        has_rows = False
    if not has_rows:
        log("[pdf] Warning: no data rows detected just before download; grace 1.5s.")
        await asyncio.sleep(1.5)

    if settle_ms > 0:
        await asyncio.sleep(settle_ms / 1000)

    async def do_pdf_click():
        clicked = await click_report_pdf_icon(page)
        if not clicked:
            raise RuntimeError("Red PDF icon not found in panel")

    ok_dl = await click_and_wait_download(page, do_pdf_click, save_path, timeout_ms=35000)
    if not ok_dl: return False

    try:
        size = Path(save_path).stat().st_size
        log(f"[pdf] size: {size} bytes")
        if size < min_bytes:
            log("[pdf] File looks too small; retry once after 3s…")
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

# ===================== MAIN FLOW =====================

async def site_login_and_download():
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = today_str()

    captured_from = ""
    captured_to   = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-dev-shm-usage","--disable-extensions",
                "--disable-background-networking","--disable-background-timer-throttling",
                "--disable-breakpad","--disable-client-side-phishing-detection",
                "--disable-default-apps","--disable-hang-monitor",
                "--disable-ipc-flooding-protection","--disable-popup-blocking",
                "--disable-prompt-on-repost","--metrics-recording-only","--no-first-run",
                "--safebrowsing-disable-auto-update"
            ]
        )
        context = await browser.new_context(accept_downloads=True)

        # Only block fonts; keep CSS/images so widgets behave
        async def speed_filter(route, request):
            if request.resource_type in ("font",):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(90000)
        page = await context.new_page()

        # ---- Login ----
        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="domcontentloaded"); break
            except PWTimeout as e: last_err = e
        if last_err: raise last_err

        if user_type:
            for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
                if await page.locator(sel).count():
                    try: await page.select_option(sel, value=user_type); break
                    except Exception:
                        try: await page.select_option(sel, label=user_type); break
                        except Exception: pass

        await panel_fill(page, ["#username","input[name='username']","input[placeholder*='Login']","input[placeholder*='Email']"], username)
        await panel_fill(page, ["#password","input[name='password']","input[name='pwd']"], password)
        await panel_click(page, ["button:has-text('Login')","button[type='submit']","[role='button']:has-text('Login')"], timeout=5000)

        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        # ---- Navigate to Application Wise Report ----
        async def open_application_wise(p):
            log("[A] Opening 'MIS Reports'…")
            ok = await panel_click(p, [
                "nav >> text=MIS Reports","text=MIS Reports","a:has-text('MIS Reports')",
                "[role='menuitem']:has-text('MIS Reports')","button:has-text('MIS Reports')","li:has-text('MIS Reports')"
            ], timeout=3000)
            if not ok:
                await p.get_by_text("MIS Reports", exact=False).first.click()
            await p.wait_for_timeout(150)

            log("[A] Clicking 'Application Wise Report'…")
            ok = await panel_click(p, [
                "text=Application Wise Report","a:has-text('Application Wise Report')",
                "[role='menuitem']:has-text('Application Wise Report')","li:has-text('Application Wise Report')"
            ], timeout=6000)
            if not ok:
                raise RuntimeError("[A] Could not open 'Application Wise Report'")

            try:
                await p.wait_for_url(re.compile(r".*/Authorities/applicationwisereport\.jsp.*"), timeout=20000)
            except Exception:
                await p.get_by_text("Application Wise Report", exact=False).wait_for(timeout=12000)

            await p.wait_for_load_state("domcontentloaded")

        async def capture_panel_defaults(p):
            panel_el = app_panel(p)
            if not await panel_el.count():
                raise RuntimeError("Application Wise Report panel not found")
            # Capture default dates **inside** the panel
            nonlocal captured_from, captured_to
            captured_from = await panel_input_value(panel_el, [
                "#fromDate","input#fromDate","input[name='fromDate']","input[name*='fromdate' i]","input[placeholder*='From' i]"
            ])
            captured_to = await panel_input_value(panel_el, [
                "#toDate","input#toDate","input[name='toDate']","input[name*='todate' i]","input[placeholder*='To' i]"
            ])
            log(f"[dates] captured defaults: From='{captured_from}' To='{captured_to}'")

        async def apply_filters_and_download(p, status_text: str, save_path: Path):
            panel_el = app_panel(p)
            if not await panel_el.count():
                raise RuntimeError("Application Wise Report panel not found")

            # Selects are now strictly panel-scoped
            await select_circle(panel_el, "LUDHIANA CANAL CIRCLE")
            await select_division(panel_el, "FARIDKOT CANAL AND GROUND WATER DIVISION")
            await select_nature_all(panel_el)
            await select_status(panel_el, status_text)
            log(f"[A] Status set to '{status_text}'")

            # Ensure dates are present inside panel
            frm = await panel_input_value(panel_el, ["#fromDate","input[name='fromDate']","input[name*='fromdate' i]","input[placeholder*='From' i]"])
            to  = await panel_input_value(panel_el, ["#toDate","input[name='toDate']","input[name*='todate' i]","input[placeholder*='To' i]"])
            if (not frm and captured_from): await panel_fill(panel_el, ["#fromDate","input[name='fromDate']","input[name*='fromdate' i]","input[placeholder*='From' i]"], captured_from)
            if (not to  and captured_to  ): await panel_fill(panel_el, ["#toDate","input[name='toDate']","input[name*='todate' i]","input[placeholder*='To' i]"], captured_to)
            frm2 = await panel_input_value(panel_el, ["#fromDate","input[name='fromDate']","input[name*='fromdate' i]","input[placeholder*='From' i]"])
            to2  = await panel_input_value(panel_el, ["#toDate","input[name='toDate']","input[name*='todate' i]","input[placeholder*='To' i]"])
            log(f"[dates] before Show Report (panel): From='{frm2}' To='{to2}'")

            ok_show = await show_report_and_wait(p, settle_ms=5600)
            if not ok_show:
                log("[A] Show Report did not render; retry with short settle.")
                ok_show = await show_report_and_wait(p, settle_ms=3000)

            # Proceed if we have either Period w/ dates or real rows
            period = await panel_read_period(panel_el)
            if looks_like_period(period):
                log(f"[period] {period}")
            else:
                # check rows to avoid empty export
                has_rows = await panel_wait_data_rows(panel_el, timeout_ms=3000)
                if not has_rows:
                    raise RuntimeError("[A] No data rows detected in panel after Show Report; aborting export.")

            await snap(p, f"after_grid_shown_{status_text.lower()}.png")

            ok_dl = await download_report_pdf_with_checks(p, save_path=save_path, settle_ms=5600, min_bytes=7000)
            if not ok_dl:
                raise RuntimeError(f"[A] Could not obtain a valid PDF ({status_text})")
            log(f"[A] PDF saved → {save_path}")

        # === Flow ===
        await open_application_wise(page)
        await capture_panel_defaults(page)

        pathA = OUT / f"Delayed Apps {stamp}.pdf"
        await apply_filters_and_download(page, "DELAYED", pathA)
        log(f"Saved {pathA.name}")

        pathB = OUT / f"Pending Apps {stamp}.pdf"
        await apply_filters_and_download(page, "PENDING", pathB)
        log(f"Saved {pathB.name}")

        await context.close(); await browser.close()
    return [str(pathA), str(pathB)]

# ===================== TELEGRAM =====================

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

# ===================== ENTRY =====================

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
        traceback.print_exc(); sys.exit(1)
