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

# === Date helpers ===
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

def today_for_filename():
    return now_ist().strftime("%d-%m-%Y")  # for file name only

def ist_today_variants():
    d = now_ist()
    return (
        d.strftime("%Y-%m-%d"),  # 2025-11-06
        d.strftime("%d-%m-%Y"),  # 06-11-2025
        d.strftime("%d/%m/%Y"),  # 06/11/2025
    )

FROM_FIXED_DATE_VARIANTS = (
    "2024-07-26",
    "26-07-2024",
    "26/07/2024",
)

# Treat anything below this as a "blank/header-only" PDF (your manual good file ~54,696 bytes)
MIN_VALID_PDF_BYTES = 50000

def log(msg):
    print(msg, flush=True)

async def snap(page, name, full=False):
    if not DEBUG:
        return
    try:
        await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception:
        pass

# ===================== FAST PANEL BINDING =====================

async def get_app_panel(page):
    panel = page.locator("xpath=//div[.//text()[contains(.,'Application Wise Report')]]").first
    await panel.wait_for(state="visible", timeout=10000)
    return panel

# ===================== FAST SELECT/INPUT OPS (PANEL-SCOPED) =====================

FAST_SELECT_JS = """
(root, args) => {
  const { labelText, wanted } = args;
  const norm = s => (s||'').trim().toLowerCase();
  const wantedNorm = norm(wanted);

  let label = Array.from(root.querySelectorAll('label'))
      .find(l => norm(l.textContent).includes(norm(labelText)));
  if (!label) return {ok:false, reason:'label not found'};

  let sel = null;
  const forId = label.getAttribute('for');
  if (forId) sel = root.querySelector('#'+CSS.escape(forId));
  if (!sel) {
    sel = (label.nextElementSibling && label.nextElementSibling.tagName === 'SELECT')
      ? label.nextElementSibling
      : (label.closest('div') || root).querySelector('select');
  }
  if (!sel) return {ok:false, reason:'select not found'};

  let idx = -1;
  for (let i=0;i<sel.options.length;i++){
    const t = norm(sel.options[i].textContent);
    if (t.includes(wantedNorm)) { idx = i; break; }
  }
  if (idx === -1) return {ok:false, reason:'option not found'};

  sel.selectedIndex = idx;
  sel.dispatchEvent(new Event('input',{bubbles:true}));
  sel.dispatchEvent(new Event('change',{bubbles:true}));
  try { sel.dispatchEvent(new Event('changed.bs.select',{bubbles:true})); } catch(e) {}
  return {ok:true, value: sel.options[idx].textContent.trim()};
}
"""

FAST_MULTISELECT_ALL_JS = """
(root, args) => {
  const { labelText } = args;
  const norm = s => (s||'').trim().toLowerCase();

  let label = Array.from(root.querySelectorAll('label'))
      .find(l => norm(l.textContent).includes(norm(labelText)));
  if (!label) return {ok:false, reason:'label not found'};

  let sel = null;
  const forId = label.getAttribute('for');
  if (forId) sel = root.querySelector('#'+CSS.escape(forId));
  if (!sel) {
    sel = (label.nextElementSibling && label.nextElementSibling.tagName === 'SELECT')
      ? label.nextElementSibling
      : (label.closest('div') || root).querySelector('select');
  }
  if (!sel) return {ok:false, reason:'select not found'};

  let changed=false;
  for (const o of sel.options){ if(!o.selected){ o.selected=true; changed=true; } }
  if (changed) {
    sel.dispatchEvent(new Event('input',{bubbles:true}));
    sel.dispatchEvent(new Event('change',{bubbles:true}));
    try { sel.dispatchEvent(new Event('changed.bs.select',{bubbles:true})); } catch(e) {}
  }
  return {ok:true};
}
"""

DATE_FILL_JS = """
(root, args) => {
  const { fromVal, toVal } = args;
  const setOne = (sel, v) => {
    const el = root.querySelector(sel);
    if (!el) return false;
    el.value = v;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    try { el.dispatchEvent(new Event('changed.bs.select',{bubbles:true})); } catch(e) {}
    return true;
  };
  let okFrom = false, okTo = false;
  const fromCandidates = ['#fromDate','input[name="fromDate"]','input[name*="fromdate" i]','input[placeholder*="From" i]'];
  const toCandidates   = ['#toDate','input[name="toDate"]','input[name*="todate" i]','input[placeholder*="To" i]'];
  for (const c of fromCandidates) if (setOne(c, fromVal)) { okFrom = true; break; }
  for (const c of toCandidates)   if (setOne(c, toVal))   { okTo = true; break; }
  return {okFrom, okTo};
}
"""

async def fast_select(panel, label, text):
    res = await panel.evaluate(FAST_SELECT_JS, {"labelText": label, "wanted": text})
    return bool(res and res.get("ok"))

