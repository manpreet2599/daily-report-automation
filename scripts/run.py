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

# ===================== DATE INPUT HELPERS =====================

FROM_DATE_CANDIDATES = [
    "#fromDate", "input#fromDate", "input[name='fromDate']",
    "input[name*='fromdate' i]", "input[placeholder*='From' i]",
    "xpath=//label[contains(normalize-space(),'From Date')]/following::input[1]"
]
TO_DATE_CANDIDATES = [
    "#toDate", "input#toDate", "input[name='toDate']",
    "input[name*='todate' i]", "input[placeholder*='To' i]",
    "xpath=//label[contains(normalize-space(),'To Date')]/following::input[1]"
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
                # fire events if the site listens
                try:
                    await p.eval_on_selector(sel, "el => { el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }")
                except Exception: pass
                return True
        except Exception: pass
    return False

# ===================== TABLE / DATA READY =====================

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
    try:
        await page.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
    except Exception:
        pass
    while asyncio.get_event_loop().time() < end:
        if await _has_data_rows(page): return True
        await asyncio.sleep(0.3)
    return False

# ===================== DOWNLOAD HELPERS =====================

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

# ===================== GENERIC UI HELPERS =====================

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

# ===================== SELECT HELPERS (native + bootstrap) =====================

def _resp_matcher(substrings):
    low = [s.lower() for s in substrings]
    def _inner(resp):
        try:
            url = resp.url.lower()
            return any(s in url for s in low)
        except Exception:
            return False
    return _inner

async def wait_for_any_response(p, substrings, timeout_ms=10000):
    try:
        await p.wait_for_response(_resp_matcher(substrings), timeout=timeout_ms)
        return True
    except Exception:
        return False

async def get_options_count(p, select_css: str):
    try:
        return await p.eval_on_selector(select_css, "s => s ? s.options.length : 0")
    except Exception:
        return 0

async def wait_options_increase(p, select_css: str, min_count=2, timeout_ms=8000):
    end = asyncio.get_event_loop().time() + timeout_ms/1000.0
    while asyncio.get_event_loop().time() < end:
        cnt = await get_options_count(p, select_css)
        if cnt >= min_count: return True
        await asyncio.sleep(0.1)
    return False

async def _native_select(p, label_text: str, id_candidates, name_candidates, option_text: str):
    label_based = [
        f"label:has-text('{label_text}') + select",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::select[1]",
        f"text={label_text} >> xpath=following::select[1]",
    ]
    for sel in label_based:
        if await p.locator(sel).count():
            try:
                await p.select_option(sel, label=option_text); return True, sel
            except Exception:
                try:
                    await p.select_option(sel, value=option_text); return True, sel
                except Exception: pass
    for key in id_candidates:
        sel = f"select#{key}"
        if await p.locator(sel).count():
            try:
                await p.select_option(sel, label=option_text); return True, sel
            except Exception:
                try:
                    await p.select_option(sel, value=option_text); return True, sel
                except Exception: pass
    for key in name_candidates:
        sel = f"select[name='{key}'], select[name*='{key}']"
        if await p.locator(sel).count():
            try:
                await p.select_option(sel, label=option_text); return True, sel
            except Exception:
                try:
                    await p.select_option(sel, value=option_text); return True, sel
                except Exception: pass
    return False, None

async def _bootstrap_select(p, label_text: str, option_text: str):
    toggle_candidates = [
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::*[contains(@class,'bootstrap-select')][1]//button[contains(@class,'dropdown-toggle')]",
        f"label:has-text('{label_text}') + * button.dropdown-toggle",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::button[contains(@class,'dropdown-toggle')][1]",
    ]
    for tsel in toggle_candidates:
        if await p.locator(tsel).count():
            try:
                btn = p.locator(tsel).first
                await btn.scroll_into_view_if_needed()
                await btn.click()
                menu = p.locator(".dropdown-menu.show, .show .dropdown-menu").first
                await menu.wait_for(timeout=5000)

                found = False
                for _ in range(20):
                    candidate = menu.locator("li, a, span, .text").filter(has_text=option_text).first
                    if await candidate.count():
                        await candidate.scroll_into_view_if_needed()
                        await candidate.click(timeout=5000, force=True)
                        found = True
                        break
                    try: await menu.evaluate("(m)=>m.scrollBy(0,250)")
                    except Exception: pass

                try: await p.keyboard.press("Escape")
                except Exception: pass
                if found: return True
            except Exception:
                try: await p.keyboard.press("Escape")
                except Exception: pass
    return False

async def select_circle(p, option_text: str):
    log(f"[filter] Circle Office → {option_text}")
    ok, sel = await _native_select(
        p, "Circle Office",
        id_candidates=["circle", "circleId", "circleOffice", "circle_office"],
        name_candidates=["circle", "circleId", "circleOffice", "circle_office"],
        option_text=option_text
    )
    if ok:
        await snap(p, "after_select_circle.png")
        return True
    ok = await _bootstrap_select(p, "Circle Office", option_text)
    await _dump_near(p, "Circle Office", "circle")
    await snap(p, "after_select_circle.png")
    return ok

async def select_division(p, option_text: str):
    log(f"[filter] Division Office → {option_text}")
    label_selects = [
        "label:has-text('Division Office') + select",
        "xpath=//label[contains(normalize-space(),'Division Office')]/following::select[1]"
    ]
    native_sel = None
    for css in label_selects:
        if await p.locator(css).count():
            native_sel = css
            break
    if not native_sel:
        for css in ["select#division","select#divisionId","select#divisionOffice",
                    "select[name='division']","select[name*='division']"]:
            if await p.locator(css).count():
                native_sel = css
                break

    if native_sel:
        wait_resp = wait_for_any_response(
            p,
            substrings=["division", "getDivision", "bycircle", "divisionList", "getdivisions"],
            timeout_ms=7000
        )
        wait_opts = wait_options_increase(p, native_sel, min_count=2, timeout_ms=7000)
        await asyncio.gather(wait_resp, wait_opts)

        for _ in range(50):
            try:
                if await p.locator(f"{native_sel} >> option", has_text=option_text).count():
                    try:
                        await p.select_option(native_sel, label=option_text)
                    except Exception:
                        await p.select_option(native_sel, value=option_text)
                    await snap(p, "after_select_division.png")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.1)

    ok = await _bootstrap_select(p, "Division Office", option_text)
    await _dump_near(p, "Division Office", "division")
    await snap(p, "after_select_division.png")
    return ok

