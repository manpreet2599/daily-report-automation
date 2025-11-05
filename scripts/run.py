#!/usr/bin/env python3
import os, sys, asyncio, traceback, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── load .env ──────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

def today_str():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")

def log(msg): print(msg, flush=True)

# ── PDF download (bounded; popup fallback) ─────────────────────────────────────
async def click_and_wait_download(p, click_pdf, save_as_path, timeout_ms=25000):
    log("[pdf] attempting direct download…")
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
            log("[pdf] popup had no link; see popup_after_pdf_click.png")
            return False
        except Exception as e2:
            log(f"[pdf] popup fallback failed: {e2}")
            return False

# ── small helpers ──────────────────────────────────────────────────────────────
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

# ── robust dropdown selection (native or Bootstrap-select) ─────────────────────
async def _debug_dump_near_label(p, label_text: str, tag: str):
    try:
        html = await p.evaluate(
            """(labelText) => {
                const lab = Array.from(document.querySelectorAll('label'))
                  .find(l => (l.textContent||'').trim().toLowerCase().includes(labelText.toLowerCase()));
                if(!lab) return 'label not found';
                const wrap = lab.closest('div') || lab.parentElement || document.body;
                return wrap.outerHTML.slice(0, 5000);
            }""", label_text
        )
        (OUT / f"debug_near_{tag}.html").write_text(html, encoding="utf-8")
    except Exception: pass

async def select_native_by_label(p, label_text: str, option_text: str):
    # try native <select> tied to the label
    candidates = [
        f"label:has-text('{label_text}') + select",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::select[1]",
        f"text={label_text} >> xpath=following::select[1]",
    ]
    for sel in candidates:
        if await p.locator(sel).count():
            try:
                await p.select_option(sel, label=option_text); return True
            except Exception:
                try:
                    await p.select_option(sel, value=option_text); return True
                except Exception: pass
    return False

async def select_bootstrap_by_label(p, label_text: str, option_text: str):
    # click the bootstrap-select toggle then pick from .dropdown-menu.show
    toggle_candidates = [
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::*[contains(@class,'bootstrap-select')][1]//button[contains(@class,'dropdown-toggle')]",
        f"label:has-text('{label_text}') + * button.dropdown-toggle",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::button[contains(@class,'dropdown-toggle')][1]"
    ]
    for tsel in toggle_candidates:
        if await p.locator(tsel).count():
            try:
                btn = p.locator(tsel).first
                await btn.scroll_into_view_if_needed()
                await btn.click()
                # wait for open menu
                menu = p.locator(".dropdown-menu.show, .show .dropdown-menu").first
                await menu.wait_for(timeout=5000)

                # try exact-ish match inside the open menu
                opt = menu.locator("li, a, span, .text").filter(has_text=option_text).first
                if not await opt.count():
                    # retry with case-insensitive contains via JS
                    opt = menu.locator("li, a, span, .text").first
                    # attempt to scroll menu (sometimes long lists)
                    try:
                        for _ in range(10):
                            await menu.evaluate("(m)=>m.scrollBy(0,200)")
                    except Exception: pass
                    # fallback: click first visible item that contains fragment
                    opt = menu.locator("li, a, span, .text").filter(has_text=option_text).first

                await opt.scroll_into_view_if_needed()
                await opt.click(timeout=5000, force=True)
                # close menu if still open
                try: await p.keyboard.press("Escape")
                except Exception: pass
                return True
            except Exception:
                try: await p.keyboard.press("Escape")
                except Exception: pass
    return False

async def select_by_label_native_or_bootstrap(p, label_text: str, option_text: str, tag: str):
    log(f"[select] {label_text} → {option_text}")
    # try native first
    if await select_native_by_label(p, label_text, option_text):
        await p.screenshot(path=str(OUT / f"after_select_{tag}.png"), full_page=True)
        return True
    # then bootstrap
    ok = await select_bootstrap_by_label(p, label_text, option_text)
    await _debug_dump_near_label(p, label_text, tag)
    await p.screenshot(path=str(OUT / f"after_select_{tag}.png"), full_page=True)
    return ok

