#!/usr/bin/env python3
import os, sys, asyncio, traceback, re, base64, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"

# === Date helpers ===
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist() -> datetime:
    return datetime.now(IST)

def today_for_filename() -> str:
    return now_ist().strftime("%d-%m-%Y")  # for file name only

def ist_today_variants():
    d = now_ist()
    return (
        d.strftime("%Y-%m-%d"),  # 2025-11-06
        d.strftime("%d-%m-%Y"),  # 06-11-2025
        d.strftime("%d/%m/%Y"),  # 06/11/2025
    )

FROM_FIXED_DATE_VARIANTS = ("2024-07-26", "26-07-2024", "26/07/2024")

# treat anything below this size as blank/header-only export
MIN_VALID_PDF_BYTES = 50000

def log(msg): print(msg, flush=True)

async def snap(page, name, full=False):
    if not DEBUG: return
    try: await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception: pass

# ===================== PANEL FINDER =====================

async def get_app_panel(page):
    # Panel that contains "Application Wise Report" (case-insensitive)
    panel = page.locator(
        "xpath=//div[.//text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'application wise report')]]"
    ).first
    await panel.wait_for(state="visible", timeout=15000)
    return panel

# ===================== BOOTSTRAP-SELECT + NATIVE HELPERS =====================

def _label_xpath_ci(label_text: str) -> str:
    return (
        "contains("
        "translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), "
        f"translate('{label_text}', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
        ")"
    )

async def _bs_find_toggle_in_root(root, label_text: str):
    xp = (
        f".//label[{_label_xpath_ci(label_text)}]"
        f"/following::*[contains(@class,'bootstrap-select')][1]"
        f"//button[contains(@class,'dropdown-toggle')]"
    )
    return root.locator(f"xpath={xp}").first

async def _bs_wait_button_text_in_root(root, label_text: str, expect_text: str, timeout_ms=6000):
    btn = await _bs_find_toggle_in_root(root, label_text)
    try:
        await btn.wait_for(timeout=timeout_ms)
        inner = btn.locator(".filter-option-inner-inner").first
        tgt = inner if await inner.count() else btn
        await root.page.wait_for_function(
            """(el, want) => (el && (el.innerText||'').trim().toLowerCase().includes((want||'').toLowerCase()))""",
            tgt, expect_text, timeout=timeout_ms
        )
        return True
    except Exception:
        return False

async def _bs_close_dropdown(page):
    try: await page.keyboard.press("Escape")
    except Exception: pass
    try: await page.mouse.click(1, 1)
    except Exception: pass
    await asyncio.sleep(0.15)

async def bs_select_option_in_root(root, label_text: str, option_text: str) -> bool:
    btn = await _bs_find_toggle_in_root(root, label_text)
    if not await btn.count():
        return False

    await btn.scroll_into_view_if_needed()
    await btn.click(timeout=6000)

    # visible menu (often appended to body)
    menu = root.page.locator(".dropdown-menu.show, .show .dropdown-menu").first
    try:
        await menu.wait_for(timeout=5000)
    except Exception:
        await _bs_close_dropdown(root.page)
        return False

    item = menu.locator("li, a, span, .text").filter(has_text=option_text).first
    if not await item.count():
        for _ in range(16):
            try: await menu.evaluate("(m)=>m.scrollBy(0,250)")
            except Exception: pass
            cand = menu.locator("li, a, span, .text").filter(has_text=option_text).first
            if await cand.count():
                item = cand; break

    if not await item.count():
        await _bs_close_dropdown(root.page)
        return False

    await item.scroll_into_view_if_needed()
    await item.click(timeout=6000, force=True)
    await _bs_close_dropdown(root.page)

    ok = await _bs_wait_button_text_in_root(root, label_text, option_text, timeout_ms=6000)
    return ok

