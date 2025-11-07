#!/usr/bin/env python3
import os, sys, asyncio, traceback, base64, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

from dotenv import load_dotenv
load_dotenv()

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / "out"
OUT.mkdir(exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))
MIN_VALID_PDF_BYTES = 50000

def log(m): print(m, flush=True)
def today_ist(): return datetime.now(IST)
def today_ddmmyyyy(): return today_ist().strftime("%d/%m/%Y")
def today_fname(): return today_ist().strftime("%d-%m-%Y")

async def wait_ms(ms): await asyncio.sleep(ms / 1000.0)

async def panel_has_data(panel):
    try:
        return await panel.evaluate("""(root)=>{
          const tb = root.querySelector('#myTable') || root.querySelector('table');
          if(!tb) return false;
          const rows = Array.from(tb.querySelectorAll('tbody tr'));
          if(rows.length===0) return false;
          for (const tr of rows){
            const tds=[...tr.querySelectorAll('td')].map(td=>(td.innerText||'').trim());
            if (tds.length>=2 && tds.some(Boolean)) return true;
          }
          const txt=(tb.innerText||'').toLowerCase();
          if (txt.includes('no data available')) return false;
          return false;
        }""")
    except Exception:
        return False

# ------------------ Dropdown Selection Logic ------------------
SET_SELECT_VALUES = r"""
(root, cfg) => {
  const { candidates, values, selectAll, exact } = cfg;
  const norm = s => (s||'').trim();
  const lower = s => norm(s).toLowerCase();

  let el = null;
  for (const q of candidates) {
    const e = root.querySelector(q) || document.querySelector(q);
    if (e) { el = e; break; }
  }
  if (!el) return {ok:false, reason:'select not found', selected:[]};

  const hasJQ = !!window.jQuery;
  const isSelectPicker = hasJQ && (typeof window.jQuery(el).selectpicker === 'function');
  const isMultiSelect  = hasJQ && (typeof window.jQuery(el).multiselect   === 'function');

  const pickTexts = Array.isArray(values) ? values : [];
  const want = pickTexts.map(v => exact ? norm(v) : lower(v));

  const allOpts = Array.from(el.options || []).map(o => ({
    text: norm(o.textContent||o.label||''),
    value: o.value
  }));

  const matchValues = () => {
    if (selectAll) return allOpts.map(o => o.value);
    const picked = [];
    for (const w of want) {
      const hit = allOpts.find(o => exact ? o.text === w : lower(o.text).includes(w));
      if (hit) picked.push(hit.value);
    }
    return picked;
  };

  try {
    if (isSelectPicker) {
      const $el = window.jQuery(el);
      const vals = matchValues();
      try { $el.selectpicker('deselectAll'); } catch(e){}
      if (selectAll) {
        $el.selectpicker('val', allOpts.map(o=>o.value));
      } else {
        $el.selectpicker('val', vals);
      }
      try{$el.selectpicker('render');}catch(e){}
      try{$el.selectpicker('refresh');}catch(e){}
      el.dispatchEvent(new Event('change',{bubbles:true}));
    } else if (isMultiSelect) {
      const $el = window.jQuery(el);
      try { $el.multiselect('deselectAll', false); } catch(e){}
      if (selectAll) {
        try { $el.multiselect('selectAll', false); } catch(e){}
      } else {
        const vals = matchValues();
        try { $el.multiselect('select', vals); } catch(e){}
      }
      try { $el.multiselect('refresh'); } catch(e){}
      try { $el.multiselect('updateButtonText'); } catch(e){}
      el.dispatchEvent(new Event('change',{bubbles:true}));
    } else {
      // Native select
      const vals = matchValues();
      const setAll = new Set(vals);
      if (el.multiple) {
        for (const o of el.options) o.selected = selectAll ? true : setAll.has(o.value);
      } else {
        for (const o of el.options) o.selected = false;
        if (vals[0] != null) {
          const v = vals[0];
          const targ = Array.from(el.options).find(o => o.value === v);
          if (targ) targ.selected = true;
        }
      }
      el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
    }
  } catch(e) {}

  try { document.body.click(); } catch(e) {}
  try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch(e) {}

  const selected = Array.from(el.selectedOptions||[]).map(o=>norm(o.textContent||o.label||o.value||'')).filter(Boolean);
  return {ok: selected.length>0, selected};
}
"""

