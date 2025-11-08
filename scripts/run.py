#!/usr/bin/env python3
import os, sys, asyncio, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ------------------ config ------------------
BASE = Path(__file__).resolve().parent
OUT  = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"
LOGIN_URL = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
USER_TYPE = os.getenv("USER_TYPE", "").strip()  # e.g. XEN

CIRCLE_TEXT   = "LUDHIANA CANAL CIRCLE"
DIVISION_TEXT = "FARIDKOT CANAL AND GROUND WATER DIVISION"

def ist_today_ddmmyyyy():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d/%m/%Y")

FROM_DATE = "26/07/2024"
TO_DATE   = ist_today_ddmmyyyy()

def stamp_for_filename():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d-%m-%Y")

def log(msg): print(msg, flush=True)

async def snap(page, name, full=False):
    if not DEBUG: return
    try:
        await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception:
        pass

# ---------- tiny helpers ----------
async def wait_until_has_data(page, timeout_ms=30000):
    end = page.context._loop.time() + timeout_ms/1000.0
    # table with at least one data row (>=2 non-empty cells)
    js = """
    () => {
      const tbodies = Array.from(document.querySelectorAll('table tbody'));
      for (const tb of tbodies) {
        const rows = Array.from(tb.querySelectorAll('tr'));
        for (const r of rows) {
          const tds = Array.from(r.querySelectorAll('td')).map(td => (td.innerText||'').trim());
          const nonEmpty = tds.filter(x => x && x !== '\\u00A0');
          if (nonEmpty.length >= 2) return true;
        }
      }
      return false;
    }
    """
    while page.context._loop.time() < end:
        try:
            ok = await page.evaluate(js)
            if ok: return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False

# Use Bootstrap Select API to set a single-select by visible text.
SET_BOOTSTRAP_SELECT_BY_TEXT = """
(el, wantedText) => {
  // el is the underlying <select> used by bootstrap-select
  const norm = s => String(s||'').trim().toLowerCase();
  const w = norm(wantedText);

  // find option whose visible text matches (exact, case-insensitive)
  let val = null;
  for (const opt of el.options) {
    if (norm(opt.textContent) === w) { val = opt.value; break; }
  }
  if (val === null) {
    // try 'contains' match as fallback
    for (const opt of el.options) {
      if (norm(opt.textContent).includes(w)) { val = opt.value; break; }
    }
  }
  if (val === null) return {ok:false, reason:'option not found'};

  // set value and fire events
  el.value = val;
  el.dispatchEvent(new Event('input',  { bubbles:true }));
  el.dispatchEvent(new Event('change', { bubbles:true }));

  // refresh bootstrap-select visuals if available
  try {
    if (window.$ && typeof window.$(el).selectpicker === 'function') {
      window.$(el).selectpicker('refresh');
    }
  } catch(e){}

  // close any open dropdown (simulate clicking dead-space)
  try {
    const btn = el.closest('.bootstrap-select')?.querySelector('button.dropdown-toggle.show');
    if (btn) btn.blur();
    const menu = document.querySelector('.dropdown-menu.show');
    if (menu) menu.classList.remove('show');
  } catch(e){}

  return {ok:true, value:val, text:wantedText};
}
"""

# Select ALL options for the multi-select (Nature Of Application)
SELECT_ALL_MULTI = """
(el) => {
  if (!el) return {ok:false, reason:'no select'};
  let changed = false;
  for (const opt of el.options) {
    if (!opt.selected) { opt.selected = true; changed = true; }
  }
  if (changed) {
    el.dispatchEvent(new Event('input',  { bubbles:true }));
    el.dispatchEvent(new Event('change', { bubbles:true }));
  }
  try {
    if (window.$ && typeof window.$(el).selectpicker === 'function') {
      window.$(el).selectpicker('refresh');
    }
  } catch(e){}
  return {ok:true};
}
"""

# Fill date input by label text; supports dd/mm/yyyy
FILL_DATE_BY_LABEL = """
(labelText, value) => {
  const norm = s => String(s||'').trim().toLowerCase();
  const labs = Array.from(document.querySelectorAll('label'));
  const L = labs.find(l => norm(l.textContent).includes(norm(labelText)));
  const root = L ? (L.closest('div') || L.parentElement || document) : document;
  const candidates = [
    '#fromDate','input#fromDate',"input[name='fromDate']",
    '#toDate','input#toDate',"input[name='toDate']",
    "input[name*='fromdate' i]","input[name*='todate' i]",
    "input[placeholder*='From' i]","input[placeholder*='To' i]"
  ];
  for (const sel of candidates) {
    const el = root.querySelector(sel);
    if (el && el.tagName === 'INPUT') {
      el.value = value;
      el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
      return true;
    }
  }
  return false;
}
"""

