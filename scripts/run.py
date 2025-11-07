#!/usr/bin/env python3
import os, sys, asyncio, traceback, re, base64, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

DEBUG = os.getenv("DEBUG", "0") == "1"
IST = timezone(timedelta(hours=5, minutes=30))
MIN_VALID_PDF_BYTES = 50000

def log(msg): print(msg, flush=True)

def ist_now():
    return datetime.now(IST)

def fname_today():
    return ist_now().strftime("%d-%m-%Y")

def today_variants():
    d = ist_now()
    return (d.strftime("%d/%m/%Y"), d.strftime("%Y-%m-%d"), d.strftime("%d-%m-%Y"))

FROM_FIXED = ("26/07/2024", "2024-07-26", "26-07-2024")

async def snap(page, name, full=False):
    if not DEBUG: return
    try: await page.screenshot(path=str(OUT / name), full_page=bool(full))
    except Exception: pass

# ---------------- Helpers that operate on the real <select> ----------------

SET_MULTI_JS = r"""
(root, cfg) => {
  const { sel, wantedTexts, exact, selectAll } = cfg; // wantedTexts: array of strings
  const norm = s => (s||'').trim();
  const lower = s => norm(s).toLowerCase();

  // find the select (prefer exact ID if given like "#circle_office")
  let el = null;
  if (sel) el = root.querySelector(sel) || document.querySelector(sel);
  if (!el) return { ok:false, reason:'select not found' };

  // If "selectAll", mark everything selected
  if (selectAll) {
    for (const o of el.options) o.selected = true;
  } else {
    const want = (wantedTexts||[]).map(lower);
    for (const o of el.options) {
      const t = lower(o.textContent||o.label||o.value||'');
      const hit = exact ? want.includes(lower(o.textContent||o.label||'')) : want.some(w=>t.includes(w));
      o.selected = hit;
    }
  }

  // Dispatch events so the site reacts
  const fire = (node) => {
    node.dispatchEvent(new Event('input', {bubbles:true}));
    node.dispatchEvent(new Event('change', {bubbles:true}));
    try { node.dispatchEvent(new Event('changed.bs.select', {bubbles:true})); } catch(e) {}
  };
  fire(el);

  // If bootstrap-multiselect is present, refresh the widget
  try {
    if (window.jQuery && (window.jQuery(el).multiselect)) {
      window.jQuery(el).multiselect('refresh');
    }
  } catch(e) {}

  // Try to close any open dropdown (click body)
  try { document.body.click(); } catch(e) {}
  try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch(e) {}

  // Read back selected option texts to verify
  const selTexts = Array.from(el.selectedOptions || [])
    .map(o => norm(o.textContent||o.label||o.value||''))
    .filter(Boolean);

  return { ok: selTexts.length > 0, selected: selTexts };
}
"""

READ_SELECTED_TEXTS_JS = r"""
(root, sel) => {
  const el = root.querySelector(sel) || document.querySelector(sel);
  if (!el) return [];
  const norm = s => (s||'').trim();
  return Array.from(el.selectedOptions || [])
    .map(o => norm(o.textContent||o.label||o.value||''))
    .filter(Boolean);
}
"""

async def select_values_by_text(panel, selector, values, *, exact=False, select_all=False, label=""):
    res = await panel.evaluate(SET_MULTI_JS, {
        "sel": selector,
        "wantedTexts": values or [],
        "exact": bool(exact),
        "selectAll": bool(select_all)
    })
    ok = bool(res and res.get("ok"))
    picked = res.get("selected") if isinstance(res, dict) else []
    log(f"[filter] {label or selector} → {picked} (ok={ok})")
    # small pause gives time for dependent XHRs (e.g., circle→division)
    await asyncio.sleep(0.3)
    return ok

async def read_selected_texts(panel, selector):
    try:
        return await panel.evaluate(READ_SELECTED_TEXTS_JS, selector)
    except Exception:
        return []

# ---------------- Wait for Division list after Circle ----------------