READ_SELECTED = r"""
(root, candidates) => {
  const norm = s => (s||'').trim();
  let el = null;
  for (const q of candidates) {
    const e = root.querySelector(q) || document.querySelector(q);
    if (e) { el = e; break; }
  }
  if (!el) return [];
  return Array.from(el.selectedOptions||[]).map(o=>norm(o.textContent||o.label||o.value||'')).filter(Boolean);
}
"""

async def set_dropdown(panel, candidates, *, values=None, select_all=False, exact=False, label=""):
    res = await panel.evaluate(SET_SELECT_VALUES, {
        "candidates": candidates,
        "values": values or [],
        "selectAll": bool(select_all),
        "exact": bool(exact)
    })
    picked = res.get("selected") if isinstance(res, dict) else []
    ok = bool(res and res.get("ok"))
    log(f"[filter] {label or candidates[0]} → {picked if picked else 'None'} (ok={ok})")
    return ok

# ------------------ Site Hooks ------------------
CALL_DIVISION_LIST = r"""(root)=>{ try{ if(typeof window.DivisionList==='function') window.DivisionList(); }catch(e){} return true; }"""

FILL_DATES_JS = r"""
(root, cfg) => {
  const { fromDDMMYYYY, toDDMMYYYY } = cfg;
  const set = (q, v) => {
    const e = root.querySelector(q) || document.querySelector(q);
    if (!e) return false;
    e.value = v;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
    return true;
  };
  const F = ['#period_from','#fromDate','input[name="fromDate"]','input[name*="fromdate" i]'];
  const T = ['#period_to','#toDate','input[name="toDate"]','input[name*="todate" i]'];
  let okF=false, okT=false;
  for(const q of F){ if(set(q, fromDDMMYYYY)){okF=true; break;} }
  for(const q of T){ if(set(q, toDDMMYYYY)){okT=true; break;} }
  return {okFrom:okF, okTo:okT};
}
"""

async def click_show_report(panel):
    for sel in [
        "button:has-text('Show Report')",
        "input[type='button'][value='Show Report']",
        "input[type='submit'][value='Show Report']",
    ]:
        try:
            btn = panel.locator(sel).first
            if await btn.count():
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=6000)
                return True
        except Exception:
            pass
    return await panel.evaluate("""(root)=>{
      const norm=s=>(s||'').trim().toLowerCase();
      const btn=[...root.querySelectorAll('button,input[type=button],input[type=submit]')]
        .find(b=>norm(b.innerText||b.value||'').includes('show report'));
      if(!btn) return false; btn.click(); return true;
    }""")

# ------------------ PDF helpers ------------------
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

async def click_and_wait_download(page, click_pdf, save_path: Path, timeout_ms=35000):
    log("[pdf] trying direct download…")
    try:
        async with page.expect_download(timeout=timeout_ms) as info:
            await click_pdf()
        dl = await info.value
        await dl.save_as(str(save_path))
        log(f"[pdf] saved → {save_path}")
        return True
    except Exception as e:
        log(f"[pdf] direct download failed ({e})")
        return False

async def render_dom_table_pdf(panel, pdf_path: Path):
    payload = await panel.evaluate("""(root)=>{
      const norm=s=>(s||'').trim();
      const sel = (cands)=>{
        for (const q of cands) {
          const el = root.querySelector(q) || document.querySelector(q);
          if (el) return Array.from(el.selectedOptions||[]).map(o=>norm(o.textContent||o.label||o.value||'')).filter(Boolean);
        }
        return [];
      };
      const table = root.querySelector('#myTable') || root.querySelector('table');
      const tableHTML = table ? table.outerHTML : '';
      return {
        filters:{
          circle: sel(['#circle_office','#circle','#circleId']),
          division: sel(['#division_office','#division','#divisionId']),
          nature: sel(['#nature_of_application','#nature','#natureId']),
          status: sel(['#status','#statusId']),
        },
        tableHTML
      };
    }""")
    table_html = (payload or {}).get("tableHTML") or ""
    if not table_html:
        png = await panel.screenshot(type="png")
        b64 = base64.b64encode(png).decode("ascii")
        ctx = panel.page.context
        tmp = await ctx.new_page()
        html = f"""<!doctype html><html><body><img src="data:image/png;base64,{b64}" style="width:100%"></body></html>"""
        await tmp.set_content(html, wait_until="load")
        await tmp.pdf(path=str(pdf_path), format="A4", print_background=True)
        await tmp.close()
        log(f"[pdf:fallback] screenshot → {pdf_path}")
        return

    f = (payload or {}).get("filters") or {}
    def line(name, arr): return f"<li><b>{name}:</b> {', '.join(arr)}</li>" if arr else ""
    head = f"""<h1>Application Wise Report</h1><ul>
      {line("Circle Office", f.get("circle"))}
      {line("Division Office", f.get("division"))}
      {line("Nature Of Application", f.get("nature"))}
      {line("Status", f.get("status"))}</ul>"""

    html = f"""<!doctype html><html><head><meta charset="utf-8"/><style>
      @page {{ size: A4 landscape; margin: 10mm; }}
      body {{ font: 12px Arial, Helvetica, sans-serif; }}
      table {{ width:100%; border-collapse:collapse; }}
      th,td {{ border:1px solid #999; padding:6px 8px; word-break:break-word; }}
      th {{ background:#f2f2f2; }}
    </style></head><body>{head}{table_html}</body></html>"""
    tmp = await panel.page.context.new_page()
    await tmp.set_content(html, wait_until="load")
    await tmp.pdf(path=str(pdf_path), format="A4", print_background=True, landscape=True)
    await tmp.close()
    log(f"[pdf:dom] table-only → {pdf_path}")