NATIVE_SELECT_JS = """
(root, args) => {
  const { labelText, wanted } = args;
  const norm = s => (s||'').trim().toLowerCase();
  const wantedNorm = norm(wanted);

  let label = Array.from(root.querySelectorAll('label'))
    .find(l => norm(l.textContent).includes(norm(labelText)));
  let sel = null;
  if (label) {
    const forId = label.getAttribute('for');
    if (forId) sel = root.querySelector('#'+CSS.escape(forId));
    if (!sel) {
      sel = (label.nextElementSibling && label.nextElementSibling.tagName === 'SELECT')
        ? label.nextElementSibling
        : (label.closest('div') || root).querySelector('select');
    }
  } else {
    sel = (root.querySelector('select'));
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
  return {ok:true, text: sel.options[idx].textContent.trim()};
}
"""

async def native_select_option_in_root(root, label_text: str, option_text: str) -> bool:
    try:
        res = await root.evaluate(NATIVE_SELECT_JS, {"labelText": label_text, "wanted": option_text})
        return bool(res and res.get("ok"))
    except Exception:
        return False

async def select_option_any(panel, page, label_text: str, option_text: str) -> bool:
    # 1: panel bootstrap
    ok = await bs_select_option_in_root(panel, label_text, option_text)
    if ok:
        log(f"[filter] {label_text} → {option_text} (panel bootstrap)")
        return True
    # 2: panel native
    ok = await native_select_option_in_root(panel, label_text, option_text)
    if ok:
        log(f"[filter] {label_text} → {option_text} (panel native)")
        return True
    # 3: page bootstrap
    ok = await bs_select_option_in_root(page, label_text, option_text)
    if ok:
        log(f"[filter] {label_text} → {option_text} (page bootstrap)")
        return True
    # 4: page native
    ok = await native_select_option_in_root(page, label_text, option_text)
    if ok:
        log(f"[filter] {label_text} → {option_text} (page native)")
        return True
    # 5: any-select fallback (by option text only)
    try:
        found = await page.evaluate("""
          (wanted) => {
            const norm = s => (s||'').trim().toLowerCase();
            const w = norm(wanted);
            const selects = Array.from(document.querySelectorAll('select'));
            for (const sel of selects) {
              let idx = -1;
              for (let i=0;i<sel.options.length;i++){
                const t = norm(sel.options[i].textContent);
                if (t.includes(w)){ idx=i; break; }
              }
              if (idx !== -1) {
                sel.selectedIndex = idx;
                sel.dispatchEvent(new Event('input',{bubbles:true}));
                sel.dispatchEvent(new Event('change',{bubbles:true}));
                return true;
              }
            }
            return false;
          }
        """, option_text)
        if found:
            log(f"[filter] {label_text} → {option_text} (any-select fallback)")
            return True
    except Exception:
        pass

    log(f"[filter] {label_text} FAILED to set '{option_text}' (all methods).")
    return False

# ====== NEW: wait until an option text exists in ANY select (for dependent dropdowns) ======
async def wait_until_option_exists_any_select(page, option_text: str, timeout_ms: int = 20000) -> bool:
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        try:
            exists = await page.evaluate("""
              (wanted) => {
                const norm = s => (s||'').trim().toLowerCase();
                const w = norm(wanted);
                for (const sel of document.querySelectorAll('select')) {
                  for (const opt of sel.options) {
                    if (norm(opt.textContent).includes(w)) return true;
                  }
                }
                return false;
              }
            """, option_text)
            if exists:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False

# A convenience: wait for fetch/XHR hint + the option to show up
async def wait_division_options_after_circle(page, target_division_text: str, max_wait_ms: int = 20000):
    # try to catch a division-related network response while polling the DOM
    async def wait_response():
        try:
            await page.wait_for_response(
                lambda r: any(k in (r.url or '').lower() for k in (
                    "division", "getdivision", "bycircle", "divisionlist", "getdivisions"
                )),
                timeout=max_wait_ms
            )
        except Exception:
            pass
    await asyncio.gather(wait_response(), wait_until_option_exists_any_select(page, target_division_text, max_wait_ms))