async def wait_for_division_option(page, division_text, timeout_ms=20000):
    end = time.time() + timeout_ms/1000.0
    target = division_text.lower()
    while time.time() < end:
        try:
            exists = await page.evaluate(r"""
                (t) => {
                  const norm = s => (s||'').trim().toLowerCase();
                  const el = document.querySelector('#division_office');
                  if (!el) return false;
                  for (const o of el.options) {
                    if (norm(o.textContent).includes(norm(t))) return true;
                  }
                  return false;
                }
            """, division_text)
            if exists: return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False

# ---------------- Dates + Show Report ----------------

FILL_DATES_JS = r"""
(root, cfg) => {
  const { fromVal, toVal } = cfg;
  const set = (sel, v) => {
    const el = root.querySelector(sel) || document.querySelector(sel);
    if (!el) return false;
    el.value = v;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    return true;
  };
  const fromCands = ['#fromDate','input[name="fromDate"]','input[name*="fromdate" i]','input[placeholder*="From" i]','#period_from','input#period_from'];
  const toCands   = ['#toDate','input[name="toDate"]','input[name*="todate" i]','input[placeholder*="To" i]','#period_to','input#period_to'];

  let okF=false, okT=false;
  for (const c of fromCands) if (set(c, fromVal)) { okF=true; break; }
  for (const c of toCands)   if (set(c, toVal))   { okT=true; break; }

  return { okFrom: okF, okTo: okT };
}
"""

async def panel_has_rows(panel):
    try:
        return await panel.evaluate(r"""
          (root)=>{
            const tbs = root.querySelectorAll('table tbody');
            for (const tb of tbs) {
              for (const tr of tb.querySelectorAll('tr')) {
                const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText||'').trim());
                if (cells.filter(Boolean).length >= 2) return true;
              }
            }
            return false;
          }
        """)
    except Exception:
        return False

async def show_report(panel, settle_ms=5600):
    # Click Show Report
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
        clicked = await panel.evaluate(r"""
            (root)=>{
              const norm = s => (s||'').trim().toLowerCase();
              const b = [...root.querySelectorAll('button,input[type=button],input[type=submit]')]
                .find(x => norm(x.innerText||x.value||'') === 'show report' || norm(x.innerText||x.value||'').includes('show report'));
              if (!b) return false; b.click(); return true;
            }
        """)
    if not clicked:
        raise RuntimeError("Show Report not found")

    # Wait a reasonable time for request to complete
    try:
        await panel.page.wait_for_response(
            lambda r: any(k in (r.url or '').lower() for k in ("applicationwisereport","report","getreport","appwisereport")),
            timeout=12000
        )
    except Exception:
        pass

    await asyncio.sleep(settle_ms/1000.0)
    return await panel_has_rows(panel)

# ---------------- PDF capture (server → replay → DOM clean → screenshot) ----------------

async def click_pdf_icon(panel):
    for sel in [
        "xpath=.//img[contains(@src,'pdf') or contains(@alt,'PDF')]",
        "xpath=(.//a[.//img[contains(@src,'pdf') or contains(@alt,'PDF')]])[1]"
    ]:
        ico = panel.locator(sel).first
        if await ico.count():
            await ico.scroll_into_view_if_needed()
            await ico.click(timeout=6000, force=True)
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

class RequestSniffer:
    def __init__(self, page):
        self.page = page
        self.events = []
        self._rq = None
        self._rs = None
    async def __aenter__(self):
        async def on_req(r):
            try:
                self.events.append({"t":"req","url":r.url,"m":r.method,"h":dict(r.headers),"d":r.post_data()})
            except Exception: pass
        async def on_res(s):
            try:
                self.events.append({"t":"res","url":s.url,"s":s.status,"h":dict(s.headers)})
            except Exception: pass
        self._rq = self.page.on("request", on_req)
        self._rs = self.page.on("response", on_res)
        return self
    async def __aexit__(self, *a):
        if self._rq: self.page.off("request", self._rq)
        if self._rs: self.page.off("response", self._rs)
    def find_pdf_pair(self):
        res = None
        for ev in reversed(self.events):
            if ev.get("t")=="res":
                h = {k.lower():v for k,v in (ev.get("h") or {}).items()}
                ct = (h.get("content-type","") or "").lower()
                if "pdf" in ct or "application/pdf" in ct or "export" in (ev.get("url","").lower()):
                    res = ev; break
        if not res: return (None, None)
        req = None
        for ev in reversed(self.events):
            if ev.get("t")=="req" and ev.get("url")==res.get("url"):
                req = ev; break
        return (req, res)