async def select_nature_all(p):
    log(f"[filter] Nature Of Application → Select all (force)")
    js = """
    (labelText) => {
      const norm = s => (s||'').trim().toLowerCase();
      const L = Array.from(document.querySelectorAll('label'))
        .find(l => norm(l.textContent).includes(norm(labelText)));
      if (!L) return { ok:false, reason:'label not found' };
      const root = L.closest('div') || L.parentElement || document.body;
      let sel = root.querySelector('select');
      if (!sel) {
        sel = (L.nextElementSibling && L.nextElementSibling.matches('select')) ? L.nextElementSibling : null;
        if (!sel) {
          const candidates = Array.from(root.querySelectorAll('select'));
          sel = candidates.length ? candidates[0] : null;
        }
      }
      if (!sel) return { ok:false, reason:'select not found' };
      let changed = false;
      for (const o of sel.options) {
        if (!o.selected) { o.selected = true; changed = true; }
      }
      if (changed) {
        sel.dispatchEvent(new Event('input',  { bubbles:true }));
        sel.dispatchEvent(new Event('change', { bubbles:true }));
      }
      return { ok:true };
    }
    """
    try:
        res = await p.evaluate(js, "Nature Of Application")
        await snap(p, "after_select_nature.png")
        return bool(res.get("ok"))
    except Exception:
        await snap(p, "fail_nature.png")
        return False

async def select_status(p, option_text: str):
    log(f"[filter] Status → {option_text}")
    ok, sel = await _native_select(
        p, "Status",
        id_candidates=["status","statusId","appStatus","applicationStatus"],
        name_candidates=["status","statusId","appStatus","applicationStatus"],
        option_text=option_text
    )
    if ok:
        await snap(p, "after_select_status.png")
        return True

    js = """
    (wanted) => {
      const norm = s => (s||'').trim().toLowerCase();
      const L = Array.from(document.querySelectorAll('label'))
        .find(l => norm(l.textContent).includes('status'));
      if (!L) return false;
      const root = L.closest('div') || L.parentElement || document.body;
      const sel = root.querySelector('select');
      if (!sel) return false;
      const w = norm(wanted);
      let idx = -1;
      for (let i=0;i<sel.options.length;i++){
        const txt = norm(sel.options[i].textContent);
        if (txt.includes(w)) { idx = i; break; }
      }
      if (idx === -1) return false;
      sel.selectedIndex = idx;
      sel.dispatchEvent(new Event('input', {bubbles:true}));
      sel.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    }
    """
    try:
        ok2 = await p.evaluate(js, option_text)
        await snap(p, "after_select_status.png")
        return bool(ok2)
    except Exception:
        pass

    ok = await _bootstrap_select(p, "Status", option_text)
    await _dump_near(p, "Status", "status")
    await snap(p, "after_select_status.png")
    return ok

