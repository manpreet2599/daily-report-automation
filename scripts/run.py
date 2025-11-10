#!/usr/bin/env python3
import os, sys, asyncio, traceback, re, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------- .env & paths ----------
from dotenv import load_dotenv
load_dotenv(override=True)

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"

def log(msg: str):
    print(msg, flush=True)

def ist_today_str(fmt="%d-%m-%Y"):
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime(fmt)

def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing {name}. Add it to your .env or export it before running.\n"
            f"Required: USERNAME, PASSWORD | Optional: USER_TYPE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DEBUG"
        )
    return val

async def snap(page, name, full=False):
    if not DEBUG: return
    try: await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception: pass


# ---------- DOM helpers ----------
async def get_text(node):
    try:
        return (await node.inner_text()).strip()
    except Exception:
        return ""

async def wait_for_any_selector(page, selectors, timeout=8000):
    for sel in selectors:
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=timeout)
            return sel
        except Exception:
            pass
    return None

async def click_first(page, selectors, timeout=6000, force=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=timeout, force=force)
                return True
        except Exception:
            pass
    return False

async def fill_first(page, selectors, value, timeout=6000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.fill(value, timeout=timeout)
                try:
                    await page.eval_on_selector(sel, "el => {el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));}")
                except Exception:
                    pass
                return True
        except Exception:
            pass
    return False


# ---------- Report page actions ----------
REPORT_URL = "https://esinchai.punjab.gov.in/Authorities/applicationwisereport.jsp"

async def goto_report_page(page):
    # Direct jump (fastest & most reliable after login)
    try:
        await page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass

    # If direct jump didn’t work, try menu path
    if "applicationwisereport.jsp" not in page.url:
        log("[nav] Opening via menu…")
        await click_first(page, ["text=MIS Reports", "a:has-text('MIS Reports')", "nav >> text=MIS Reports"], timeout=6000)
        await asyncio.sleep(0.2)
        ok = await click_first(page, ["text=Application Wise Report", "a:has-text('Application Wise Report')"], timeout=6000)
        if not ok:
            raise RuntimeError("Could not open 'Application Wise Report' via menu")
        try:
            await page.wait_for_url(re.compile(r".*/Authorities/applicationwisereport\.jsp.*"), timeout=20000)
        except Exception:
            pass

    await page.wait_for_load_state("domcontentloaded")
    # Wait for at least one control to show
    await wait_for_any_selector(page, [
        "label:has-text('Circle Office')",
        "label:has-text('Division Office')",
        "label:has-text('Nature Of Application')",
        "label:has-text('Status')",
        "text=Show Report",
        "input#fromDate", "input#toDate"
    ], timeout=10000)
    log("[nav] Application Wise Report page ready.")
    await snap(page, "after_open_app_wise.png")


async def _find_control_near_label(page, label_text):
    """
    Returns a dict with keys:
      kind: 'select'|'bootstrap'|'input'|None
      handle: selector string for primary control (select/input/button)
      root: container selector for this section
    """
    # Find the label
    label = page.locator(f"label:has-text('{label_text}')").first
    if not await label.count():
        # Try contains (case-insensitive) via xpath
        label = page.locator(f"xpath=//label[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{label_text.lower()}')]").first
        if not await label.count():
            return {"kind": None, "handle": None, "root": None}

    # Prefer sibling select
    # 1) Direct select after label
    sib_select = label.locator("xpath=following::select[1]").first
    if await sib_select.count():
        return {"kind": "select", "handle": "xpath=(" + await sib_select.evaluate("e => e.outerHTML") + ")", "root": None}

    # 2) Bootstrap-select pattern: a div with .bootstrap-select and a button.dropdown-toggle
    #    Try label's next container
    bs_button = label.locator("xpath=following::*[contains(@class,'bootstrap-select')][1]//button[contains(@class,'dropdown-toggle')]").first
    if await bs_button.count():
        return {"kind": "bootstrap", "handle": "xpath=(//label[contains(normalize-space(), \"" + label_text + "\")]/following::*[contains(@class,'bootstrap-select')][1]//button[contains(@class,'dropdown-toggle')])[1]", "root": None}

    # 3) Any input (for dates)
    date_input = label.locator("xpath=following::input[1]").first
    if await date_input.count():
        return {"kind": "input", "handle": "xpath=(//label[contains(normalize-space(), \"" + label_text + "\")]/following::input[1])[1]", "root": None}

    # Fallbacks by known ids / names
    low = label_text.lower()
    if "circle" in low:
        return {"kind": "select", "handle": "select#circle, select#circleId, select[name='circle'], select[name*='circle']", "root": None}
    if "division" in low:
        return {"kind": "select", "handle": "select#division, select#divisionId, select[name='division'], select[name*='division']", "root": None}
    if "status" in low:
        return {"kind": "select", "handle": "select#status, select#statusId, select[name='status'], select[name*='status']", "root": None}
    if "nature" in low:
        # multi-select
        return {"kind": "select", "handle": "label:has-text('Nature Of Application') ~ select, select[name*='nature']", "root": None}
    if "from" in low:
        return {"kind": "input", "handle": "#fromDate, input#fromDate, input[name='fromDate'], input[name*='fromdate' i], input[placeholder*='From' i]", "root": None}
    if "to" in low:
        return {"kind": "input", "handle": "#toDate, input#toDate, input[name='toDate'], input[name*='todate' i], input[placeholder*='To' i]", "root": None}

    return {"kind": None, "handle": None, "root": None}


async def set_select_by_label(page, label_text: str, wanted_text: str) -> bool:
    """
    Works with native <select> and bootstrap-select dropdowns.
    """
    info = await _find_control_near_label(page, label_text)
    kind, handle = info["kind"], info["handle"]
    if not kind or not handle:
        return False

    if kind == "select":
        # Native <select>
        try:
            # Prefer label match
            await page.select_option(handle, label=wanted_text)
            return True
        except Exception:
            try:
                await page.select_option(handle, value=wanted_text)
                return True
            except Exception:
                pass
        # Try contains match via JS
        try:
            js = """
            (sel, txt) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              const target = (txt||'').trim().toLowerCase();
              let idx = -1;
              for (let i=0;i<el.options.length;i++){
                const t = (el.options[i].text || '').trim().toLowerCase();
                if (t.includes(target)){ idx = i; break; }
              }
              if (idx === -1) return false;
              el.selectedIndex = idx;
              el.dispatchEvent(new Event('input',{bubbles:true}));
              el.dispatchEvent(new Event('change',{bubbles:true}));
              return true;
            }
            """
            ok = await page.evaluate(js, handle, wanted_text)
            return bool(ok)
        except Exception:
            return False

    if kind == "bootstrap":
        try:
            # Open menu
            await page.locator(handle).click(timeout=6000)
            # Find any menu item containing wanted text
            menu = page.locator(".dropdown-menu.show, .show .dropdown-menu").first
            await menu.wait_for(timeout=6000)
            item = menu.locator("li, a, span, .text").filter(has_text=wanted_text).first
            if not await item.count():
                # Attempt to scroll & find
                found = False
                for _ in range(20):
                    if await menu.locator("li, a, span, .text").filter(has_text=wanted_text).count():
                        found = True
                        break
                    try:
                        await menu.evaluate("(m)=>m.scrollBy(0,250)")
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
                if not found:
                    # close
                    try: await page.keyboard.press("Escape")
                    except Exception: pass
                    return False
                item = menu.locator("li, a, span, .text").filter(has_text=wanted_text).first

            await item.click(timeout=6000)
            # Close the open dropdown (the site leaves it open)
            try: await page.keyboard.press("Escape")
            except Exception: pass
            try: await page.mouse.click(10,10)
            except Exception: pass
            return True
        except Exception:
            try: await page.keyboard.press("Escape")
            except Exception: pass
            return False

    return False


async def select_nature_all(page) -> bool:
    """
    Force-select all options for 'Nature Of Application'.
    """
    info = await _find_control_near_label(page, "Nature Of Application")
    if not info["kind"] or not info["handle"]:
        return False

    if info["kind"] == "select":
        try:
            js = """
            (sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              if (!el.options || el.options.length === 0) return false;
              let changed = false;
              for (const o of el.options) { if (!o.selected) { o.selected = true; changed = true; } }
              if (changed) {
                el.dispatchEvent(new Event('input',{bubbles:true}));
                el.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return true;
            }
            """
            ok = await page.evaluate(js, info["handle"])
            return bool(ok)
        except Exception:
            pass

    if info["kind"] == "bootstrap":
        try:
            await page.locator(info["handle"]).click(timeout=6000)
            menu = page.locator(".dropdown-menu.show, .show .dropdown-menu").first
            await menu.wait_for(timeout=6000)
            # click "Select All" if present; otherwise click every option
            sel_all = menu.locator("text=Select All, text=Select all, text=Select All ").first
            if await sel_all.count():
                await sel_all.click(timeout=4000)
            else:
                items = menu.locator("li a, .dropdown-item, .text").all()
                for _ in range(30):
                    try:
                        await menu.evaluate("(m)=>m.scrollBy(0,400)")
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
                # attempt clicking all visible options
                try:
                    count = await menu.locator("li a, .dropdown-item, .text").count()
                    for i in range(count):
                        try:
                            await menu.locator("li a, .dropdown-item, .text").nth(i).click(timeout=1500)
                        except Exception:
                            pass
                except Exception:
                    pass
            try: await page.keyboard.press("Escape")
            except Exception: pass
            try: await page.mouse.click(10,10)
            except Exception: pass
            return True
        except Exception:
            try: await page.keyboard.press("Escape")
            except Exception: pass
            return False

    return False


async def set_date_inputs(page, from_str: str, to_str: str) -> bool:
    okF = await fill_first(page, [
        "#fromDate", "input#fromDate", "input[name='fromDate']",
        "input[name*='fromdate' i]", "input[placeholder*='From' i]",
    ], from_str)
    okT = await fill_first(page, [
        "#toDate", "input#toDate", "input[name='toDate']",
        "input[name*='todate' i]", "input[placeholder*='To' i]",
    ], to_str)
    return okF and okT


async def click_show_report_and_wait(page) -> bool:
    await click_first(page, [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "text=Show Report"
    ], timeout=8000)
    # Wait for table-like content
    try:
        await page.wait_for_selector("table, .table, .dataTable", state="visible", timeout=30000)
    except Exception:
        pass

    # Verify rows
    try:
        has_rows = await page.evaluate("""
            () => {
              const tbodies = Array.from(document.querySelectorAll('table tbody'));
              for (const tb of tbodies) {
                const trs = Array.from(tb.querySelectorAll('tr'));
                for (const tr of trs) {
                  const tds = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText||'').trim());
                  if (tds.filter(Boolean).length >= 2) return true;
                }
              }
              return false;
            }
        """)
        return bool(has_rows)
    except Exception:
        return False


async def render_current_panel_to_pdf(context, page, save_path: Path, title: str, filters_text: str):
    """
    Takes the visible report table (and header info) and renders a clean PDF.
    """
    html = await page.evaluate("""
        () => {
          const clone = document.documentElement.cloneNode(true);
          // Try to isolate the central report panel if possible
          // Keep all tables
          // Remove nav/menus if obvious
          const rmBySel = sel => clone.querySelectorAll(sel).forEach(el => el.remove());
          rmBySel("nav, header, footer, [role='navigation'], .navbar, .sidebar, .breadcrumbs");
          // Remove scripts
          clone.querySelectorAll("script").forEach(s => s.remove());
          return "<!doctype html>" + clone.outerHTML;
        }
    """)
    # Build a minimal printable frame around the table content
    # (We’ll not rely on site CSS; add simple table borders for clarity)
    skeleton = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4 landscape; margin: 12mm; }}
  body {{ font-family: Arial, sans-serif; font-size: 12px; }}
  h1 {{ margin: 0 0 6px 0; font-size: 18px; }}
  .meta {{ margin: 0 0 12px 0; font-size: 12px; color: #333; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #888; padding: 6px 8px; vertical-align: top; }}
  thead th {{ background: #eee; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">{filters_text}</div>
  <div id="content">
    <!-- We will inject extracted tables only to avoid nav detritus -->
  </div>
  <script>
    (function() {{
      const src = document.createElement('html');
      src.innerHTML = `{html.replace("`", "\\`")}`;
      const tables = src.querySelectorAll('table');
      const content = document.getElementById('content');
      if (tables.length === 0) {{
        const p = document.createElement('p');
        p.textContent = "No rows found.";
        content.appendChild(p);
      }} else {{
        tables.forEach(t => {{
          // only keep if there are data rows
          const rows = Array.from(t.querySelectorAll('tbody tr'));
          const has = rows.some(r => Array.from(r.querySelectorAll('td')).filter(td => (td.innerText||'').trim()).length >= 2);
          if (has) {{
            const clone = t.cloneNode(true);
            // Strip inline widths
            clone.querySelectorAll('*').forEach(el => el.removeAttribute('width'));
            content.appendChild(clone);
            const spacer = document.createElement('div');
            spacer.style.height = '12px';
            content.appendChild(spacer);
          }}
        }});
      }}
    }})();
  </script>
</body>
</html>"""

    pdf_page = await context.new_page()
    await pdf_page.set_content(skeleton, wait_until="load")
    await pdf_page.emulate_media(media="screen")
    await pdf_page.pdf(path=str(save_path), format="A4", landscape=True, print_background=True, margin={"top":"12mm","right":"12mm","bottom":"12mm","left":"12mm"})
    await pdf_page.close()
    log(f"[pdf] rendered → {save_path}")


# ---------- Main flow ----------
async def run_one(context, page, status_text: str, title_prefix: str):
    # Force filters
    circle = "LUDHIANA CANAL CIRCLE"
    division = "FARIDKOT CANAL AND GROUND WATER DIVISION"

    ok_c = await set_select_by_label(page, "Circle Office", circle)
    log(f"[filter] Circle Office → {circle} (ok={ok_c})")
    # Divisions often load after Circle; small wait helps
    await asyncio.sleep(0.6)
    ok_d = await set_select_by_label(page, "Division Office", division)
    log(f"[filter] Division Office → {division} (ok={ok_d})")

    ok_n = await select_nature_all(page)
    log(f"[filter] Nature Of Application → Select All (ok={ok_n})")

    ok_s = await set_select_by_label(page, "Status", status_text)
    log(f"[filter] Status → {status_text} (ok={ok_s})")

    # Dates: 26/07/2024 → today (dd/mm/yyyy)
    from_str = "26/07/2024"
    to_str   = ist_today_str("%d/%m/%Y")
    ok_dt = await set_date_inputs(page, from_str, to_str)
    log(f"[dates] set From='{from_str}' To='{to_str}' (ok={ok_dt})")

    # Show Report and ensure data rows
    ok_show = await click_show_report_and_wait(page)
    if not ok_show:
        # If no rows, still render page so you see “No rows”
        log("[show] No data rows detected; will still render for diagnosis.")
    await snap(page, f"after_grid_{status_text.lower()}.png")

    # Render to PDF from DOM
    stamp = ist_today_str("%d-%m-%Y")
    save_path = OUT / f"{title_prefix} {stamp}.pdf"
    filters_text = f"Circle: {circle} | Division: {division} | Nature: ALL | Status: {status_text} | Period: {from_str} to {to_str}"
    await render_current_panel_to_pdf(context, page, save_path, f"{title_prefix}", filters_text)
    return str(save_path)


async def login(context, login_url, username, password, user_type):
    page = await context.new_page()
    log(f"Opening login page: {login_url}")
    await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)

    # user type (optional)
    if user_type:
        for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
            try:
                if await page.locator(sel).count():
                    try: await page.select_option(sel, value=user_type)
                    except Exception:
                        await page.select_option(sel, label=user_type)
                    break
            except Exception:
                pass

    # username / password
    await fill_first(page, ["#username","input[name='username']","#login","#loginid","input[name='loginid']","input[name='userid']"], username)
    await fill_first(page, ["#password","input[name='password']","#pwd","input[name='pwd']"], password)

    # click login
    await click_first(page, [
        "button:has-text('Login')","button:has-text('Sign in')",
        "button[type='submit']","[role='button']:has-text('Login')"
    ], timeout=6000, force=True)

    # wait for dashboard or same page but authenticated
    try:
        await page.wait_for_url(re.compile(r".*/Authorities/.*dashboard\.jsp.*"), timeout=25000)
    except Exception:
        pass

    # If still on signup, but we might be logged-in and redirected later; wait a little
    await page.wait_for_load_state("domcontentloaded")
    log("Login complete.")
    return page


async def site_login_and_download():
    login_url = "https://esinchai.punjab.gov.in/signup.jsp"
    username  = require_env("USERNAME")
    password  = require_env("PASSWORD")
    user_type = os.getenv("USER_TYPE", "").strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-dev-shm-usage","--disable-extensions",
                "--disable-background-networking","--disable-client-side-phishing-detection",
                "--disable-default-apps","--disable-hang-monitor",
                "--disable-ipc-flooding-protection","--disable-popup-blocking",
                "--disable-prompt-on-repost","--no-first-run",
            ],
        )
        context = await browser.new_context(accept_downloads=True)
        # Block only fonts for speed; keep css/js/img so widgets work
        async def speed_filter(route, request):
            if request.resource_type in ("font",):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        # Login
        page = await login(context, login_url, username, password, user_type)

        # Go to report page
        await goto_report_page(page)

        # DELAYED
        delayed = await run_one(context, page, "DELAYED", "Delayed Apps")
        log(f"Saved {Path(delayed).name}")

        # Navigate fresh for PENDING (same page is fine; we’ll re-apply)
        await goto_report_page(page)
        pending = await run_one(context, page, "PENDING", "Pending Apps")
        log(f"Saved {Path(pending).name}")

        await context.close(); await browser.close()

    return [delayed, pending]


# ---------- Telegram ----------
async def send_via_telegram(files):
    bot = os.getenv("TELEGRAM_BOT_TOKEN"); chat = os.getenv("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        log("[tg] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping Telegram."); return
    import requests
    for p in files:
        with open(p, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{bot}/sendDocument",
                data={"chat_id": chat},
                files={"document": (Path(p).name, f, "application/pdf")}
            )
        if r.status_code != 200:
            log(f"[tg] send failed for {p}: {r.text}")
        else:
            log(f"[tg] sent {Path(p).name}")


# ---------- Entry ----------
async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join([Path(f).name for f in files]))
    try:
        await send_via_telegram(files)
    except Exception as e:
        log(f"[tg] error (continuing): {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