async def replay_pdf(context, req_ev, save_path: Path):
    if not req_ev: return False
    url = req_ev.get("url"); method = (req_ev.get("m") or "GET").upper()
    headers = dict(req_ev.get("h") or {})
    data = req_ev.get("d")
    # strip browser headers that break server expectations
    for k in ["content-length","origin","referer","cookie","user-agent","sec-fetch-site","sec-fetch-mode","sec-fetch-dest","sec-ch-ua","sec-ch-ua-mobile","sec-ch-ua-platform"]:
        headers.pop(k, None)
    try:
        if method=="POST":
            resp = await context.request.post(url, data=data, headers=headers)
        else:
            resp = await context.request.get(url, headers=headers)
    except Exception as e:
        log(f"[replay] error: {e}"); return False
    if not resp.ok:
        log(f"[replay] HTTP {resp.status}"); return False
    ct = (resp.headers.get("content-type","") or "").lower()
    if "pdf" not in ct: log(f"[replay] not a PDF (ct={ct})"); return False
    b = await resp.body()
    Path(save_path).write_bytes(b)
    log(f"[replay] saved → {save_path} ({len(b)} bytes)")
    return True

async def render_panel_screenshot_pdf(panel, pdf_path: Path):
    png = await panel.screenshot(type="png")
    b64 = base64.b64encode(png).decode("ascii")
    ctx = panel.page.context
    tmp = await ctx.new_page()
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html,body{{margin:0}} .wrap{{padding:8mm}} img{{width:100%;height:auto}}
    </style></head><body><div class="wrap">
      <img src="data:image/png;base64,{b64}" />
    </div></body></html>"""
    await tmp.set_content(html, wait_until="load")
    await tmp.emulate_media(media="print")
    await tmp.pdf(path=str(pdf_path), format="A4", print_background=True)
    await tmp.close()
    log(f"[pdf:fallback] screenshot → {pdf_path}")

async def render_dom_table_pdf(panel, pdf_path: Path):
    payload = await panel.evaluate(r"""
      (root)=>{
        const norm = s => (s||'').trim();
        const selTexts = (sel) => {
          const el = root.querySelector(sel) || document.querySelector(sel);
          if (!el) return [];
          return Array.from(el.selectedOptions||[]).map(o => norm(o.textContent||o.label||o.value||'')).filter(Boolean);
        };
        let table = root.querySelector('#myTable');
        if (!table) {
          const all = Array.from(root.querySelectorAll('table'));
          table = all.find(t => t.querySelector('tbody tr td')) || all[0] || null;
        }
        const tableHTML = table ? table.outerHTML : '';

        const filters = {
          circle: selTexts('#circle_office'),
          division: selTexts('#division_office'),
          nature: selTexts('#nature_of_application'),
          status: selTexts('#status'),
        };

        // period line if present
        let period = '';
        const any = Array.from(root.querySelectorAll('*')).find(n => (n.textContent||'').toLowerCase().includes('period:'));
        if (any) period = (any.textContent||'').trim();

        // dates if inputs exist
        const val = (q) => {
          const e = root.querySelector(q) || document.querySelector(q);
          return e && e.value ? e.value.trim() : '';
        };
        const fromD = val('#fromDate')||val('input[name="fromDate"]')||val('#period_from');
        const toD   = val('#toDate')  ||val('input[name="toDate"]')  ||val('#period_to');

        return { tableHTML, filters, period, dates:{fromD, toD} };
      }
    """)
    table_html = (payload or {}).get("tableHTML") or ""
    if not table_html:
        await render_panel_screenshot_pdf(panel, pdf_path); return

    filters = (payload or {}).get("filters") or {}
    period = (payload or {}).get("period") or ""
    dates = (payload or {}).get("dates") or {}

    # Build clean header from real selected options (not the button text)
    def line(name, arr):
        if not arr: return ""
        return f"<li><strong>{name}:</strong> {', '.join(arr)}</li>"

    f_html = "".join([
        line("Circle Office", filters.get("circle")),
        line("Division Office", filters.get("division")),
        line("Nature Of Application", filters.get("nature")),
        line("Status", filters.get("status")),
    ])
    filters_block = f"<ul>{f_html}</ul>" if f_html else ""

    if not period:
        fD = dates.get("fromD") or "26/07/2024"
        tD = dates.get("toD") or ""
        if tD:
            period = f"Period: From {fD} to {tD}"
        else:
            period = f"Period: From {fD}"

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/>
<style>
  @page {{ size: A4 landscape; margin: 10mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 12px; color:#111; }}
  h1 {{ font-size: 18px; margin:0 0 6px 0; }}
  .meta {{ margin: 6px 0 10px 0; }}
  ul {{ margin: 6px 0 8px 18px; }}
  table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
  th, td {{ border:1px solid #999; padding:6px 8px; vertical-align:top; word-break:break-word; }}
  th {{ background:#f2f2f2; }}
</style></head>
<body>
  <h1>Application Wise Report</h1>
  <div class="meta"><strong>{period}</strong></div>
  {filters_block}
  {table_html}
</body></html>"""

    tmp = await panel.page.context.new_page()
    await tmp.set_content(html, wait_until="load")
    await tmp.emulate_media(media="print")
    await tmp.pdf(path=str(pdf_path), format="A4", print_background=True, landscape=True)
    await tmp.close()
    log(f"[pdf:dom] table-only → {pdf_path}")

