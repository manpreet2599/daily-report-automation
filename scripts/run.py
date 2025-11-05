#!/usr/bin/env python3
import os, sys, asyncio, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── load .env so USERNAME/PASSWORD/etc. are available ──────────────────────────
from dotenv import load_dotenv
load_dotenv()

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

def today_str():
    # Use IST date in filename
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")

def log(msg):
    print(msg, flush=True)

# ───────────────────────────────────────────────────────────────────────────────
# SAFE PDF DOWNLOAD (bounded; tries popup fallback)
# ───────────────────────────────────────────────────────────────────────────────
async def click_and_wait_download(p, click_pdf, save_as_path, timeout_ms=25000):
    """
    Click a control that should trigger a PDF download.
    If no download event fires, try a popup fallback (viewer window), and grab the PDF from there.
    """
    try:
        async with p.expect_download(timeout=timeout_ms) as dl_info:
            await click_pdf()
        dl = await dl_info.value
        await dl.save_as(save_as_path)
        return True
    except Exception as e:
        print(f"[warn] No direct download detected ({e}). Trying popup fallback...", flush=True)
        try:
            async with p.expect_popup(timeout=5000) as pop_info:
                await click_pdf()
            pop = await pop_info.value
            await pop.wait_for_load_state("load")

            # Try to find a direct PDF link in the popup
            link = pop.locator("a[href$='.pdf'], a[download], a[href*='application/pdf']").first
            if await link.count():
                async with pop.expect_download(timeout=timeout_ms) as dl_info2:
                    await link.click()
                dl2 = await dl_info2.value
                await dl2.save_as(save_as_path)
                await pop.close()
                return True

            # Last resort: screenshot the popup for debugging
            await pop.screenshot(path=str(OUT / "popup_after_pdf_click.png"), full_page=True)
            await pop.close()
            return False
        except Exception as e2:
            print(f"[err] Popup fallback also failed: {e2}", flush=True)
            return False

# ---------- generic helpers ----------
async def fill_first(page, candidates, value):
    for sel in candidates:
        try:
            if await page.locator(sel).count():
                await page.fill(sel, value)
                return True
        except Exception:
            pass
    return False

async def click_first(page, candidates, timeout=3000):
    for sel in candidates:
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            pass
    return False

# --- selects (native and Bootstrap-select fallback) ---
async def select_by_label_native_or_bootstrap(p, label_text: str, option_text: str):
    """
    Try selecting in a normal <select> by label/value.
    If not found, fallback to Bootstrap-select: click the toggle next to the label and click the option text.
    """
    label_text = label_text.strip()
    option_text = option_text.strip()

    # Native <select> attempts
    native_candidates = [
        f"label:has-text('{label_text}') + select",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::select[1]",
        f"text={label_text} >> xpath=following::select[1]",
    ]
    for sel in native_candidates:
        if await p.locator(sel).count():
            try:
                await p.select_option(sel, label=option_text)
                return True
            except Exception:
                try:
                    await p.select_option(sel, value=option_text)
                    return True
                except Exception:
                    pass

    # Bootstrap-select style fallback:
    # find the toggle button next to the label and pick the option in the open menu
    toggle_candidates = [
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::*[contains(@class,'bootstrap-select')][1]//button",
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::*[self::button or self::*[contains(@class,'dropdown-toggle')]][1]",
        f"label:has-text('{label_text}') + * button"
    ]
    for tsel in toggle_candidates:
        if await p.locator(tsel).count():
            try:
                await p.click(tsel)
                # Option can be inside a <ul role="listbox"> or similar
                opt = p.get_by_text(option_text, exact=False).first
                # restrict to visible and near the dropdown if needed
                await opt.click(timeout=5000)
                return True
            except Exception:
                # try closing and continue
                try:
                    await p.keyboard.press("Escape")
                except Exception:
                    pass
    return False

async def select_all_in_bootstrap_by_label(p, label_text: str):
    """Special case for 'Nature Of Application' -> 'Select all' in Bootstrap-select."""
    label_text = label_text.strip()
    toggle_candidates = [
        f"xpath=//label[contains(normalize-space(), '{label_text}')]/following::*[contains(@class,'bootstrap-select')][1]//button",
        f"label:has-text('{label_text}') + * button"
    ]
    for tsel in toggle_candidates:
        if await p.locator(tsel).count():
            try:
                await p.click(tsel)
                # try 'Select all' / 'Select All'
                for txt in ["Select all", "Select All", "Select All (", "All selected", "All Selected"]:
                    el = p.get_by_text(txt, exact=False).first
                    if await el.count():
                        await el.click(timeout=5000)
                        # close the dropdown
                        try:
                            await p.keyboard.press("Escape")
                        except Exception:
                            pass
                        return True
                # fallback: pick all visible list items
                try:
                    items = p.locator("ul li a, ul li span").filter(has_text="").all()
                    for it in items:
                        if await it.is_visible():
                            try:
                                await it.click()
                            except Exception:
                                pass
                    await p.keyboard.press("Escape")
                    return True
                except Exception:
                    pass
            except Exception:
                pass
    return False