async def select_all_in_bootstrap_by_label(p, label_text: str, tag: str):
    log(f"[select] {label_text} → Select all")
    # open dropdown similarly
    toggle_candidates = [
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::*[contains(@class,'bootstrap-select')][1]//button[contains(@class,'dropdown-toggle')]",
        f"label:has-text('{label_text}') + * button.dropdown-toggle",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::button[contains(@class,'dropdown-toggle')][1]"
    ]
    for tsel in toggle_candidates:
        if await p.locator(tsel).count():
            try:
                btn = p.locator(tsel).first
                await btn.scroll_into_view_if_needed()
                await btn.click()
                menu = p.locator(".dropdown-menu.show, .show .dropdown-menu").first
                await menu.wait_for(timeout=5000)

                # common labels for select-all
                for txt in ["Select all", "Select All", "All selected", "All Selected", "Select all items", "Select All Items"]:
                    node = menu.locator("li, a, span, .text, button").filter(has_text=txt).first
                    if await node.count():
                        await node.scroll_into_view_if_needed()
                        await node.click(timeout=5000, force=True)
                        try: await p.keyboard.press("Escape")
                        except Exception: pass
                        await _debug_dump_near_label(p, label_text, tag)
                        await p.screenshot(path=str(OUT / f"after_select_{tag}.png"), full_page=True)
                        return True

                # fallback: click all visible list items (best-effort)
                try:
                    items = menu.locator("li a, li span, li .text").all()
                    for it in items:
                        try:
                            if await it.is_visible():
                                await it.click()
                        except Exception: pass
                    try: await p.keyboard.press("Escape")
                    except Exception: pass
                    await _debug_dump_near_label(p, label_text, tag)
                    await p.screenshot(path=str(OUT / f"after_select_{tag}.png"), full_page=True)
                    return True
                except Exception: pass
            except Exception: pass
    await _debug_dump_near_label(p, label_text, tag)
    await p.screenshot(path=str(OUT / f"after_select_{tag}.png"), full_page=True)
    return False