async def set_bootstrap_select(page, label_contains_text: str, wanted_text: str):
    """
    Finds the first <select> next to/under a label that contains label_contains_text
    and sets it via bootstrap-select API by visible option text.
    """
    js = """
    (labelText) => {
      const norm = s => String(s||'').trim().toLowerCase();
      const labs = Array.from(document.querySelectorAll('label'));
      const L = labs.find(l => norm(l.textContent).includes(norm(labelText)));
      if (!L) return null;
      const root = L.closest('div') || L.parentElement || document;
      // Prefer a select inside bootstrap-select wrapper; else any select nearby
      let sel = root.querySelector('.bootstrap-select select, select');
      return sel || null;
    }
    """
    sel = await page.evaluate_handle(js, label_contains_text)
    try:
        res = await sel.evaluate(SET_BOOTSTRAP_SELECT_BY_TEXT, wanted_text)
        return bool(res and res.get("ok"))
    finally:
        try: await sel.dispose()
        except Exception: pass

async def select_nature_all(page):
    js = """
    () => {
      const labs = Array.from(document.querySelectorAll('label'));
      const L = labs.find(l => (l.textContent||'').toLowerCase().includes('nature of application'));
      if (!L) return {ok:false, reason:'label not found'};
      const root = L.closest('div') || L.parentElement || document;
      const sel = root.querySelector('.bootstrap-select select, select[multiple], select');
      if (!sel) return {ok:false, reason:'select not found'};
      return (function(el){
        let changed=false;
        for (const opt of el.options){ if(!opt.selected){ opt.selected = true; changed=true; } }
        if (changed){
          el.dispatchEvent(new Event('input',{bubbles:true}));
          el.dispatchEvent(new Event('change',{bubbles:true}));
        }
        try { if (window.$ && typeof window.$(el).selectpicker==='function') window.$(el).selectpicker('refresh'); } catch(e){}
        try {
          const menu = document.querySelector('.dropdown-menu.show');
          if (menu) menu.classList.remove('show');
        } catch(e){}
        return {ok:true};
      })(sel);
    }
    """
    try:
        res = await page.evaluate(js)
        return bool(res and res.get("ok"))
    except Exception:
        return False

async def set_date(page, which_label: str, value: str):
    try:
        ok = await page.evaluate(FILL_DATE_BY_LABEL, which_label, value)
        return bool(ok)
    except Exception:
        return False

async def click_show_report(page):
    # Click the "Show Report" button/input
    candidates = [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "text=Show Report"
    ]
    for sel in candidates:
        if await page.locator(sel).count():
            await page.locator(sel).first.click(timeout=5000)
            return True
    return False