async def fast_select_all(panel, label):
    res = await panel.evaluate(FAST_MULTISELECT_ALL_JS, {"labelText": label})
    return bool(res and res.get("ok"))

async def panel_input_value(panel, selectors) -> str:
    for sel in selectors:
        try:
            loc = panel.locator(sel).first
            if await loc.count():
                v = (await loc.input_value()).strip()
                if v:
                    return v
        except Exception:
            pass
    return ""

# ===================== DATA READINESS (PANEL-SCOPED, QUICK) =====================

async def panel_has_rows(panel):
    try:
        return await panel.evaluate("""
          (root)=>{
            const tbs = root.querySelectorAll('table tbody');
            for(const tb of tbs){
              for(const tr of tb.querySelectorAll('tr')){
                const tds = Array.from(tr.querySelectorAll('td')).map(td=>(td.innerText||'').trim());
                if (tds.filter(x=>x).length>=2) return true;
              }
            }
            return false;
          }
        """)
    except Exception:
        return False

# ===================== SHOW REPORT (PANEL) =====================

async def show_report_and_wait(panel, *, settle_ms=5600, response_wait_ms=12000):
    clicked = False
    for sel in [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "input[type='submit'][value='Show Report']",
    ]:
        try:
            loc = panel.locator(sel).first
            if await loc.count():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=5000)
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        clicked = await panel.evaluate("""
          (root)=>{
            const norm=s=>(s||'').trim().toLowerCase();
            const btn=[...root.querySelectorAll('button,input[type=button],input[type=submit]')]
              .find(b=>{const t=norm(b.innerText||b.value||''); return t==='show report'||t.includes('show report');});
            if(!btn) return false; btn.click(); return true;
          }
        """)
    if not clicked:
        raise RuntimeError("Show Report button not found in panel")

    # best-effort: wait for a report-ish response (but don't stall too long)
    try:
        await panel.page.wait_for_response(
            lambda r: any(k in (r.url or '').lower() for k in (
                "applicationwisereport","appwisereport","getreport","reportdata","report"
            )),
            timeout=response_wait_ms
        )
    except Exception:
        pass

    await asyncio.sleep(settle_ms/1000)
    return await panel_has_rows(panel)

# ===================== PDF (PANEL) =====================

