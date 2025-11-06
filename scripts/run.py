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

def today_str():
    # dd-mm-yyyy
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d-%m-%Y")

def log(msg): print(msg, flush=True)

# ---------------- PDF download (bounded; popup fallback) ----------------
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
            await pop.screenshot(path=str(OUT / "popup_after_pdf_click.png"), full_page=True)
            await pop.close()
            return False
        except Exception as e2:
            log(f"[pdf] popup fallback failed: {e2}")
            return False

# ---------------- small helpers ----------------
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

# --- read displayed value near label (bootstrap/native) ---
async def read_display_value_near_label(p, label_text: str):
    """Returns visible text on bootstrap toggle or selected option on native <select> near a label."""
    try:
        txt = await p.evaluate(
            """(labelText) => {
                const norm = s => (s||'').trim().toLowerCase();
                const label = Array.from(document.querySelectorAll('label'))
                  .find(l => norm(l.textContent).includes(norm(labelText)));
                if (!label) return null;
                const root = label.closest('div') || label.parentElement || document.body;

                const btn = root.querySelector('.bootstrap-select .dropdown-toggle, button.dropdown-toggle');
                if (btn) {
                  const inner = btn.querySelector('.filter-option-inner-inner') || btn;
                  const t = (inner.textContent || '').trim();
                  if (t) return t;
                }
                const sel = root.querySelector('select');
                if (sel && sel.selectedIndex >= 0) {
                  const opt = sel.options[sel.selectedIndex];
                  if (opt) return (opt.textContent || '').trim();
                }
                return null;
            }""",
            label_text
        )
        return (txt or "").strip()
    except Exception:
        return ""

# --- select helpers (native / bootstrap) ---
async def _wait_option_on_select(p, select_css: str, option_text: str, timeout_ms=20000):
    end = asyncio.get_event_loop().time() + (timeout_ms/1000)
    while asyncio.get_event_loop().time() < end:
        try:
            exists = await p.locator(f"{select_css} >> option", has_text=option_text).count()
            if exists:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.1)
    return False

async def _native_select(p, label_text: str, id_candidates, name_candidates, option_text: str):
    # try label → select
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
    # try by id/name
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

# --- specific selectors for each dropdown ---
async def select_circle(p, option_text: str):
    log(f"[filter] Circle Office → {option_text}")
    ok, sel = await _native_select(
        p, "Circle Office",
        id_candidates=["circle", "circleId", "circleOffice", "circle_office"],
        name_candidates=["circle", "circleId", "circleOffice", "circle_office"],
        option_text=option_text
    )
    if ok:
        await p.screenshot(path=str(OUT / "after_select_circle.png"), full_page=True)
        (OUT / "selected_circle.txt").write_text(f"via native: {sel}", encoding="utf-8")
        return True
    ok = await _bootstrap_select(p, "Circle Office", option_text)
    await _dump_near(p, "Circle Office", "circle")
    await p.screenshot(path=str(OUT / "after_select_circle.png"), full_page=True)
    return ok

# ⚡ Fast, reactive Division selection (tight loops, no long waits)
async def select_division(p, option_text: str):
    log(f"[filter] Division Office → {option_text}")

    # Find native select near label
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
        # fallback: common ids/names
        for css in ["select#division","select#divisionId","select#divisionOffice",
                    "select[name='division']","select[name*='division']"]:
            if await p.locator(css).count():
                native_sel = css
                break

    if native_sel:
        # wait until enabled (max ~3s)
        for _ in range(30):
            try:
                disabled = await p.eval_on_selector(native_sel, "s => s.disabled")
                if not disabled:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)

        # wait until our option appears (max ~6s)
        for _ in range(60):
            try:
                if await p.locator(f"{native_sel} >> option", has_text=option_text).count():
                    try:
                        await p.select_option(native_sel, label=option_text)
                    except Exception:
                        await p.select_option(native_sel, value=option_text)
                    await p.screenshot(path=str(OUT / "after_select_division.png"), full_page=True)
                    (OUT / "selected_division.txt").write_text(f"via native: {native_sel}", encoding="utf-8")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.1)

    # Bootstrap-select fallback (fast)
    ok = await _bootstrap_select(p, "Division Office", option_text)
    await _dump_near(p, "Division Office", "division")
    await p.screenshot(path=str(OUT / "after_select_division.png"), full_page=True)
    return ok