# ------------------ Main Flow ------------------
async def site_login_and_download():
    login_url = os.getenv("LOGIN_URL", "https://esinchai.punjab.gov.in/signup.jsp")
    username = os.environ["USERNAME"]
    password = os.environ["PASSWORD"]
    user_type = os.getenv("USER_TYPE", "").strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        log(f"Opening login page: {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded")

        if user_type:
            for sel in ["select#usertype","select#userType","select[name='userType']"]:
                if await page.locator(sel).count():
                    await page.select_option(sel, label=user_type)
                    break

        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("button:has-text('Login')")
        await page.wait_for_load_state("domcontentloaded")
        log("Login step complete.")
        log(f"Current URL: {page.url}")

        panel = page.locator(
            "xpath=//div[.//text()[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'application wise report')]]"
        ).first
        await panel.wait_for(state="visible", timeout=20000)
        log("[nav] Application Wise Report panel ready.")

        circle_cands = ['#circle_office','#circle']
        division_cands = ['#division_office','#division']
        nature_cands = ['#nature_of_application','#nature']
        status_cands = ['#status']

        async def run_one(status_text: str, base_name: str):
            ok = await set_dropdown(panel, circle_cands, values=["LUDHIANA CANAL CIRCLE"], exact=True, label="Circle Office")
            if not ok: raise RuntimeError("Circle Office selection failed")
            try: await panel.evaluate(CALL_DIVISION_LIST)
            except Exception: pass

            await wait_ms(800)
            ok = await set_dropdown(panel, division_cands, values=["FARIDKOT CANAL AND GROUND WATER DIVISION"], exact=True, label="Division Office")
            if not ok: raise RuntimeError("Division Office selection failed")

            ok = await set_dropdown(panel, nature_cands, select_all=True, label="Nature Of Application (Select All)")
            if not ok: raise RuntimeError("Nature selection failed")

            ok = await set_dropdown(panel, status_cands, values=[status_text], exact=True, label="Status")
            if not ok: raise RuntimeError("Status selection failed")

            res = await panel.evaluate(FILL_DATES_JS, {"fromDDMMYYYY":"26/07/2024","toDDMMYYYY":today_ddmmyyyy()})
            log(f"[dates] set -> {res}")

            if not await click_show_report(panel):
                raise RuntimeError("Show Report not found")

            for _ in range(40):
                if await panel_has_data(panel): break
                await wait_ms(250)
            if not await panel_has_data(panel):
                raise RuntimeError("No data rows after report")

            pdf_path = OUT / f"{base_name} {today_fname()}.pdf"
            async def do_click():
                if not await click_pdf_icon(panel):
                    raise RuntimeError("PDF icon not found")

            got = await click_and_wait_download(page, do_click, pdf_path)
            size = pdf_path.stat().st_size if os.path.exists(pdf_path) else 0
            if not got or size < MIN_VALID_PDF_BYTES:
                await render_dom_table_pdf(panel, pdf_path)
            log(f"Saved {pdf_path.name}")
            return str(pdf_path)

        a = await run_one("DELAYED", "Delayed Apps")
        b = await run_one("PENDING", "Pending Apps")
        await context.close(); await browser.close()
        return [a, b]

# ------------------ Entry ------------------
async def main():
    files = await site_login_and_download()
    log("Downloads complete: " + ", ".join(Path(f).name for f in files))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
