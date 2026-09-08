
import os
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Tomasz Trading Journal")
print("LOADED MAIN.PY FROM:", __file__, flush=True)

for route in app.routes:
    print("ROUTE AT START:", getattr(route, "path", "?"), flush=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
JOURNAL_KEY = os.getenv("JOURNAL_KEY", "")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "trade-screenshots")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not JOURNAL_KEY:
    print("WARNING: Set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and JOURNAL_KEY env vars.")

def auth_headers(extra: Optional[dict] = None) -> dict:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    if extra:
        headers.update(extra)
    return headers

def check_key(key: str):
    if not JOURNAL_KEY or not secrets.compare_digest(key, JOURNAL_KEY):
        raise HTTPException(status_code=403, detail="Invalid journal key")

async def sb_request(method: str, path: str, **kwargs):
    url = f"{SUPABASE_URL}{path}"
    headers = kwargs.pop("headers", {})
    merged = auth_headers(headers)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, url, headers=merged, **kwargs)
    if r.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"Supabase error: {r.status_code} {r.text[:500]}")
    return r

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/journal/{key:path}", response_class=HTMLResponse)
def journal_page(key: str):
    check_key(key)
    safe_key = json.dumps(key)
    html = JOURNAL_HTML.replace("__SAFE_KEY__", safe_key)
    return HTMLResponse(html)

@app.get("/api/trades/{key:path}")
async def list_trades(
    key: str,
    instrument: str = Query(default=""),
    side: str = Query(default=""),
    setup: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
):
    check_key(key)
    params = {
        "select": "*",
        "order": "trade_time.desc",
        "limit": str(limit),
    }
    if instrument:
        params["instrument"] = f"eq.{instrument}"
    if side:
        params["side"] = f"eq.{side}"
    if setup:
        params["setup"] = f"eq.{setup}"

    r = await sb_request("GET", "/rest/v1/trades", params=params)
    items = r.json()

    qn = q.strip().lower()
    if qn:
        def matches(t):
            blob = " ".join([
                str(t.get("instrument", "")),
                str(t.get("setup", "")),
                str(t.get("notes", "")),
                str(t.get("lesson", "")),
                str(t.get("tags", "")),
            ]).lower()
            return qn in blob
        items = [x for x in items if matches(x)]

    for item in items:
        if item.get("screenshot_path"):
            item["screenshot_url"] = f"/journal-image/{key}?path={item['screenshot_path']}"
        else:
            item["screenshot_url"] = None
    return {"items": items}

@app.get("/api/options/{key:path}")
async def options(key: str):
    check_key(key)
    r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"select": "instrument,setup", "limit": "1000"},
    )
    rows = r.json()
    instruments = sorted({str(x.get("instrument")).strip() for x in rows if x.get("instrument")})
    setups = sorted({str(x.get("setup")).strip() for x in rows if x.get("setup")})
    return {"instruments": instruments, "setups": setups}

@app.get("/api/stats/{key:path}")
async def stats(key: str):
    check_key(key)
    r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"select": "pnl", "limit": "5000"},
    )
    rows = r.json()
    vals = [float(x.get("pnl") or 0) for x in rows]
    trades = len(vals)
    total = sum(vals)
    wins = sum(1 for x in vals if x > 0)
    losses = sum(1 for x in vals if x < 0)
    return {
        "trades": trades,
        "total_pnl": total,
        "win_rate": (wins / trades * 100) if trades else 0,
        "avg_trade": (total / trades) if trades else 0,
        "wins": wins,
        "losses": losses,
    }

async def upload_screenshot(file: UploadFile) -> str:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".jpg"
    path = f"{datetime.now(timezone.utc).strftime('%Y/%m')}/{uuid.uuid4().hex}{ext}"
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Screenshot too large (max 12 MB).")

    await sb_request(
        "POST",
        f"/storage/v1/object/{STORAGE_BUCKET}/{path}",
        content=data,
        headers={
            "Content-Type": file.content_type or "application/octet-stream",
            "x-upsert": "false",
        },
    )
    return path

async def delete_screenshot(path: str):
    if not path:
        return
    await sb_request(
        "DELETE",
        f"/storage/v1/object/{STORAGE_BUCKET}/{path}",
    )

@app.get("/journal-image/{key:path}")
async def journal_image(key: str, path: str = Query(...)):
    check_key(key)
    r = await sb_request("GET", f"/storage/v1/object/{STORAGE_BUCKET}/{path}")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=300"},
    )