async def select_nature_all(p):
    """
    Nature Of Application → Select all (robust).
    Finds the <select> tied to the label, selects all options, dispatches events.
    """
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

      let visible = '';
      const btn = root.querySelector('.bootstrap-select .dropdown-toggle, button.dropdown-toggle');
      if (btn) {
        const inner = btn.querySelector('.filter-option-inner-inner') || btn;
        visible = (inner.textContent || '').trim();
      } else if (sel && sel.selectedOptions && sel.selectedOptions.length) {
        visible = Array.from(sel.selectedOptions).map(o => (o.textContent||'').trim()).join(', ');
      }
      return { ok:true, visible };
    }
    """
    try:
        res = await p.evaluate(js, "Nature Of Application")
        (OUT / "seen_nature.txt").write_text((res.get("visible") or ""), encoding="utf-8")
        await p.screenshot(path=str(OUT / "after_select_nature.png"), full_page=True)
        return bool(res.get("ok"))
    except Exception as e:
        (OUT / "seen_nature.txt").write_text(f"error: {e}", encoding="utf-8")
        await p.screenshot(path=str(OUT / "fail_nature.png"), full_page=True)
        return False

# Status with extra robustness: try exact, then case-insensitive contains (JS)
async def select_status(p, option_text: str):
    log(f"[filter] Status → {option_text}")
    ok, sel = await _native_select(
        p, "Status",
        id_candidates=["status","statusId","appStatus","applicationStatus"],
        name_candidates=["status","statusId","appStatus","applicationStatus"],
        option_text=option_text
    )
    if ok:
        await p.screenshot(path=str(OUT / "after_select_status.png"), full_page=True)
        (OUT / "selected_status.txt").write_text(f"via native: {sel}", encoding="utf-8")
        return True

    # case-insensitive contains on label "Status"
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
        await p.screenshot(path=str(OUT / "after_select_status.png"), full_page=True)
        return bool(ok2)
    except Exception:
        pass

    ok = await _bootstrap_select(p, "Status", option_text)
    await _dump_near(p, "Status", "status")
    await p.screenshot(path=str(OUT / "after_select_status.png"), full_page=True)
    return ok

async def wait_for_report_table(p, timeout_ms=45000):
    try:
        await p.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
        return True
    except Exception:
        try:
            await p.get_by_text("No", exact=False).wait_for(timeout=2000)
            return True
        except Exception:
            return False

# ---------------- main flow ----------------
async def site_login_and_download():
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    # Default the names to what you want
    nameA       = os.getenv("REPORT_A_NAME", "Delayed Apps")
    nameB       = os.getenv("REPORT_B_NAME", "Pending Apps")
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = today_str()

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

        # 🚀 Speedup: block heavy resources (images, fonts, CSS)
        async def speed_filter(route, request):
            rtype = request.resource_type
            if rtype in ("image", "stylesheet", "font"):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        # timeouts
        context.set_default_timeout(45000)               # actions: 45s
        context.set_default_navigation_timeout(120000)   # nav: 2 min

        page = await context.new_page()

        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="networkidle"); break
            except PWTimeout as e: last_err = e
        if last_err: raise last_err

        await page.screenshot(path=str(OUT / "step1_login_page.png"), full_page=True)

        selected = False
        if user_type:
            log(f"Selecting user type: {user_type}")
            for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=user_type); selected=True; break
                    except Exception: pass
                    try:
                        await page.select_option(sel, label=user_type); selected=True; break
                    except Exception: pass
        log(f"Selected user type? {selected} (requested='{user_type}')")
        await page.screenshot(path=str(OUT / "after_user_type.png"), full_page=True)

        user_ok = await fill_first(page,
            ["input[name='username']", "#username", "input[name='login']", "#login",
             "input[name='userid']", "#userid", "#loginid", "input[name='loginid']",
             "input[placeholder*='Email']", "input[placeholder*='Mobile']", "input[placeholder*='Login']"],
            username)
        pass_ok = await fill_first(page,
            ["input[name='password']", "#password", "input[name='pwd']", "#pwd", "input[placeholder='Password']"],
            password)
        log(f"Filled username: {user_ok}, password: {pass_ok}")
        await page.screenshot(path=str(OUT / "step2_before_login.png"), full_page=True)

        clicked = await click_first(page,
            ["button:has-text('Login')","button:has-text('Sign in')",
             "button[type='submit']","[role='button']:has-text('Login')"],
            timeout=5000)
        log(f"Clicked login button? {clicked}")
        await page.wait_for_load_state("networkidle", timeout=120000)
        await page.screenshot(path=str(OUT / "step3_after_login.png"), full_page=True)
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        async def open_application_wise(p):
            log("[A] Opening 'MIS Reports'…")
            if not await click_first(p, [
                "nav >> text=MIS Reports","text=MIS Reports","a:has-text('MIS Reports')",
                "[role='menuitem']:has-text('MIS Reports')","button:has-text('MIS Reports')","li:has-text('MIS Reports')"
            ], timeout=3000):
                try:
                    await p.get_by_text("MIS Reports", exact=False).wait_for(timeout=9000)
                    await p.get_by_text("MIS Reports", exact=False).first.click()
                except Exception:
                    await p.screenshot(path=str(OUT / "fail_find_mis_reports.png"), full_page=True)
                    raise RuntimeError("[A] Could not click 'MIS Reports'")
            await p.wait_for_timeout(200)
            await p.screenshot(path=str(OUT / "after_open_mis.png"), full_page=True)

            log("[A] Clicking 'Application Wise Report'…")
            ok = await click_first(p, [
                "text=Application Wise Report","a:has-text('Application Wise Report')",
                "[role='menuitem']:has-text('Application Wise Report')","li:has-text('Application Wise Report')"
            ], timeout=6000)
            if not ok:
                await p.screenshot(path=str(OUT / "fail_open_app_wise.png"), full_page=True)
                raise RuntimeError("[A] Could not open 'Application Wise Report'")

            try:
                await p.wait_for_url(re.compile(r".*/Authorities/applicationwisereport\.jsp.*"), timeout=20000)
                log("[A] URL is applicationwisereport.jsp")
            except Exception:
                try:
                    await p.get_by_text("Application Wise Report", exact=False).wait_for(timeout=12000)
                except Exception:
                    await p.screenshot(path=str(OUT / "fail_wait_app_wise.png"), full_page=True)
                    raise RuntimeError("[A] App Wise page not loaded in time")

            await p.wait_for_load_state("networkidle")
            await p.screenshot(path=str(OUT / "after_open_app_wise.png"), full_page=True)

        async def apply_common_filters(p):
            # Circle / Division / Nature(all) — left as-is
            log("[A] Selecting filters…")
            ok1 = await select_circle(p, "LUDHIANA CANAL CIRCLE")
            await p.wait_for_timeout(250)
            ok2 = await select_division(p, "FARIDKOT CANAL AND GROUND WATER DIVISION")
            await p.wait_for_timeout(200)
            ok3 = await select_nature_all(p)
            await p.wait_for_timeout(150)
            (OUT / "dropdown_warning.txt").write_text(
                f"Circle:{ok1} Division:{ok2} NatureAll:{ok3}\n", encoding="utf-8"
            )

        async def set_status_and_download(p, status_text: str, save_path: Path):
            # Set Status (Delayed / Pending), Show Report, wait grid, download PDF
            ok4 = await select_status(p, status_text)
            await p.wait_for_timeout(150)
            status_seen = await read_display_value_near_label(p, "Status")
            (OUT / f"seen_status_{status_text.lower()}.txt").write_text(status_seen or "", encoding="utf-8")
            log(f"[A] Status set to '{status_text}' (ok={ok4}, seen='{status_seen}')")

            log("[A] Clicking 'Show Report'…")
            try:
                btn = p.locator("button:has-text('Show Report'), input[type='button'][value='Show Report']").first
                await btn.scroll_into_view_if_needed()
                await btn.wait_for({"state": "visible"})
            except Exception:
                pass
            if not await click_first(p, [
                "button:has-text('Show Report')",
                "input[type='button'][value='Show Report']",
                "text=Show Report"
            ], timeout=12000):
                await p.screenshot(path=str(OUT / "fail_show_report.png"), full_page=True)
                raise RuntimeError("[A] Show Report button not found/clickable")

            log("[A] Waiting for report grid (up to 45s)…")
            grid_ok = await wait_for_report_table(p, timeout_ms=45000)
            if not grid_ok:
                (OUT / "report_timeout.txt").write_text(f"Report grid did not appear for {status_text} in 45s.", encoding="utf-8")
                await p.screenshot(path=str(OUT / f"fail_no_grid_{status_text.lower()}.png"), full_page=True)
                try:
                    (OUT / f"snippet_after_show_{status_text.lower()}.html").write_text(await p.content(), encoding="utf-8")
                except Exception: pass
                raise RuntimeError(f"[A] Report grid did not appear ({status_text})")
            await p.screenshot(path=str(OUT / f"after_grid_shown_{status_text.lower()}.png"), full_page=True)
            log(f"[A] Report grid is visible ({status_text}).")

            # PDF
            log("[A] Looking for PDF control…")
            pdf_targets = [
                "a[title*='PDF']","button[title*='PDF']","button.buttons-pdf",
                "div.dt-buttons >> button:has-text('PDF')",
                "button:has-text('Export PDF')","button:has-text('Download PDF')",
                "i.fa-file-pdf","img[alt*='PDF']",
            ]
            found_any = any([await p.locator(sel).count() for sel in pdf_targets])
            if not found_any:
                await p.screenshot(path=str(OUT / f"fail_find_pdf_{status_text.lower()}.png"), full_page=True)
                raise RuntimeError(f"[A] No PDF control found on the page ({status_text})")

            async def do_pdf_click():
                try: await p.evaluate("window.scrollTo(0,0)")
                except Exception: pass
                for sel in pdf_targets:
                    try:
                        if await p.locator(sel).count():
                            el = p.locator(sel).first
                            await el.scroll_into_view_if_needed()
                            await el.click(timeout=8000); return
                    except Exception: continue
                try:
                    el = p.locator("div.dt-buttons button").first
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=8000)
                except Exception:
                    pass

            ok_dl = await click_and_wait_download(p, do_pdf_click, save_path, timeout_ms=35000)
            if not ok_dl:
                await p.screenshot(path=str(OUT / f"fail_pdf_click_{status_text.lower()}.png"), full_page=True)
                try:
                    html = await p.evaluate("""() => {
                        const tb = document.querySelector('.dt-buttons') || document.body;
                        return tb.outerHTML.slice(0, 8000);
                    }""")
                    (OUT / f"snippet_toolbar_html_{status_text.lower()}.html").write_text(html, encoding="utf-8")
                except Exception: pass
                raise RuntimeError(f"[A] Could not obtain PDF ({status_text})")
            log(f"[A] PDF saved → {save_path}")

        # === Flow ===
        # 1) Open the report page once
        await open_application_wise(page)

        # 2) Apply the shared filters once (Circle/Division/Nature)
        await apply_common_filters(page)

        # 3) Report A: Status = DELAYED → download
        pathA = OUT / f"{os.getenv('REPORT_A_NAME','Delayed Apps')} {stamp}.pdf"
        await set_status_and_download(page, "DELAYED", pathA)
        log(f"Saved {pathA.name}")

        # 4) Report B: toggle only Status to PENDING → download
        pathB = OUT / f"{os.getenv('REPORT_B_NAME','Pending Apps')} {stamp}.pdf"
        await set_status_and_download(page, "PENDING", pathB)
        log(f"Saved {pathB.name}")

        await context.close(); await browser.close()
    return [str(pathA), str(pathB)]

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
    try:
        await send_via_telegram(files)
    except Exception as e:
        log(f"Telegram send error (continuing): {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc(); sys.exit(1)