# ── main flow ──────────────────────────────────────────────────────────────────
async def site_login_and_download():
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    nameA       = os.getenv("REPORT_A_NAME", "ReportA")
    nameB       = os.getenv("REPORT_B_NAME", "ReportB")
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = today_str()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        context = await browser.new_context(accept_downloads=True)
        context.set_default_timeout(60000)
        context.set_default_navigation_timeout(180000)
        page = await context.new_page()

        # 1) Login screen
        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="networkidle"); break
            except PWTimeout as e: last_err = e
        if last_err: raise last_err
        await page.screenshot(path=str(OUT / "step1_login_page.png"), full_page=True)

        # 2) Select user type
        selected = False
        if user_type:
            log(f"Selecting user type: {user_type}")
            for sel in ["select#usertype", "select#userType", "select[name='userType']", "select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=user_type); selected = True; break
                    except Exception: pass
                    try:
                        await page.select_option(sel, label=user_type); selected = True; break
                    except Exception: pass
        log(f"Selected user type? {selected} (requested='{user_type}')")
        await page.screenshot(path=str(OUT / "after_user_type.png"), full_page=True)

        # 3) Credentials
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

        # 4) Login
        clicked = await click_first(page, [
            "button:has-text('Login')", "button:has-text('Sign in')",
            "button[type='submit']", "[role='button']:has-text('Login')"
        ], timeout=5000)
        log(f"Clicked login button? {clicked}")
        await page.wait_for_load_state("networkidle", timeout=120000)
        await page.screenshot(path=str(OUT / "step3_after_login.png"), full_page=True)
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        # 5) Report A: MIS Reports → Application Wise Report → filters → Show → PDF
        async def steps_A(p, save_path):
            log("[A] Opening 'MIS Reports'…")
            if not await click_first(p, [
                "nav >> text=MIS Reports", "text=MIS Reports", "a:has-text('MIS Reports')",
                "[role='menuitem']:has-text('MIS Reports')", "button:has-text('MIS Reports')",
                "li:has-text('MIS Reports')"
            ], timeout=3000):
                try:
                    await p.get_by_text("MIS Reports", exact=False).wait_for(timeout=9000)
                    await p.get_by_text("MIS Reports", exact=False).first.click()
                except Exception:
                    await p.screenshot(path=str(OUT / "fail_find_mis_reports.png"), full_page=True)
                    raise RuntimeError("[A] Could not click 'MIS Reports'")
            await p.wait_for_timeout(400)
            await p.screenshot(path=str(OUT / "after_open_mis.png"), full_page=True)

            log("[A] Clicking 'Application Wise Report'…")
            ok = await click_first(p, [
                "text=Application Wise Report", "a:has-text('Application Wise Report')",
                "[role='menuitem']:has-text('Application Wise Report')", "li:has-text('Application Wise Report')"
            ], timeout=6000)
            if not ok:
                await p.screenshot(path=str(OUT / "fail_open_app_wise.png"), full_page=True)
                raise RuntimeError("[A] Could not open 'Application Wise Report'")

            try:
                await p.wait_for_url(re.compile(r".*/Authorities/applicationwisereport\.jsp.*"), timeout=20000)
                log("[A] URL is applicationwisereport.jsp")
            except Exception:
                log("[A] URL unchanged; waiting for content…")
                try:
                    await p.get_by_text("Application Wise Report", exact=False).wait_for(timeout=12000)
                except Exception:
                    await p.screenshot(path=str(OUT / "fail_wait_app_wise.png"), full_page=True)
                    raise RuntimeError("[A] App Wise page not loaded in time")

            await p.wait_for_load_state("networkidle")
            await p.screenshot(path=str(OUT / "after_open_app_wise.png"), full_page=True)

            # Filters — robust selection
            log("[A] Selecting filters…")
            ok1 = await select_by_label_native_or_bootstrap(p, "Circle Office", "LUDHIANA CANAL CIRCLE", "circle")
            ok2 = await select_by_label_native_or_bootstrap(p, "Division Office", "FARIDKOT CANAL AND GROUND WATER DIVISION", "division")
            ok3 = await select_all_in_bootstrap_by_label(p, "Nature Of Application", "nature")
            ok4 = await select_by_label_native_or_bootstrap(p, "Status", "DELAYED", "status")

            if not all([ok1, ok2, ok3, ok4]):
                (OUT / "dropdown_warning.txt").write_text(
                    f"Circle:{ok1} Division:{ok2} NatureAll:{ok3} Status:{ok4}\n"
                    "One or more dropdowns not found/selected. Check labels/casing.",
                    encoding="utf-8")
                await p.screenshot(path=str(OUT / "fail_dropdowns.png"), full_page=True)
                raise RuntimeError("[A] Dropdown selection failed (see dropdown_warning.txt + debug_near_*.html)")

            await p.screenshot(path=str(OUT / "after_select_filters.png"), full_page=True)
            log("[A] Filters selected.")

            log("[A] Clicking 'Show Report'…")
            if not await click_first(p, [
                "button:has-text('Show Report')",
                "input[type='button'][value='Show Report']",
                "text=Show Report"
            ], timeout=8000):
                await p.screenshot(path=str(OUT / "fail_show_report.png"), full_page=True)
                raise RuntimeError("[A] Show Report button not found")

            log("[A] Waiting for report grid…")
            if not await wait_for_report_table(p, timeout_ms=20000):
                (OUT / "report_timeout.txt").write_text("Report grid did not appear in 20s.", encoding="utf-8")
                await p.screenshot(path=str(OUT / "fail_no_grid.png"), full_page=True)
                raise RuntimeError("[A] Report grid did not appear")
            await p.screenshot(path=str(OUT / "after_grid_shown.png"), full_page=True)
            log("[A] Report grid visible.")

            log("[A] Clicking PDF icon…")
            async def do_pdf_click():
                if not await click_first(p, [
                    "a[title*='PDF']", "button[title*='PDF']", "text=PDF",
                    "i.fa-file-pdf", "img[alt*='PDF']",
                    "button:has-text('Export PDF')", "button:has-text('Download PDF')"
                ], timeout=6000):
                    await p.locator("a[title*='PDF'], button[title*='PDF'], img[alt*='PDF']").first.click(timeout=6000)

            ok_dl = await click_and_wait_download(p, do_pdf_click, save_path, timeout_ms=25000)
            if not ok_dl:
                await p.screenshot(path=str(OUT / "fail_pdf_click.png"), full_page=True)
                raise RuntimeError("[A] Could not obtain PDF")

            log(f"[A] PDF saved → {save_path}")

        # A then B (reuse A for now)
        pathA = OUT / f"{nameA}_{stamp}.pdf"
        await steps_A(page, pathA); log(f"Saved {pathA.name}")

        pathB = OUT / f"{nameB}_{stamp}.pdf"
        await steps_A(page, pathB); log(f"Saved {pathB.name}")

        await context.close(); await browser.close()

    return [str(pathA), str(pathB)]

async def wait_for_report_table(p, timeout_ms=20000):
    try:
        await p.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
        return True
    except Exception:
        try:
            await p.get_by_text("No", exact=False).wait_for(timeout=2000)
            return True
        except Exception:
            return False

async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join([Path(f).name for f in files]))

    # Telegram delivery (if configured)
    try:
        await send_via_telegram(files)
    except Exception as e:
        log(f"Telegram send error (continuing): {e}")

# Telegram sender kept from your original
async def send_via_telegram(files):
    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        log("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping Telegram delivery.")
        return
    import requests
    for p in files:
        with open(p, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{bot}/sendDocument",
                data={"chat_id": chat},
                files={"document": (Path(p).name, f, "application/pdf")}
            )
        if r.status_code != 200:
            log(f"Telegram send failed for {p}: {r.text}")
            raise RuntimeError("Telegram send failed")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc(); sys.exit(1)