@app.post("/api/trades/{key:path}")
async def create_trade(
    key: str,
    instrument: str = Form(...),
    side: str = Form(...),
    trade_time: str = Form(...),
    setup: str = Form(""),
    entry: str = Form(""),
    exit: str = Form(""),
    qty: str = Form(""),
    pnl: str = Form("0"),
    rating: str = Form(""),
    tags: str = Form(""),
    notes: str = Form(""),
    lesson: str = Form(""),
    screenshot: Optional[UploadFile] = File(default=None),
):
    check_key(key)

    screenshot_path = None
    if screenshot and screenshot.filename:
        screenshot_path = await upload_screenshot(screenshot)

    payload = {
        "instrument": instrument.strip().upper(),
        "side": side.strip().upper(),
        "trade_time": trade_time,
        "setup": setup.strip(),
        "entry": float(entry) if entry.strip() else None,
        "exit": float(exit) if exit.strip() else None,
        "qty": int(qty) if qty.strip() else None,
        "pnl": float(pnl or 0),
        "rating": int(rating) if rating.strip() else None,
        "tags": tags.strip(),
        "notes": notes.strip(),
        "lesson": lesson.strip(),
        "screenshot_path": screenshot_path,
    }

    r = await sb_request(
        "POST",
        "/rest/v1/trades",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    return {"ok": True, "trade": r.json()[0] if r.json() else payload}

@app.put("/api/trades/{key:path}")
async def update_trade(
    key: str,
    trade_id: str = Form(...),
    instrument: str = Form(...),
    side: str = Form(...),
    trade_time: str = Form(...),
    setup: str = Form(""),
    entry: str = Form(""),
    exit: str = Form(""),
    qty: str = Form(""),
    pnl: str = Form("0"),
    rating: str = Form(""),
    tags: str = Form(""),
    notes: str = Form(""),
    lesson: str = Form(""),
    screenshot: Optional[UploadFile] = File(default=None),
):
    check_key(key)

    existing = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"id": f"eq.{trade_id}", "select": "*", "limit": "1"},
    )
    rows = existing.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Trade not found")
    old = rows[0]
    screenshot_path = old.get("screenshot_path")

    if screenshot and screenshot.filename:
        new_path = await upload_screenshot(screenshot)
        if screenshot_path:
            try:
                await delete_screenshot(screenshot_path)
            except Exception:
                pass
        screenshot_path = new_path

    payload = {
        "instrument": instrument.strip().upper(),
        "side": side.strip().upper(),
        "trade_time": trade_time,
        "setup": setup.strip(),
        "entry": float(entry) if entry.strip() else None,
        "exit": float(exit) if exit.strip() else None,
        "qty": int(qty) if qty.strip() else None,
        "pnl": float(pnl or 0),
        "rating": int(rating) if rating.strip() else None,
        "tags": tags.strip(),
        "notes": notes.strip(),
        "lesson": lesson.strip(),
        "screenshot_path": screenshot_path,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    r = await sb_request(
        "PATCH",
        "/rest/v1/trades",
        params={"id": f"eq.{trade_id}"},
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    return {"ok": True, "trade": r.json()[0] if r.json() else payload}

@app.delete("/api/trades/{key:path}")
async def delete_trade(key: str, trade_id: str = Query(...)):
    check_key(key)

    existing = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"id": f"eq.{trade_id}", "select": "screenshot_path", "limit": "1"},
    )
    rows = existing.json()
    if rows and rows[0].get("screenshot_path"):
        try:
            await delete_screenshot(rows[0]["screenshot_path"])
        except Exception:
            pass

    await sb_request(
        "DELETE",
        "/rest/v1/trades",
        params={"id": f"eq.{trade_id}"},
        headers={"Prefer": "return=minimal"},
    )
    return {"ok": True}