async def try_download_pdf(panel, save_path: Path):
    page = panel.page
    async def do_click():
        ok = await click_pdf_icon(panel)
        if not ok: raise RuntimeError("PDF icon not found")
    size = 0
    ok = False
    async with RequestSniffer(page) as sn:
        ok = await click_and_wait_download(page, do_click, str(save_path), timeout_ms=35000)
        if ok:
            try: size = Path(save_path).stat().st_size
            except FileNotFoundError: size = 0
            log(f"[pdf] size: {size} bytes")
        if (not ok) or (size < MIN_VALID_PDF_BYTES):
            req, _ = sn.find_pdf_pair()
            if req:
                rep = await replay_pdf(page.context, req, save_path)
                if rep:
                    try: size = Path(save_path).stat().st_size
                    except FileNotFoundError: size = 0
                    ok = True
    return ok, size

# ---------------- Main flow ----------------

async def get_app_panel(page):
    panel = page.locator(
        "xpath=//div[.//text()[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'application wise report')]]"
    ).first
    await panel.wait_for(state="visible", timeout=15000)
    return panel

async def set_dates_and_show(panel):
    to_opts = list(today_variants())
    tries = [(f, t) for f in FROM_FIXED for t in to_opts]
    for i,(f,t) in enumerate(tries,1):
        res = await panel.evaluate(FILL_DATES_JS, {"fromVal": f, "toVal": t})
        log(f"[dates] try {i}: from='{f}' to='{t}' set={res}")
        ok = await show_report(panel, settle_ms=5600 if i==1 else 3000)
        if ok:
            log(f"[dates] data present with try {i}")
            return True
        log(f"[dates] no rows on try {i}")
    return False