# ===================== DATE INPUTS =====================

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

# ===================== DATA READINESS (PANEL) =====================

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

# ===================== SHOW REPORT =====================

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
                await loc.click(timeout=6000)
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

# ===================== PDF ICON =====================

async def click_pdf_icon(panel):
    for sel in [
        "xpath=.//img[contains(@src,'pdf') or contains(@alt,'PDF')][ancestor::div[.//button[contains(.,'Show Report')]]]",
        "xpath=(.//img[contains(@src,'pdf') or contains(@alt,'PDF')])[1]",
        "xpath=(.//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]:
        ico = panel.locator(sel).first
        if await ico.count():
            await ico.scroll_into_view_if_needed()
            await ico.click(timeout=6000, force=True)
            return True
    return False

# ===================== REQUEST CAPTURE & REPLAY =====================

class RequestSniffer:
    def __init__(self, page):
        self.page = page
        self.events: List[Dict[str, Any]] = []
        self._req_handler = None
        self._resp_handler = None

    async def __aenter__(self):
        async def on_request(req):
            try:
                body = None
                try: body = req.post_data()
                except Exception: body = None
                self.events.append({
                    "type":"request",
                    "time": time.time(),
                    "url": req.url,
                    "method": req.method,
                    "headers": dict(req.headers),
                    "post_data": body
                })
            except Exception: pass

        async def on_response(resp):
            try:
                hdrs = {}
                try: hdrs = dict(resp.headers)
                except Exception: hdrs = {}
                self.events.append({
                    "type":"response",
                    "time": time.time(),
                    "url": resp.url,
                    "status": resp.status,
                    "headers": hdrs
                })
            except Exception: pass

        self._req_handler = self.page.on("request", on_request)
        self._resp_handler = self.page.on("response", on_response)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._req_handler: self.page.off("request", self._req_handler)
        if self._resp_handler: self.page.off("response", self._resp_handler)

    def find_pdf_exchange(self) -> Tuple[Optional[Dict[str,Any]], Optional[Dict[str,Any]]]:
        cand_resp = None
        for ev in reversed(self.events):
            if ev.get("type") == "response":
                url_l = (ev.get("url") or "").lower()
                h = {k.lower():v for k,v in (ev.get("headers") or {}).items()}
                ctype = h.get("content-type","").lower()
                if "pdf" in ctype or any(x in url_l for x in ("pdf","export","download","report")):
                    cand_resp = ev
                    break
        if not cand_resp:
            return (None, None)

        cand_req = None
        for ev in reversed(self.events):
            if ev.get("type") == "request" and (ev.get("url")==cand_resp.get("url")):
                cand_req = ev; break
        if not cand_req:
            base = cand_resp.get("url","")
            base_key = re.sub(r"\?.*$","", base)
            for ev in reversed(self.events):
                if ev.get("type")=="request" and ev.get("method") in ("POST","GET"):
                    u = ev.get("url","")
                    if re.sub(r"\?.*$","", u) == base_key:
                        cand_req = ev; break
        return (cand_req, cand_resp)

async def safe_replay_pdf(context, req_event: Dict[str,Any], save_path: Path) -> bool:
    if not req_event: return False
    url = req_event.get("url")
    method = (req_event.get("method") or "GET").upper()
    headers = dict(req_event.get("headers") or {})
    body = req_event.get("post_data")

    for k in ["content-length","host","origin","referer","cookie","sec-fetch-site","sec-fetch-mode",
              "sec-fetch-dest","sec-ch-ua","sec-ch-ua-platform","sec-ch-ua-mobile","user-agent"]:
        headers.pop(k, None)

    try:
        if method == "POST":
            resp = await context.request.post(url, data=body, headers=headers)
        else:
            resp = await context.request.get(url, headers=headers)
    except Exception as e:
        log(f"[replay] fetch error: {e}")
        return False

    if not resp.ok:
        log(f"[replay] HTTP {resp.status} on replay")
        return False

    ctype = (resp.headers.get("content-type","") or "").lower()
    if "pdf" not in ctype:
        log(f"[replay] Not a PDF content-type: {ctype}")
        return False

    try:
        b = await resp.body()
        Path(save_path).write_bytes(b)
        log(f"[replay] saved real PDF → {save_path} ({len(b)} bytes)")
        return True
    except Exception as e:
        log(f"[replay] write error: {e}")
        return False

# ===================== DOWNLOAD / FALLBACKS =====================

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

async def download_pdf_via_capture_and_replay(panel, save_path: Path, *, settle_ms=5600) -> Tuple[bool,int]:
    page = panel.page
    context = page.context
    if settle_ms>0:
        await asyncio.sleep(settle_ms/1000)

    async def do_click():
        ok = await click_pdf_icon(panel)
        if not ok:
            raise RuntimeError("PDF icon not found in panel")

    ok = False
    size = 0
    async with RequestSniffer(page) as sniff:
        ok = await click_and_wait_download(page, do_click, save_as_path=save_path, timeout_ms=35000)
        if ok:
            try:
                size = Path(save_path).stat().st_size
                log(f"[pdf] size: {size} bytes")
            except FileNotFoundError:
                size = 0

        if (not ok) or (size < MIN_VALID_PDF_BYTES):
            req_ev, resp_ev = sniff.find_pdf_exchange()
            if not req_ev and not resp_ev:
                log("[replay] No PDF-like network exchange captured.")
                return (ok, size)
            target = req_ev or {}
            r_ok = await safe_replay_pdf(context, target, save_path)
            if r_ok:
                try:
                    size = Path(save_path).stat().st_size
                except FileNotFoundError:
                    size = 0
                return (True, size)
    return (ok, size)

async def render_panel_via_screenshot_to_pdf(panel, pdf_path: Path):
    page = panel.page
    png_bytes = await panel.screenshot(type="png")
    b64 = base64.b64encode(png_bytes).decode("ascii")

    ctx = page.context
    tmp = await ctx.new_page()
    html = f"""<!doctype html><html><head><meta charset="utf-8"/><title>Report</title>
    <style>html,body{{margin:0;padding:0}}.wrap{{width:100%;box-sizing:border-box;padding:8mm}}img{{width:100%;height:auto;display:block}}</style>
    </head><body><div class="wrap"><img src="data:image/png;base64,{b64}" alt="report"/></div></body></html>"""
    await tmp.set_content(html, wait_until="load")
    await tmp.emulate_media(media="print")
    await tmp.pdf(path=str(pdf_path), format="A4", margin={"top":"0","right":"0","bottom":"0","left":"0"}, print_background=True)
    await tmp.close()
    log(f"[pdf:fallback] panel screenshot rendered to → {pdf_path}")

# ===================== DATES =====================

async def set_dates_and_show(panel, *, settle_ms_first=5600):
    to_variants = ist_today_variants()
    tries = [(f,t) for f in FROM_FIXED_DATE_VARIANTS for t in to_variants]

    for idx, (from_val, to_val) in enumerate(tries, start=1):
        try:
            res = await panel.evaluate(DATE_FILL_JS, {"fromVal": from_val, "toVal": to_val})
            log(f"[dates] try {idx}: From='{from_val}' To='{to_val}' set={res}")
        except Exception as e:
            log(f"[dates] set error on try {idx}: {e}")

        has_rows = await show_report_and_wait(panel, settle_ms=settle_ms_first if idx==1 else 3000, response_wait_ms=8000)
        if has_rows:
            log(f"[dates] data present with format try {idx}")
            return True
        log(f"[dates] no rows with try {idx}; trying next format…")
    return False

# ===================== MAIN FLOW =====================

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
        if last_err: raise last_err

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

        for sel in ["#username","input[name='username']","input[placeholder*='Login']","input[placeholder*='Email']"]:
            if await page.locator(sel).count():
                await page.fill(sel, username); break
        for sel in ["#password","input[name='password']","input[name='pwd']"]:
            if await page.locator(sel).count():
                await page.fill(sel, password); break

        await page.locator("button:has-text('Login'), button[type='submit'], [role='button']:has-text('Login')").first.click(timeout=6000)
        await page.wait_for_load_state("domcontentloaded")
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        # Navigate → MIS Reports → Application Wise Report
        log("[nav] Opening 'MIS Reports'…")
        try:
            await page.get_by_text("MIS Reports", exact=False).first.click(timeout=9000)
        except Exception:
            await page.locator("a:has-text('MIS Reports'), button:has-text('MIS Reports'), li:has-text('MIS Reports')").first.click(timeout=9000)
        await asyncio.sleep(0.2)

        log("[nav] Clicking 'Application Wise Report'…")
        await page.get_by_text("Application Wise Report", exact=False).first.click(timeout=12000)
        await page.wait_for_load_state("domcontentloaded")
        panel = await get_app_panel(page)
        log("[nav] Application Wise Report panel ready.")

        async def run_one(status_text: str, filename: str):
            # 1) Circle
            if not await select_option_any(panel, page, "Circle Office", "LUDHIANA CANAL CIRCLE"):
                await snap(page, "fail_circle.png")
                raise RuntimeError("Could not set Circle Office (all methods)")

            # 2) Division depends on Circle → wait for options to appear, then select
            target_div = "FARIDKOT CANAL AND GROUND WATER DIVISION"
            log("[wait] Waiting for Division list to populate after Circle…")
            await wait_division_options_after_circle(page, target_division_text=target_div, max_wait_ms=25000)

            # try selection again (now that options are present)
            if not await select_option_any(panel, page, "Division Office", target_div):
                # last-chance: directly pick the option where it exists
                picked = await page.evaluate("""
                  (wanted) => {
                    const norm = s => (s||'').trim().toLowerCase();
                    const w = norm(wanted);
                    const selects = Array.from(document.querySelectorAll('select'));
                    for (const sel of selects) {
                      for (let i=0;i<sel.options.length;i++){
                        if (norm(sel.options[i].textContent).includes(w)) {
                          sel.selectedIndex = i;
                          sel.dispatchEvent(new Event('input',{bubbles:true}));
                          sel.dispatchEvent(new Event('change',{bubbles:true}));
                          return true;
                        }
                      }
                    }
                    return false;
                  }
                """, target_div)
                if picked:
                    log(f"[filter] Division Office → {target_div} (direct any-select set after wait)")
                else:
                    await snap(page, "fail_division.png")
                    raise RuntimeError("Could not set Division Office (all methods)")

            # 3) Nature of Application → select all if present
            await bs_select_all_if_present(panel, page, "Nature Of Application")

            # 4) Status
            if not await select_option_any(panel, page, "Status", status_text):
                await snap(page, f"fail_status_{status_text}.png")
                raise RuntimeError(f"Could not set Status='{status_text}' (all methods)")

            # 5) Dates 26/07/2024 → today + show
            ok_rows = await set_dates_and_show(panel, settle_ms_first=5600)
            if not ok_rows:
                await snap(page, f"fail_rows_{status_text}.png", full=True)
                raise RuntimeError(f"No data rows after filters+dates for status {status_text}")

            await snap(page, f"after_grid_shown_{status_text.lower()}.png")

            # 6) Export PDF (download → capture&replay → screenshot fallback)
            save_path = OUT / f"{filename} {stamp}.pdf"
            ok, size = await download_pdf_via_capture_and_replay(panel, save_path, settle_ms=5600)
            if (not ok) or (size < MIN_VALID_PDF_BYTES):
                if ok:
                    log(f"[pdf] replay still small ({size} bytes < {MIN_VALID_PDF_BYTES}); using screenshot→PDF fallback.")
                else:
                    log("[pdf] could not obtain via replay; using screenshot→PDF fallback.")
                await render_panel_via_screenshot_to_pdf(panel, save_path)

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

