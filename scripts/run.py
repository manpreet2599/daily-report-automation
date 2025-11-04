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

async def site_login_and_download():
    login_url   = os.environ["LOGIN_URL"]
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    nameA       = os.getenv("REPORT_A_NAME", "ReportA")
    nameB       = os.getenv("REPORT_B_NAME", "ReportB")
    stamp       = today_str()
    user_type   = os.getenv("USER_TYPE", "").strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage"]
        )
        context = await browser.new_context(accept_downloads=True)
        # Be a bit more patient than defaults
        context.set_default_timeout(60000)                 # 60s for actions
        context.set_default_navigation_timeout(120000)     # 120s for page.goto
        page = await context.new_page()

        # 1) Login page
        # try a couple times in case of slow CDN/redirects
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="load")
                await page.wait_for_load_state("networkidle", timeout=60000)
                break
            except PWTimeout as e:
                last_err = e
        if last_err:
            raise last_err

        # --- Select User Type (do this BEFORE typing username/password) ---
        if user_type:
            # Try native <select> first (update IDs/names if your site differs)
            selected = False
            for sel in ["select#userType", "select[name='userType']", "select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, label=user_type)
                        selected = True
                        break
                    except Exception:
                        pass
            if not selected:
                # Fallback for custom dropdowns (click to open, then click option text)
                for sel in ["#userTypeDropdown", "[data-testid='user-type']", "div.select-user-type"]:
                    if await page.locator(sel).count():
                        await page.click(sel)
                        await page.get_by_text(user_type, exact=True).click()
                        selected = True
                        break
            log(f"User type selected: {user_type} (selected={selected})")

        # --- Fill username and password (adjust selectors if needed) ---
        if await page.locator("input[name='username']").count():
            await page.fill("input[name='username']", username)
        elif await page.locator("#email").count():
            await page.fill("#email", username)

        if await page.locator("input[name='password']").count():
            await page.fill("input[name='password']", password)
        elif await page.locator("#pwd").count():
            await page.fill("#pwd", password)

        # Click the login/submit button (several common variants)
        for sel in [
            "button:has-text('Sign in')",
            "button:has-text('Login')",
            "button[type='submit']",
            "[role='button']:has-text('Sign in')",
        ]:
            try:
                await page.click(sel, timeout=3000)
                break
            except Exception:
                pass

        await page.wait_for_load_state("networkidle", timeout=60000)

        # 2) Report A — EDIT these clicks to match your site’s menus/buttons
        async def steps_A(p):
            await p.click("text=Reports")
            await p.click("text=Daily Report A")
            # Add your filters/dropdowns/date selections here if needed
            await p.click("button:has-text('Download PDF')")

        pathA = OUT / f"{nameA}_{stamp}.pdf"
        await download_report(page, steps_A, pathA)

        # 3) Report B — EDIT these clicks to match your site
        async def steps_B(p):
            await p.click("text=Reports")
            await p.click("text=Daily Report B")
            # Add your filters/dropdowns/date selections here if needed
            await p.click("button:has-text('Download PDF')")

        pathB = OUT / f"{nameB}_{stamp}.pdf"
        await download_report(page, steps_B, pathB)

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