async def click_pdf_icon(page):
    # red pdf icon near the report grid
    candidates = [
        "xpath=(//img[contains(@src,'pdf') or contains(@alt,'PDF')])[1]",
        "xpath=(//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]
    for sel in candidates:
        if await page.locator(sel).count():
            await page.locator(sel).first.scroll_into_view_if_needed()
            await page.locator(sel).first.click(timeout=6000, force=True)
            return True
    return False

async def run_once(context, page, status_text: str, file_prefix: str):
    # Navigate to the report page (menu click path is sometimes flaky; open directly)
    # Once logged in, this URL is accessible.
    await page.goto("https://esinchai.punjab.gov.in/Authorities/applicationwisereport.jsp", wait_until="domcontentloaded")
    await page.wait_for_selector("body", timeout=15000)
    await snap(page, "opened_report.png")

    # Set filters in UI (Bootstrap Select) exactly like a human:
    ok_c = await set_bootstrap_select(page, "Circle Office", CIRCLE_TEXT)
    log(f"[filter] Circle Office → {CIRCLE_TEXT} (ok={ok_c})")
    # wait Division options populate
    await asyncio.sleep(0.8)

    ok_d = await set_bootstrap_select(page, "Division Office", DIVISION_TEXT)
    log(f"[filter] Division Office → {DIVISION_TEXT} (ok={ok_d})")
    await asyncio.sleep(0.5)

    ok_n = await select_nature_all(page)
    log(f"[filter] Nature Of Application → Select All (ok={ok_n})")
    await asyncio.sleep(0.3)

    ok_s = await set_bootstrap_select(page, "Status", status_text)
    log(f"[filter] Status → {status_text} (ok={ok_s})")

    # Dates
    ok_f = await set_date(page, "From Date", FROM_DATE)
    ok_t = await set_date(page, "To Date", TO_DATE)
    log(f"[dates] From='{FROM_DATE}' set={ok_f}  To='{TO_DATE}' set={ok_t}")

    # Show report
    clicked = await click_show_report(page)
    if not clicked:
        raise RuntimeError("Could not click 'Show Report'")

    # Wait for real rows
    has_rows = await wait_until_has_data(page, timeout_ms=35000)
    if not has_rows:
        # Some pages render a "No record" message; try detect quickly
        try:
            await page.get_by_text("No record", exact=False).wait_for(timeout=1500)
            log("[info] The report returned 'No record'.")
        except Exception:
            raise RuntimeError("Report table did not populate with data rows.")

    await snap(page, f"after_grid_{status_text.lower()}.png")

    # Try the server PDF (red icon)
    target = OUT / f"{file_prefix} {stamp_for_filename()}.pdf"
    try:
        async with page.expect_download(timeout=35000) as dl_info:
            ok_pdf = await click_pdf_icon(page)
            if not ok_pdf:
                raise RuntimeError("PDF icon not found")
        dl = await dl_info.value
        await dl.save_as(str(target))
        size = target.stat().st_size
        log(f"[pdf] saved → {target}  ({size} bytes)")
        if size < 50000:
            log("[warn] Server PDF looks too small; likely blank headers. Try again once after 3s.")
            await asyncio.sleep(3.0)
            async with page.expect_download(timeout=35000) as dl2info:
                await click_pdf_icon(page)
            dl2 = await dl2info.value
            await dl2.save_as(str(target))
            size2 = target.stat().st_size
            log(f"[pdf] retry size → {size2} bytes")
            if size2 < 50000:
                raise RuntimeError("Server PDF remained small; aborting this attempt.")
    except Exception as e:
        # If server export still fails/blank, last resort: print the visible table area
        log(f"[warn] Direct server PDF failed: {e}. Falling back to printing the table node.")
        # isolate the report panel/table and print to PDF
        await page.evaluate("""
        () => {
          const tbl = document.querySelector('table');
          if (tbl) tbl.style.boxShadow = 'none';
        }
        """)
        # Use a temporary new page to print full content including table
        pdf_page = await context.new_page()
        html = await page.content()
        await pdf_page.set_content(html, wait_until="domcontentloaded")
        await pdf_page.emulate_media(media="screen")
        await pdf_page.pdf(path=str(target), print_background=True, margin={"top":"10mm","bottom":"10mm","left":"10mm","right":"10mm"})
        await pdf_page.close()
        log(f"[pdf:fallback] printed → {target}")

    return str(target)

# ------------------ main flow ------------------
async def site_login_and_download():
    username = os.environ["USERNAME"]
    password = os.environ["PASSWORD"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-extensions"]
        )
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # Go login
        log(f"Opening login page: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Sometimes there is a user type select
        if USER_TYPE:
            for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
                if await page.locator(sel).count():
                    try:
                        await page.select_option(sel, value=USER_TYPE)
                        break
                    except Exception:
                        try:
                            await page.select_option(sel, label=USER_TYPE)
                            break
                        except Exception:
                            pass

        # Fill username/password (try several common fields)
        user_cands = ["#username","input[name='username']","#login","#loginid","input[name='loginid']","input[placeholder*='Login' i]"]
        pass_cands = ["#password","input[name='password']","#pwd","input[placeholder='Password']"]

        filled_u = False
        for s in user_cands:
            if await page.locator(s).count():
                try:
                    await page.fill(s, username)
                    filled_u = True
                    break
                except Exception:
                    pass

        filled_p = False
        for s in pass_cands:
            if await page.locator(s).count():
                try:
                    await page.fill(s, password)
                    filled_p = True
                    break
                except Exception:
                    pass

        if not (filled_u and filled_p):
            raise RuntimeError("Could not find login fields")

        # Click Login
        btn_cands = [
            "button:has-text('Login')","button:has-text('Sign in')",
            "button[type='submit']","[role='button']:has-text('Login')","input[type='submit']"
        ]
        clicked = False
        for s in btn_cands:
            if await page.locator(s).count():
                try:
                    await page.locator(s).first.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    pass
        if not clicked:
            # fallback: press Enter on password field
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
        except PWTimeout:
            pass

        log("Login complete.")
        log(f"Current URL: {page.url}")
        await snap(page, "step_after_login.png")

        # ---- Run DELAYED then PENDING ----
        delayed = await run_once(context, page, "DELAYED", "Delayed Apps")
        log(f"Saved {Path(delayed).name}")

        # New tab sometimes keeps old state; re-use same page but re-open URL fresh:
        pending = await run_once(context, page, "PENDING", "Pending Apps")
        log(f"Saved {Path(pending).name}")

        await context.close(); await browser.close()
        return [delayed, pending]

async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join([Path(f).name for f in files]))

    # Optional Telegram send
    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if bot and chat:
        import requests
        for p in files:
            with open(p, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{bot}/sendDocument",
                    data={"chat_id": chat},
                    files={"document": (Path(p).name, f, "application/pdf")}
                )
            if r.status_code == 200:
                log(f"[tg] sent {Path(p).name}")
            else:
                log(f"[tg] send failed for {Path(p).name}: {r.text}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