async def wait_for_report_table(p, timeout_ms=30000):
    try:
        await p.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
        return True
    except Exception:
        try:
            await p.get_by_text("No", exact=False).wait_for(timeout=2000)
            return True
        except Exception:
            return False

# ===================== PERIOD DETECTION + SHOW REPORT =====================

async def _read_period_text(page) -> str:
    try:
        sel_candidates = [
            "xpath=//*[contains(normalize-space(),'Period:')][1]",
            "xpath=//*[contains(@id,'period')][1]",
            "xpath=//div[contains(.,'Period:')][1]",
        ]
        for s in sel_candidates:
            if await page.locator(s).count():
                txt = (await page.locator(s).first.inner_text()).strip()
                if txt:
                    return " ".join(txt.split())
    except Exception:
        pass
    return ""

def _looks_like_dated_period(text: str) -> bool:
    if not text: return False
    if "period" not in text.lower(): return False
    # capture date-ish tokens like 2024-07-26, 26/07/2024, 26-07-2024
    dates = re.findall(r"\b(\d{2,4}[-/]\d{1,2}[-/]\d{2,4})\b", text)
    return len(dates) >= 2

async def show_report_and_wait(page, *, click_timeout=8000, network_timeout=30000):
    # Try normal click first
    clicked = await click_first(page, [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "input[type='submit'][value='Show Report']",
        "text=Show Report"
    ], timeout=click_timeout)

    # JS fallback: force the bound handler
    if not clicked:
        try:
            clicked = await page.evaluate("""
            () => {
              const norm = s => (s||'').trim().toLowerCase();
              const btns = Array.from(document.querySelectorAll('button,input[type=button],input[type=submit]'));
              const el = btns.find(b => norm(b.innerText||b.value||'') === 'show report' ||
                                        norm(b.innerText||b.value||'').includes('show report'));
              if (!el) return false;
              el.click();
              return true;
            }
            """)
        except Exception:
            clicked = False
    if not clicked:
        raise RuntimeError("Could not click Show Report")

    # Wait for a plausible report response
    try:
        await page.wait_for_response(
            lambda r: any(k in (r.url or '').lower() for k in [
                "applicationwisereport", "appwisereport", "getreport", "reportdata", "report"
            ]) and r.ok,
            timeout=network_timeout
        )
    except Exception:
        pass  # continue to DOM checks

    # Require either a proper Period line with two dates OR data rows
    for _ in range(60):  # ~18s
        period = await _read_period_text(page)
        if _looks_like_dated_period(period):
            return True
        if await _has_data_rows(page):
            return True
        await asyncio.sleep(0.3)
    return False

# ===================== PDF CONTROLS =====================