JOURNAL_HTML = r"""
<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Journal</title>
<style>
:root{
  --bg:#090d12;--panel:#111820;--panel2:#161f29;--line:#27323e;
  --text:#edf3f8;--muted:#8492a0;--green:#48dc8a;--red:#ff7070;--blue:#6aa7ff;--yellow:#e5bd59;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,sans-serif}
button,input,select,textarea{font:inherit}
button{cursor:pointer}
.app{max-width:1180px;margin:auto;min-height:100vh}
.header{position:sticky;top:0;z-index:20;background:rgba(9,13,18,.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.header-row{padding:15px 18px;display:flex;gap:12px;align-items:center}
.brand{font-size:22px;font-weight:800}.sub{font-size:12px;color:var(--muted);margin-top:3px}.spacer{flex:1}
.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:9px 12px;border-radius:10px}.btn.primary{background:#1d6df2;border-color:#1d6df2}
.filters{padding:14px 18px;display:grid;grid-template-columns:1fr 160px 150px 180px;gap:9px;border-bottom:1px solid var(--line)}
.filters input,.filters select,.form-grid input,.form-grid select,.form-grid textarea{
 width:100%;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px
}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:16px 18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:13px}
.stat .k{font-size:10px;color:var(--muted);font-weight:700}.stat .v{font-size:21px;font-weight:800;margin-top:5px}
.feed{padding:0 18px 80px}.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;overflow:hidden;margin-bottom:15px}
.card-head{padding:13px 15px;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line)}
.symbol{font-size:18px;font-weight:800}.badge{font-size:11px;font-weight:750;padding:4px 8px;border-radius:999px;background:#202a34;color:#cad4dd}
.badge.long{background:rgba(72,220,138,.12);color:var(--green)}.badge.short{background:rgba(255,112,112,.12);color:var(--red)}
.pnl{margin-left:auto;font-weight:850}.pos{color:var(--green)}.neg{color:var(--red)}
.card-body{display:grid;grid-template-columns:minmax(280px,440px) 1fr;gap:16px;padding:15px}
.shot{width:100%;aspect-ratio:16/9;object-fit:cover;background:#0d1218;border:1px solid var(--line);border-radius:11px}
.meta{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.meta span{font-size:11px;color:#bec8d1;background:#19212a;border:1px solid var(--line);border-radius:999px;padding:4px 7px}
.note{white-space:pre-wrap;line-height:1.5}.lesson{margin-top:13px;padding-top:11px;border-top:1px solid var(--line)}
.empty{text-align:center;color:var(--muted);padding:100px 15px}
dialog{width:min(760px,95vw);padding:0;border:1px solid var(--line);border-radius:15px;background:#0f151c;color:var(--text)}
dialog::backdrop{background:rgba(0,0,0,.68)}
.modal-head,.modal-foot{padding:14px 16px;display:flex;align-items:center;border-bottom:1px solid var(--line)}.modal-foot{border-top:1px solid var(--line);border-bottom:0;justify-content:flex-end;gap:8px}
.form-grid{padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:11px}.full{grid-column:1/-1}label{display:block;font-size:10px;color:var(--muted);font-weight:750;margin-bottom:5px}
textarea{min-height:105px;resize:vertical}.preview{max-width:100%;max-height:250px;border-radius:10px;border:1px solid var(--line);display:none}
@media(max-width:800px){.header-row{flex-wrap:wrap}.filters{grid-template-columns:1fr 1fr}.filters input{grid-column:1/-1}.stats{grid-template-columns:1fr 1fr}.card-body{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}.full{grid-column:auto}}
</style>
</head>
<body>
<div class="app">
<header class="header">
 <div class="header-row">
  <div><div class="brand">Trading Journal</div><div class="sub">Feed trejdów · dostępny na każdym urządzeniu</div></div>
  <div class="spacer"></div>
  <button class="btn primary" onclick="openNew()">+ Dodaj trade</button>
 </div>
</header>

<section class="filters">
 <input id="search" placeholder="Szukaj po opisie, setupie, tagach...">
 <select id="instrumentFilter"><option value="">Wszystkie instrumenty</option></select>
 <select id="sideFilter"><option value="">LONG + SHORT</option><option>LONG</option><option>SHORT</option></select>
 <select id="setupFilter"><option value="">Wszystkie setupy</option></select>
</section>

<section class="stats">
 <div class="stat"><div class="k">TRADES</div><div class="v" id="sTrades">0</div></div>
 <div class="stat"><div class="k">TOTAL PNL</div><div class="v" id="sPnl">$0.00</div></div>
 <div class="stat"><div class="k">WIN RATE</div><div class="v" id="sWin">0%</div></div>
 <div class="stat"><div class="k">AVG TRADE</div><div class="v" id="sAvg">$0.00</div></div>
</section>

<main id="feed" class="feed"><div class="empty">Ładowanie...</div></main>
</div>

<dialog id="dlg">
 <div class="modal-head"><strong id="dlgTitle">Dodaj trade</strong><div class="spacer"></div><button class="btn" onclick="dlg.close()">Zamknij</button></div>
 <div class="form-grid">
  <div><label>Instrument</label><input id="instrument" list="instrumentList" placeholder="MNQ" required><datalist id="instrumentList"><option>MNQ</option><option>NQ</option><option>MES</option><option>ES</option><option>MCL</option><option>CL</option></datalist></div>
  <div><label>Kierunek</label><select id="side"><option>LONG</option><option>SHORT</option></select></div>
  <div><label>Data i czas</label><input id="trade_time" type="datetime-local"></div>
  <div><label>Setup</label><input id="setup" placeholder="ORB retest / VWAP MR"></div>
  <div><label>Entry</label><input id="entry" type="number" step="any"></div>
  <div><label>Exit</label><input id="exit" type="number" step="any"></div>
  <div><label>Qty</label><input id="qty" type="number" step="1"></div>
  <div><label>PnL</label><input id="pnl" type="number" step="any" placeholder="250 lub -120"></div>
  <div><label>Rating setupu</label><select id="rating"><option value="">—</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></div>
  <div><label>Tagi</label><input id="tags" placeholder="A+, trend, FOMO"></div>
  <div class="full"><label>Screenshot</label><input id="screenshot" type="file" accept="image/*"><img id="preview" class="preview"></div>
  <div class="full"><label>Opis</label><textarea id="notes" placeholder="Co widziałem, dlaczego wszedłem, jak zarządzałem..."></textarea></div>
  <div class="full"><label>Wniosek</label><textarea id="lesson" placeholder="Co powtórzyć / czego nie robić następnym razem?"></textarea></div>
 </div>
 <div class="modal-foot">
  <button class="btn" id="deleteBtn" style="display:none;background:#36171a;color:#ffb0b0" onclick="deleteCurrent()">Usuń</button>
  <button class="btn" onclick="dlg.close()">Anuluj</button>
  <button class="btn primary" onclick="saveTrade()">Zapisz trade</button>
 </div>
</dialog>

<script>
const key=__SAFE_KEY__;
const dlg=document.getElementById('dlg');
let editingId=null;
let currentTrade=null;

const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const money=v=>{const n=Number(v)||0;return (n>=0?'+':'-')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function nowLocal(){const d=new Date(),o=d.getTimezoneOffset();return new Date(d.getTime()-o*60000).toISOString().slice(0,16)}
function isoFromLocal(v){return v?new Date(v).toISOString():new Date().toISOString()}
function localInputFromIso(v){if(!v)return nowLocal();const d=new Date(v),o=d.getTimezoneOffset();return new Date(d.getTime()-o*60000).toISOString().slice(0,16)}

async function loadOptions(){
 const r=await fetch(`/api/options/${encodeURIComponent(key)}`,{cache:'no-store'}); const d=await r.json();
 const ifi=$('instrumentFilter'), sf=$('setupFilter'), ci=ifi.value, cs=sf.value;
 ifi.innerHTML='<option value="">Wszystkie instrumenty</option>'+d.instruments.map(x=>`<option>${esc(x)}</option>`).join('');
 sf.innerHTML='<option value="">Wszystkie setupy</option>'+d.setups.map(x=>`<option>${esc(x)}</option>`).join('');
 ifi.value=ci;sf.value=cs;
}
async function loadStats(){
 const r=await fetch(`/api/stats/${encodeURIComponent(key)}`,{cache:'no-store'}); const d=await r.json();
 $('sTrades').textContent=d.trades;
 $('sPnl').textContent=money(d.total_pnl).replace('+','');$('sPnl').className='v '+(d.total_pnl>0?'pos':d.total_pnl<0?'neg':'');
 $('sWin').textContent=Math.round(d.win_rate)+'%';
 $('sAvg').textContent=money(d.avg_trade).replace('+','');
}
async function loadFeed(){
 const p=new URLSearchParams();
 if($('search').value)p.set('q',$('search').value);
 if($('instrumentFilter').value)p.set('instrument',$('instrumentFilter').value);
 if($('sideFilter').value)p.set('side',$('sideFilter').value);
 if($('setupFilter').value)p.set('setup',$('setupFilter').value);
 const r=await fetch(`/api/trades/${encodeURIComponent(key)}?${p}`,{cache:'no-store'});
 const d=await r.json();
 if(!d.items.length){$('feed').innerHTML='<div class="empty">Brak trejdów. Kliknij <b>+ Dodaj trade</b>.</div>';return}
 $('feed').innerHTML=d.items.map(t=>{
   const pnl=Number(t.pnl)||0;
   const date=new Date(t.trade_time).toLocaleString('pl-PL',{dateStyle:'medium',timeStyle:'short'});
   return `<article class="card">
    <div class="card-head">
      <div class="symbol">${esc(t.instrument)}</div>
      <span class="badge ${t.side==='LONG'?'long':'short'}">${esc(t.side)}</span>
      ${t.setup?`<span class="badge">${esc(t.setup)}</span>`:''}
      <div class="pnl ${pnl>0?'pos':pnl<0?'neg':''}">${money(pnl)}</div>
    </div>
    <div class="card-body">
      <div>${t.screenshot_url?`<img class="shot" src="${t.screenshot_url}">`:`<div class="shot" style="display:grid;place-items:center;color:var(--muted)">Brak screenshotu</div>`}</div>
      <div>
        <div style="font-size:12px;color:var(--muted)">${esc(date)}</div>
        <div class="meta">
          ${t.entry!=null?`<span>Entry ${esc(t.entry)}</span>`:''}
          ${t.exit!=null?`<span>Exit ${esc(t.exit)}</span>`:''}
          ${t.qty!=null?`<span>Qty ${esc(t.qty)}</span>`:''}
          ${t.rating?`<span>${'★'.repeat(Number(t.rating))}</span>`:''}
          ${t.tags?`<span>${esc(t.tags)}</span>`:''}
        </div>
        <div class="note">${esc(t.notes||'')}</div>
        ${t.lesson?`<div class="lesson"><b>Wniosek:</b><div class="note">${esc(t.lesson)}</div></div>`:''}
        <div style="margin-top:13px"><button class="btn" onclick='editTrade(${JSON.stringify(t).replaceAll("'","&#39;")})'>Edytuj</button></div>
      </div>
    </div>
   </article>`
 }).join('');
}
function openNew(){
 editingId=null;currentTrade=null;
 ['instrument','setup','entry','exit','qty','pnl','rating','tags','notes','lesson'].forEach(id=>$(id).value='');
 $('side').value='LONG';$('trade_time').value=nowLocal();$('screenshot').value='';$('preview').style.display='none';$('preview').src='';
 $('deleteBtn').style.display='none';$('dlgTitle').textContent='Dodaj trade';dlg.showModal();
}
window.editTrade=t=>{
 editingId=t.id;currentTrade=t;
 $('instrument').value=t.instrument||'';$('side').value=t.side||'LONG';$('trade_time').value=localInputFromIso(t.trade_time);
 ['setup','entry','exit','qty','pnl','rating','tags','notes','lesson'].forEach(id=>$(id).value=t[id]??'');
 $('screenshot').value='';const p=$('preview');if(t.screenshot_url){p.src=t.screenshot_url;p.style.display='block'}else{p.src='';p.style.display='none'}
 $('deleteBtn').style.display='inline-block';$('dlgTitle').textContent='Edytuj trade';dlg.showModal();
}
$('screenshot').addEventListener('change',e=>{const f=e.target.files[0];if(!f)return;const p=$('preview');p.src=URL.createObjectURL(f);p.style.display='block'});
async function saveTrade(){
 if(!$('instrument').value.trim())return alert('Podaj instrument.');
 const fd=new FormData();
 fd.append('instrument',$('instrument').value.trim());fd.append('side',$('side').value);fd.append('trade_time',isoFromLocal($('trade_time').value));
 ['setup','entry','exit','qty','pnl','rating','tags','notes','lesson'].forEach(id=>fd.append(id,$(id).value));
 const file=$('screenshot').files[0];if(file)fd.append('screenshot',file);
 let method='POST';if(editingId){method='PUT';fd.append('trade_id',editingId)}
 const r=await fetch(`/api/trades/${encodeURIComponent(key)}`,{method,body:fd});
 if(!r.ok){alert(await r.text());return}
 dlg.close();await Promise.all([loadOptions(),loadStats(),loadFeed()]);
}
async function deleteCurrent(){
 if(!editingId||!confirm('Usunąć ten trade?'))return;
 const r=await fetch(`/api/trades/${encodeURIComponent(key)}?trade_id=${encodeURIComponent(editingId)}`,{method:'DELETE'});
 if(!r.ok){alert(await r.text());return}
 dlg.close();await Promise.all([loadOptions(),loadStats(),loadFeed()]);
}
['search','instrumentFilter','sideFilter','setupFilter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',loadFeed));
(async()=>{await Promise.all([loadOptions(),loadStats(),loadFeed()]);setInterval(loadFeed,15000)})();
</script>
</body>
</html>
"""
print("FINAL ROUTES:", [getattr(r, "path", "?") for r in app.routes], flush=True)