async def click_pdf_icon(panel):
    # Prefer an icon near the filters; fallback to first visible inside panel
    for sel in [
        "xpath=.//img[contains(@src,'pdf') or contains(@alt,'PDF')][ancestor::div[.//button[contains(.,'Show Report')]]]",
        "xpath=(.//img[contains(@src,'pdf') or contains(@alt,'PDF')])[1]",
        "xpath=(.//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]:
        ico = panel.locator(sel).first
        if await ico.count():
            await ico.scroll_into_view_if_needed()
            await ico.click(timeout=5000, force=True)
            return True
    return False

async def click_and_wait_download(page, click_pdf, save_as_path, timeout_ms=35000):
    log("[pdf] trying direct download…")
    try:
        async with page.expect_download(timeout=timeout_ms) as dl_info:
            await click_pdf()
        dl = await dl_info.value
        await dl.save_as(save_as_path)
        log(f"[pdf] saved → {save_as_path}")
        return True
    except Exception as e:
        log(f"[pdf] direct download failed ({e}); popup fallback…")
        try:
            async with page.expect_popup(timeout=5000) as pop_info:
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
        except Exception as e2:
            log(f"[pdf] popup fallback failed: {e2}")
        return False

async def download_pdf_from_panel(panel, save_path: Path, *, settle_ms=5600):
    """Returns (ok, size_bytes_or_0)."""
    if settle_ms > 0:
        await asyncio.sleep(settle_ms/1000)

    async def do_click():
        ok = await click_pdf_icon(panel)
        if not ok:
            raise RuntimeError("PDF icon not found in panel")

    ok = await click_and_wait_download(panel.page, do_click, save_path, timeout_ms=35000)
    if not ok:
        return False, 0

    try:
        size = Path(save_path).stat().st_size
        log(f"[pdf] size: {size} bytes")
        return True, size
    except FileNotFoundError:
        return False, 0

# ===================== LOCAL PRINT FALLBACK (panel → PDF) =====================

PRINT_INJECT_CSS = """
<style id="__print_only">
  @media print {
    body * { visibility: hidden !important; }
    #__print_target, #__print_target * { visibility: visible !important; }
    #__print_target { position: absolute; left: 0; top: 0; width: 100% !important; }
  }
</style>
"""

async def render_panel_to_pdf(panel, pdf_path: Path):
    page = panel.page
    # Wrap the panel in a special container and inject @media print CSS
    await page.evaluate(
        """({ panelSelector, css }) => {
            const panel = document.querySelector(panelSelector);
            if (!panel) return;
            if (!document.getElementById('__print_only')) {
              document.head.insertAdjacentHTML('beforeend', css);
            }
            const wrapId = '__print_target';
            if (!document.getElementById(wrapId)) {
              const wrapper = document.createElement('div');
              wrapper.id = wrapId;
              panel.parentNode.insertBefore(wrapper, panel);
              wrapper.appendChild(panel);
            }
        }""",
        {
            "panelSelector": await panel.evaluate("e => e.tagName === 'DIV' ? '#' + (e.id || '') : 'div'"),
            "css": PRINT_INJECT_CSS,
        }
    )

    # Ensure print media and save PDF
    await page.emulate_media(media="print")
    await page.pdf(
        path=str(pdf_path),
        format="A4",
        margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
        print_background=True,
    )
    log(f"[pdf:fallback] printed panel to → {pdf_path}")

# ===================== DATES: TRY MULTIPLE FORMATS THEN SHOW REPORT =====================

async def set_dates_and_show(panel, *, settle_ms_first=5600):
    to_variants = ist_today_variants()
    tries = []
    for f in FROM_FIXED_DATE_VARIANTS:
        for t in to_variants:
            tries.append((f, t))

    for idx, (from_val, to_val) in enumerate(tries, start=1):
        try:
            res = await panel.evaluate(DATE_FILL_JS, {"fromVal": from_val, "toVal": to_val})
            log(f"[dates] try {idx}: From='{from_val}' To='{to_val}' set={res}")
        except Exception as e:
            log(f"[dates] set error on try {idx}: {e}")

        has_rows = await show_report_and_wait(panel, settle_ms=settle_ms_first if idx == 1 else 3000, response_wait_ms=8000)
        if has_rows:
            log(f"[dates] data present with format try {idx}")
            return True

        log(f"[dates] no rows with try {idx}; trying next format…")

    return False

# ===================== MAIN =====================

async def site_login_and_download():
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = today_for_filename()

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

        context.set_default_timeout(20000)
        context.set_default_navigation_timeout(60000)
        page = await context.new_page()

        # Login
        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="domcontentloaded"); break
            except PWTimeout as e:
                last_err = e
        if last_err:
            raise last_err

        if user_type:
            for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=user_type); break
                    except Exception:
                        try:
                            await page.select_option(sel, label=user_type); break
                        except Exception:
                            pass

        # Fill creds
        for sel in ["#username","input[name='username']","input[placeholder*='Login']","input[placeholder*='Email']"]:
            if await page.locator(sel).count():
                await page.fill(sel, username); break
        for sel in ["#password","input[name='password']","input[name='pwd']"]:
            if await page.locator(sel).count():
                await page.fill(sel, password); break

        await page.locator("button:has-text('Login'), button[type='submit'], [role='button']:has-text('Login')").first.click(timeout=5000)
        await page.wait_for_load_state("domcontentloaded")
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        # Navigate to Application Wise Report
        try:
            await page.get_by_text("MIS Reports", exact=False).first.click(timeout=7000)
        except Exception:
            await page.locator("a:has-text('MIS Reports'), button:has-text('MIS Reports'), li:has-text('MIS Reports')").first.click(timeout=7000)
        await asyncio.sleep(0.1)
        await page.get_by_text("Application Wise Report", exact=False).first.click(timeout=10000)
        await page.wait_for_load_state("domcontentloaded")

        # Bind panel once
        panel = await get_app_panel(page)

        async def run_one(status_text: str, filename: str):
            # Filters (panel-scoped, fast)
            await fast_select(panel, "Circle Office",   "LUDHIANA CANAL CIRCLE")
            await fast_select(panel, "Division Office", "FARIDKOT CANAL AND GROUND WATER DIVISION")
            await fast_select_all(panel, "Nature Of Application")
            await fast_select(panel, "Status", status_text)

            # Explicit date range (26/07/2024 → today) with multi-format fallback
            ok_rows = await set_dates_and_show(panel, settle_ms_first=5600)
            if not ok_rows:
                raise RuntimeError(f"No data rows in panel after trying date formats ({status_text}).")

            await snap(page, f"after_grid_shown_{status_text.lower()}.png")

            # 1) Try server export
            save_path = OUT / f"{filename} {stamp}.pdf"
            ok, size = await download_pdf_from_panel(panel, save_path, settle_ms=5600)

            # 2) If blank (or failed), print the panel to PDF locally
            if (not ok) or (size < MIN_VALID_PDF_BYTES):
                if ok:
                    log(f"[pdf] server export looks blank ({size} bytes < {MIN_VALID_PDF_BYTES}); using print fallback.")
                else:
                    log("[pdf] server export failed; using print fallback.")
                await render_panel_to_pdf(panel, save_path)

            log(f"Saved {save_path.name}")
            return str(save_path)

        pathA = await run_one("DELAYED", "Delayed Apps")
        pathB = await run_one("PENDING", "Pending Apps")

        await context.close(); await browser.close()
        return [pathA, pathB]

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
