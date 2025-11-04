#!/usr/bin/env python3
import os, sys, asyncio, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

def today_str():
    # Use IST date in filename
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")

def log(msg):
    print(msg, flush=True)

async def download_report(page, nav_steps, save_as_path):
    async with page.expect_download() as dl_info:
        await nav_steps(page)
    dl = await dl_info.value
    await dl.save_as(save_as_path)
    return save_as_path

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

# ---------- helpers to be tolerant with selectors ----------
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
        if user_type:
            log(f"Selecting user type: {user_type}")
            # Try native select first
            selected = False
            for sel in ["select#usertype", "select#userType", "select[name='userType']", "select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, label=user_type)
                        selected = True
                        break
                    except Exception:
                        pass
            # Fallback: custom dropdown (click box then option text)
            if not selected:
                for opener in ["#userTypeDropdown", "[data-testid='user-type']", "div.select-user-type", "label:has-text('Select User Type') + *"]:
                    if await page.locator(opener).count():
                        try:
                            await page.click(opener)
                            await page.get_by_text(user_type, exact=True).click()
                            selected = True
                            break
                        except Exception:
                            pass
            log(f"User type selected ok? {selected}")

        # 3) Fill username & password (be tolerant with ids/names)
        user_ok = await fill_first(page,
            ["input[name='username']", "#username", "input[name='login']", "#login", "input[name='userid']", "#userid", "#loginid", "input[name='loginid']"],
            username
        )
        pass_ok = await fill_first(page,
            ["input[name='password']", "#password", "input[name='pwd']", "#pwd"],
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

        # --------- Report A (EDIT selectors to match your site) ----------
        async def steps_A(p):
            await p.click("text=Reports")
            await p.click("text=Daily Report A")
            # Add filters/date selection here if needed
            await p.click("button:has-text('Download PDF')")

        pathA = OUT / f"{nameA}_{stamp}.pdf"
        await download_report(page, steps_A, pathA)
        log(f"Saved {pathA.name}")

        # --------- Report B (EDIT selectors to match your site) ----------
        async def steps_B(p):
            await p.click("text=Reports")
            await p.click("text=Daily Report B")
            # Add filters/date selection here if needed
            await p.click("button:has-text('Download PDF')")

        pathB = OUT / f"{nameB}_{stamp}.pdf"
        await download_report(page, steps_B, pathB)
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