async def click_report_pdf_icon(page):
    candidates = [
        "xpath=//div[contains(.,'Application Wise Report')]//img[contains(@src,'pdf') or contains(@alt,'PDF')]",
        "xpath=(//img[contains(@src,'pdf') or contains(@alt,'PDF')])[1]",
        "xpath=(//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]
    for sel in candidates:
        if await page.locator(sel).count():
            ico = page.locator(sel).first
            await ico.scroll_into_view_if_needed()
            await ico.click(timeout=6000, force=True)
            return True
    return False

async def download_report_pdf_with_checks(page, save_path: Path, settle_ms: int = 5600, min_bytes: int = 7000):
    if not await _has_data_rows(page):
        log("[pdf] Warning: no data rows detected just before download; giving 1.5s grace.")
        await asyncio.sleep(1.5)
    if settle_ms > 0: await asyncio.sleep(settle_ms / 1000)

    async def do_pdf_click():
        clicked = await click_report_pdf_icon(page)
        if not clicked:
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

        # keep css/images to avoid breaking date widgets; only block fonts
        async def speed_filter(route, request):
            rtype = request.resource_type
            if rtype in ("font",):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        context.set_default_timeout(30000)
        context.set_default_navigation_timeout(90000)

        page = await context.new_page()

        # Guard: auto-restore dates if site JS clears them
        await page.add_init_script("""
        (function(){
          window.__keepDates = {from:'', to:''};
          const setBack = () => {
            const f = document.querySelector('#fromDate, input[name="fromDate"], input[name*="fromdate" i], input[placeholder*="From" i]');
            const t = document.querySelector('#toDate, input[name="toDate"], input[name*="todate" i], input[placeholder*="To" i]');
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
          document.addEventListener('DOMContentLoaded', setBack);
          mo.observe(document.documentElement, {subtree:true, childList:true, attributes:true});
          setInterval(setBack, 500);
        })();
        """)

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
        await snap(page, "after_user_type.png")

        await fill_first(page,
            ["input[name='username']", "#username", "input[name='login']", "#login",
             "input[name='userid']", "#userid", "#loginid", "input[name='loginid']",
             "input[placeholder*='Email']", "input[placeholder*='Mobile']", "input[placeholder*='Login']"],
            username)
        await fill_first(page,
            ["input[name='password']", "#password", "input[name='pwd']", "#pwd", "input[placeholder='Password']"],
            password)
        await snap(page, "step2_before_login.png")

        await click_first(page,
            ["button:has-text('Login')","button:has-text('Sign in')",
             "button[type='submit']","[role='button']:has-text('Login')"], timeout=5000)

        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        await snap(page, "step3_after_login.png")
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        # ---- Navigate to Application Wise Report ----
        async def open_application_wise(p):
            nonlocal captured_from, captured_to

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
            if not ok:
                await snap(p, "fail_open_app_wise.png")
                raise RuntimeError("[A] Could not open 'Application Wise Report'")

            try:
                await p.wait_for_url(re.compile(r".*/Authorities/applicationwisereport\.jsp.*"), timeout=20000)
            except Exception:
                await p.get_by_text("Application Wise Report", exact=False).wait_for(timeout=12000)

            await p.wait_for_load_state("domcontentloaded")
            await snap(p, "after_open_app_wise.png")

            # Capture default dates precisely
            captured_from = await get_first_input_value(p, FROM_DATE_CANDIDATES)
            captured_to   = await get_first_input_value(p, TO_DATE_CANDIDATES)
            log(f"[dates] captured defaults: From='{captured_from}' To='{captured_to}'")

            # Feed guard so any clears are auto-restored
            try:
                await p.evaluate("(f,t)=>{window.__keepDates.from=f; window.__keepDates.to=t;}", captured_from, captured_to)
            except Exception:
                pass

        async def apply_common_filters(p):
            # Circle
            ok1 = await select_circle(p, "LUDHIANA CANAL CIRCLE")
            # Division
            ok2 = await select_division(p, "FARIDKOT CANAL AND GROUND WATER DIVISION")
            # Nature(all)
            ok3 = await select_nature_all(p)
            (OUT / "dropdown_warning.txt").write_text(
                f"Circle:{ok1} Division:{ok2} NatureAll:{ok3}\n", encoding="utf-8"
            )

        async def set_status_and_download(p, status_text: str, save_path: Path):
            ok4 = await select_status(p, status_text)
            log(f"[A] Status set to '{status_text}' (ok={ok4})")

            # Ensure dates present before Show Report
            cur_from = await get_first_input_value(p, FROM_DATE_CANDIDATES)
            cur_to   = await get_first_input_value(p, TO_DATE_CANDIDATES)
            if not cur_from and captured_from:
                await set_first_input_value(p, FROM_DATE_CANDIDATES, captured_from)
            if not cur_to and captured_to:
                await set_first_input_value(p, TO_DATE_CANDIDATES, captured_to)
            log(f"[dates] before Show Report: From='{await get_first_input_value(p, FROM_DATE_CANDIDATES)}' To='{await get_first_input_value(p, TO_DATE_CANDIDATES)}'")

            # Must successfully re-run Show Report and see a dated Period OR data rows
            ok_show = await show_report_and_wait(p)
            if not ok_show:
                if captured_from and captured_to:
                    await set_first_input_value(p, FROM_DATE_CANDIDATES, captured_from)
                    await set_first_input_value(p, TO_DATE_CANDIDATES, captured_to)
                    log("[A] Re-applying dates and retrying Show Report once…")
                    ok_show = await show_report_and_wait(p)
            if not ok_show:
                period_dbg = await _read_period_text(p)
                raise RuntimeError(f"[A] Show Report did not produce data. Period text seen: '{period_dbg}'")

            # Refuse to export if Period still lacks dates (prevents header-only PDFs)
            period_now = await _read_period_text(p)
            if not _looks_like_dated_period(period_now):
                raise RuntimeError(f"[A] Period line lacks dates after Show Report: '{period_now}'")

            await snap(p, f"after_grid_shown_{status_text.lower()}.png")

            ok_dl = await download_report_pdf_with_checks(
                p,
                save_path=save_path,
                settle_ms=5600,
                min_bytes=7000
            )
            if not ok_dl:
                await snap(p, f"fail_pdf_click_{status_text.lower()}.png")
                raise RuntimeError(f"[A] Could not obtain a valid PDF ({status_text})")
            log(f"[A] PDF saved → {save_path}")

        # === Flow ===
        await open_application_wise(page)
        await apply_common_filters(page)

        pathA = OUT / f"Delayed Apps {stamp}.pdf"
        await set_status_and_download(page, "DELAYED", pathA)
        log(f"Saved {pathA.name}")

        pathB = OUT / f"Pending Apps {stamp}.pdf"
        await set_status_and_download(page, "PENDING", pathB)
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