async def wait_for_report_table(p, timeout_ms=20000):
    """Wait for either a results table/grid or a 'No records' message."""
    try:
        await p.wait_for_selector("table, .table, .dataTable, .ag-root, #reportGrid", timeout=timeout_ms, state="visible")
        return True
    except Exception:
        try:
            await p.get_by_text("No", exact=False).wait_for(timeout=2000)
            return True
        except Exception:
            return False

# ───────────────────────────────────────────────────────────────────────────────

async def site_login_and_download():
    # If LOGIN_URL not provided as a secret, default to the real e-Sinchai login
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    nameA       = os.getenv("REPORT_A_NAME", "ReportA")
    nameB       = os.getenv("REPORT_B_NAME", "ReportB")
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = today_str()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(accept_downloads=True)
        # Be more patient than defaults
        context.set_default_timeout(60000)                 # 60s for actions
        context.set_default_navigation_timeout(180000)     # 180s for page.goto
        page = await context.new_page()

        # 1) Open login page
        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="networkidle")
                break
            except PWTimeout as e:
                last_err = e
        if last_err:
            raise last_err

        # Screenshot: initial login page
        await page.screenshot(path=str(OUT / "step1_login_page.png"), full_page=True)

        # 2) Select User Type (native <select id="usertype"> on e-SInchai)
        selected = False
        if user_type:
            log(f"Selecting user type: {user_type}")
            for sel in ["select#usertype", "select#userType", "select[name='userType']", "select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=user_type)  # e.g., XEN
                        selected = True
                        break
                    except Exception:
                        pass
                    try:
                        await page.select_option(sel, label=user_type)
                        selected = True
                        break
                    except Exception:
                        pass
            if not selected:
                try:
                    opts = await page.eval_on_selector_all(
                        "select, select#usertype, select#userType",
                        "els => els.flatMap(s => Array.from(s.options).map(o => [o.value, o.textContent.trim()]))"
                    )
                    print("USER_TYPE options discovered:", opts, flush=True)
                except Exception:
                    pass
        log(f"Selected user type? {selected} (requested='{user_type}')")
        await page.screenshot(path=str(OUT / "after_user_type.png"), full_page=True)

        # 3) Fill username & password (be tolerant with ids/names)
        user_ok = await fill_first(
            page,
            [
                "input[name='username']",
                "#username",
                "input[name='login']",
                "#login",
                "input[name='userid']",
                "#userid",
                "#loginid",
                "input[name='loginid']",
                "input[placeholder*='Email']",
                "input[placeholder*='Mobile']",
                "input[placeholder*='Login']",
            ],
            username
        )
        pass_ok = await fill_first(
            page,
            ["input[name='password']", "#password", "input[name='pwd']", "#pwd", "input[placeholder='Password']"],
            password
        )
        log(f"Filled username: {user_ok}, password: {pass_ok}")
        await page.screenshot(path=str(OUT / "step2_before_login.png"), full_page=True)

        # 4) Click Login
        clicked = await click_first(page, [
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "button[type='submit']",
            "[role='button']:has-text('Login')"
        ], timeout=5000)
        log(f"Clicked login button? {clicked}")

        # Wait for navigation after login (dashboard)
        await page.wait_for_load_state("networkidle", timeout=120000)
        await page.screenshot(path=str(OUT / "step3_after_login.png"), full_page=True)
        log("Login step complete.")

        # ───────────────────────────────────────────────────────────────────
        # Report A: MIS Reports → Application Wise Report → filters → Show → PDF
        # ───────────────────────────────────────────────────────────────────
        async def steps_A(p, save_path):
            # 1) Open MIS Reports (left sidebar)
            opened = await click_first(p, [
                "nav >> text=MIS Reports",
                "text=MIS Reports",
                "a:has-text('MIS Reports')",
                "[role='menuitem']:has-text('MIS Reports')",
                "button:has-text('MIS Reports')",
                "li:has-text('MIS Reports')"
            ], timeout=5000)
            if not opened:
                try:
                    await p.get_by_text("MIS Reports", exact=False).hover()
                except Exception:
                    pass
            await p.wait_for_timeout(300)
            await p.screenshot(path=str(OUT / "after_open_mis.png"), full_page=True)

            # 2) Click Application Wise Report and wait for correct page
            with p.expect_navigation(url_regex=r".*/Authorities/applicationwisereport\.jsp.*", timeout=20000):
                ok = await click_first(p, [
                    "text=Application Wise Report",
                    "a:has-text('Application Wise Report')",
                    "[role='menuitem']:has-text('Application Wise Report')",
                    "li:has-text('Application Wise Report')",
                ], timeout=8000)
                if not ok:
                    await p.screenshot(path=str(OUT / "fail_open_app_wise.png"), full_page=True)
                    raise RuntimeError("Could not open 'Application Wise Report'")

            await p.wait_for_load_state("networkidle")
            await p.screenshot(path=str(OUT / "after_open_app_wise.png"), full_page=True)

            # 3) Select dropdowns in exact order
            ok1 = await select_by_label_native_or_bootstrap(p, "Circle Office", "LUDHIANA CANAL CIRCLE")
            ok2 = await select_by_label_native_or_bootstrap(p, "Division Office", "FARIDKOT CANAL AND GROUND WATER DIVISION")
            # Nature Of Application uses 'Select all'
            ok3 = await select_all_in_bootstrap_by_label(p, "Nature Of Application")
            ok4 = await select_by_label_native_or_bootstrap(p, "Status", "DELAYED")

            if not all([ok1, ok2, ok3, ok4]):
                (OUT / "dropdown_warning.txt").write_text(
                    f"Circle:{ok1} Division:{ok2} NatureAll:{ok3} Status:{ok4}\n"
                    "One or more dropdowns not found/selected. Check labels/casing.",
                    encoding="utf-8"
                )
                await p.screenshot(path=str(OUT / "fail_dropdowns.png"), full_page=True)
                raise RuntimeError("Dropdown selection failed (see dropdown_warning.txt)")

            await p.screenshot(path=str(OUT / "after_select_filters.png"), full_page=True)

            # 4) Click Show Report
            if not await click_first(p, [
                "button:has-text('Show Report')",
                "input[type='button'][value='Show Report']",
                "text=Show Report"
            ], timeout=8000):
                await p.screenshot(path=str(OUT / "fail_show_report.png"), full_page=True)
                raise RuntimeError("Show Report button not found")

            # 5) Wait for grid/content
            ok_grid = await wait_for_report_table(p, timeout_ms=20000)
            if not ok_grid:
                (OUT / "report_timeout.txt").write_text("Report grid did not appear in 20s.", encoding="utf-8")
                await p.screenshot(path=str(OUT / "fail_no_grid.png"), full_page=True)
                raise RuntimeError("Report grid did not appear in time")
            await p.screenshot(path=str(OUT / "after_grid_shown.png"), full_page=True)

            # 6) Click the PDF export icon/button – only this click is wrapped
            async def do_pdf_click():
                # try several likely controls
                if not await click_first(p, [
                    "a[title*='PDF']",
                    "button[title*='PDF']",
                    "text=PDF",
                    "i.fa-file-pdf",
                    "img[alt*='PDF']",
                    "button:has-text('Export PDF')",
                    "button:has-text('Download PDF')"
                ], timeout=8000):
                    await p.locator("a[title*='PDF'], button[title*='PDF'], img[alt*='PDF']").first.click(timeout=8000)

            ok_download = await click_and_wait_download(p, do_pdf_click, save_path, timeout_ms=25000)
            if not ok_download:
                await p.screenshot(path=str(OUT / "fail_pdf_click.png"), full_page=True)
                raise RuntimeError("Could not obtain PDF (no direct download and popup fallback failed)")

        # Run A
        pathA = OUT / f"{nameA}_{stamp}.pdf"
        await steps_A(page, pathA)
        log(f"Saved {pathA.name}")

        # For now, reuse the same flow for B (change later if needed)
        pathB = OUT / f"{nameB}_{stamp}.pdf"
        await steps_A(page, pathB)
        log(f"Saved {pathB.name}")

        await context.close()
        await browser.close()

    return [str(pathA), str(pathB)]

async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join([Path(f).name for f in files]))
    await send_via_telegram(files)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