async def site_login_and_download():
    login_url   = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username    = os.environ["USERNAME"]
    password    = os.environ["PASSWORD"]
    user_type   = os.getenv("USER_TYPE", "").strip()
    stamp       = fname_today()

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

        async def speed_filter(route, request):
            # keep css/images (date widgets depend on them); block only fonts
            if request.resource_type in ("font",):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", speed_filter)

        context.set_default_timeout(20000)
        context.set_default_navigation_timeout(60000)

        page = await context.new_page()

        # ---- Login ----
        log(f"Opening login page: {login_url}")
        last_err = None
        for _ in range(2):
            try:
                await page.goto(login_url, wait_until="domcontentloaded"); break
            except PWTimeout as e: last_err = e
        if last_err: raise last_err

        if user_type:
            for sel in ["select#usertype","select#userType","select[name='userType']","select#user_type"]:
                if await page.locator(sel).count():
                    try: await page.select_option(sel, value=user_type); break
                    except Exception:
                        try: await page.select_option(sel, label=user_type); break
                        except Exception: pass

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

        # ---- Navigate to Application Wise Report ----
        try:
            await page.get_by_text("MIS Reports", exact=False).first.click(timeout=9000)
        except Exception:
            await page.locator("a:has-text('MIS Reports'), button:has-text('MIS Reports'), li:has-text('MIS Reports')").first.click(timeout=9000)
        await asyncio.sleep(0.2)

        await page.get_by_text("Application Wise Report", exact=False).first.click(timeout=12000)
        await page.wait_for_load_state("domcontentloaded")
        panel = await get_app_panel(page)
        log("[nav] Application Wise Report panel ready.")

        # ---- One run (DELAYED / PENDING) ----
        async def run_one(status_text: str, fname: str):
            # Circle
            ok = await select_values_by_text(panel, "#circle_office",
                                             ["LUDHIANA CANAL CIRCLE"],
                                             exact=True, select_all=False, label="Circle Office")
            if not ok: raise RuntimeError("Circle Office could not be set")

            # Wait for Division list to load, then Division
            target_div = "FARIDKOT CANAL AND GROUND WATER DIVISION"
            log("[wait] waiting division options…")
            await wait_for_division_option(page, target_div, timeout_ms=25000)

            ok = await select_values_by_text(panel, "#division_office",
                                             [target_div], exact=True, select_all=False, label="Division Office")
            if not ok: raise RuntimeError("Division Office could not be set")

            # Nature — Select All
            ok = await select_values_by_text(panel, "#nature_of_application",
                                             [], select_all=True, label="Nature Of Application (Select All)")
            if not ok: raise RuntimeError("Nature Of Application could not be set")

            # Status
            ok = await select_values_by_text(panel, "#status",
                                             [status_text], exact=True, select_all=False, label="Status")
            if not ok: raise RuntimeError("Status could not be set")

            # Dates and Show
            rows_ok = await set_dates_and_show(panel)
            if not rows_ok:
                await snap(page, f"no_rows_{status_text}.png", full=True)
                raise RuntimeError(f"No data rows after Show Report ({status_text})")

            await snap(page, f"after_grid_{status_text}.png")

            save_path = OUT / f"{fname} {fname_today()}.pdf"

            # Try server download/replay
            ok, size = await try_download_pdf(panel, save_path)
            need_dom = True
            if ok and size >= MIN_VALID_PDF_BYTES:
                need_dom = False
            else:
                if ok:
                    log(f"[pdf] server PDF small ({size} < {MIN_VALID_PDF_BYTES}); will render DOM.")
                else:
                    log("[pdf] server PDF unavailable; will render DOM.")

            if need_dom:
                try:
                    await render_dom_table_pdf(panel, save_path)
                except Exception as e:
                    log(f"[pdf:dom] failed ({e}); using screenshot fallback.")
                    await render_panel_screenshot_pdf(panel, save_path)

            log(f"Saved {save_path.name}")
            return str(save_path)

        a = await run_one("DELAYED", "Delayed Apps")
        b = await run_one("PENDING", "Pending Apps")

        await context.close(); await browser.close()
        return [a, b]

# ---------------- Telegram ----------------

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

# ---------------- Entry ----------------

async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join(Path(f).name for f in files))
    try:
        await send_via_telegram(files)
    except Exception as e:
        log(f"Telegram send error (continuing): {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc(); sys.exit(1)
