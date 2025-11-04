#!/usr/bin/env python3
import os, sys, asyncio, traceback
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

TZ = "Asia/Kolkata"
BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

def today_str():
    # Use IST date in filename
    from datetime import timezone, timedelta
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # 1) Login page
        await page.goto(login_url, wait_until="domcontentloaded")
        # CHANGE these selectors if your site uses different names/IDs:
        if await page.locator("input[name='username']").count():
            await page.fill("input[name='username']", username)
            await page.fill("input[name='password']", password)
            await page.click("button[type='submit']")
            await page.wait_for_load_state("networkidle")

        # 2) Report A — EDIT these clicks to match your site’s menus/buttons
        async def steps_A(p):
            await p.click("text=Reports")
            await p.click("text=Daily Report A")
            # If your site needs filters/dates, add clicks here.
            await p.click("button:has-text('Download PDF')")

        pathA = OUT / f"{nameA}_{stamp}.pdf"
        await download_report(page, steps_A, pathA)

        # 3) Report B — EDIT these clicks to match your site
        async def steps_B(p):
            await p.click("text=Reports")
            await p.click("text=Daily Report B")
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
