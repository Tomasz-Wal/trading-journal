
import os
import json
import secrets
import uuid
import csv
import io
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Tomasz Trading Journal v2.5")

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

@app.get("/")
def root():
    return {"ok": True, "app": "Tomasz Trading Journal v2.5", "open": "/journal/YOUR_JOURNAL_KEY"}

@app.get("/health")
def health():
    return {"ok": True, "version": "2.5"}

def range_start(period: str):
    now = datetime.now(timezone.utc)
    if period == "today":
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if period == "week":
        start = now - timedelta(days=now.weekday())
        return datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    if period == "month":
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return None

def filter_period(rows: list[dict], period: str) -> list[dict]:
    start = range_start(period)
    if start is None:
        return rows
    out = []
    for row in rows:
        raw = row.get("trade_time")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt >= start:
                out.append(row)
        except ValueError:
            pass
    return out

UK_TZ = ZoneInfo("Europe/London")

# Rzeczywisty koszt round-trip z konta Tradify / Cash History.
# Importer zapisuje w journalu wyłącznie PnL po kosztach.
ROUND_TRIP_FEES = {"MNQ": 1.90}

def _csv_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}

def _parse_money(value: str) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    negative = raw.startswith("$(") or (raw.startswith("(") and raw.endswith(")"))
    cleaned = raw.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Nieprawidłowy PnL: {value}") from exc
    return -abs(number) if negative else number

def _parse_performance_dt(value: str) -> datetime:
    raw = str(value or "").strip()
    formats = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
    for fmt in formats:
        try:
            local_dt = datetime.strptime(raw, fmt).replace(tzinfo=UK_TZ)
            return local_dt.astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Nieznany format daty/czasu: {value}")

def _normalize_futures_symbol(symbol: str) -> str:
    symbol = str(symbol or "").strip().upper()
    # Typowe symbole: MNQU6, NQZ26, ESH7 -> MNQ, NQ, ES.
    m = re.match(r"^([A-Z]{1,4}?)[FGHJKMNQUVXZ]\d{1,2}$", symbol)
    return m.group(1) if m else symbol

def _parse_performance_csv(data: bytes, normalize_symbol: bool) -> tuple[list[dict], list[str]]:
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="CSV jest za duży (max 5 MB).")
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(status_code=400, detail="Nie udało się odczytać kodowania CSV.")

    reader = csv.DictReader(io.StringIO(text))
    headers = {str(h or "").strip() for h in (reader.fieldnames or [])}
    required = {"symbol", "qty", "buyPrice", "sellPrice", "pnl", "boughtTimestamp", "soldTimestamp"}
    missing = sorted(required - headers)
    if missing:
        raise HTTPException(status_code=400, detail=f"Nieobsługiwany CSV. Brakuje kolumn: {', '.join(missing)}")

    trades = []
    errors = []
    for row_no, row in enumerate(reader, start=2):
        if not any(str(v or "").strip() for v in row.values()):
            continue
        try:
            symbol_raw = str(row.get("symbol") or "").strip().upper()
            instrument = _normalize_futures_symbol(symbol_raw) if normalize_symbol else symbol_raw
            qty = int(float(str(row.get("qty") or "0").strip()))
            if qty <= 0:
                raise ValueError("Qty musi być większe od 0")
            buy_price = float(str(row.get("buyPrice") or "").replace(",", "").strip())
            sell_price = float(str(row.get("sellPrice") or "").replace(",", "").strip())
            bought_at = _parse_performance_dt(row.get("boughtTimestamp"))
            sold_at = _parse_performance_dt(row.get("soldTimestamp"))
            gross_pnl = _parse_money(row.get("pnl"))

            if bought_at < sold_at:
                side = "LONG"
                entry_time, exit_time = bought_at, sold_at
                entry, exit_price = buy_price, sell_price
            elif sold_at < bought_at:
                side = "SHORT"
                entry_time, exit_time = sold_at, bought_at
                entry, exit_price = sell_price, buy_price
            else:
                raise ValueError("Czas kupna i sprzedaży jest identyczny — nie można ustalić kierunku")

            fee_symbol = _normalize_futures_symbol(symbol_raw)
            if fee_symbol not in ROUND_TRIP_FEES:
                raise ValueError(f"Brak ustawionego kosztu dla instrumentu {fee_symbol}")
            net_pnl = round(gross_pnl - (qty * ROUND_TRIP_FEES[fee_symbol]), 2)
            trades.append({
                "instrument": instrument,
                "source_symbol": symbol_raw,
                "side": side,
                "trade_time": entry_time.isoformat(),
                "exit_time": exit_time.isoformat(),
                "entry": entry,
                "exit": exit_price,
                "qty": qty,
                "pnl": net_pnl,
                "source_buy_fill_id": str(row.get("buyFillId") or "").strip(),
                "source_sell_fill_id": str(row.get("sellFillId") or "").strip(),
            })
        except Exception as exc:
            errors.append(f"Wiersz {row_no}: {exc}")
    return trades, errors

def _trade_fingerprint(t: dict) -> tuple:
    def n(v):
        return None if v is None else round(float(v), 8)
    raw_time = str(t.get("trade_time") or "")
    try:
        dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        time_key = dt.replace(microsecond=0).isoformat()
    except Exception:
        time_key = raw_time
    return (
        str(t.get("instrument") or "").upper(),
        str(t.get("side") or "").upper(),
        time_key,
        int(t.get("qty") or 0),
        n(t.get("entry")),
        n(t.get("exit")),
    )

@app.get("/journal/{key:path}", response_class=HTMLResponse)
def journal_page(key: str):
    check_key(key)
    safe_key = json.dumps(key)
    html = JOURNAL_HTML.replace("__SAFE_KEY__", safe_key)
    return HTMLResponse(html)

@app.get("/api/trades/{key:path}")
async def list_trades(
    key: str,
    period: str = Query(default="all", pattern="^(today|week|month|all)$"),
    instrument: str = Query(default=""),
    side: str = Query(default=""),
    setup: str = Query(default=""),
    tag: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=250, ge=1, le=500),
):
    check_key(key)
    params = {"select": "*", "order": "trade_time.desc", "limit": "1000"}
    if instrument:
        params["instrument"] = f"eq.{instrument}"
    if side:
        params["side"] = f"eq.{side}"
    if setup:
        params["setup"] = f"eq.{setup}"

    r = await sb_request("GET", "/rest/v1/trades", params=params)
    items = filter_period(r.json(), period)

    tag_q = tag.strip().lower()
    text_q = q.strip().lower()

    def matches(t):
        if tag_q and tag_q not in str(t.get("tags", "")).lower():
            return False
        if text_q:
            blob = " ".join([
                str(t.get("instrument", "")),
                str(t.get("setup", "")),
                str(t.get("notes", "")),
                str(t.get("lesson", "")),
                str(t.get("tags", "")),
            ]).lower()
            if text_q not in blob:
                return False
        return True

    items = [x for x in items if matches(x)][:limit]

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
        params={"select": "instrument,setup,tags", "limit": "5000"},
    )
    rows = r.json()
    instruments = sorted({str(x.get("instrument")).strip() for x in rows if x.get("instrument")})
    setups = sorted({str(x.get("setup")).strip() for x in rows if x.get("setup")})
    tags = set()
    for x in rows:
        raw = str(x.get("tags") or "")
        for tag in raw.replace("#", "").split(","):
            tag = tag.strip()
            if tag:
                tags.add(tag)
    return {"instruments": instruments, "setups": setups, "tags": sorted(tags)}

@app.post("/api/import-csv-preview/{key:path}")
async def import_csv_preview(
    key: str,
    file: UploadFile = File(...),
    normalize_symbol: str = Form("true"),
):
    check_key(key)
    data = await file.read()
    trades, errors = _parse_performance_csv(data, _csv_bool(normalize_symbol))

    existing_r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"select": "trade_time,instrument,side,entry,exit,qty", "limit": "5000"},
    )
    existing = {_trade_fingerprint(x) for x in existing_r.json()}
    preview = []
    duplicates = 0
    for t in trades:
        is_dup = _trade_fingerprint(t) in existing
        duplicates += int(is_dup)
        item = dict(t)
        item["duplicate"] = is_dup
        preview.append(item)

    return {
        "ok": True,
        "count": len(preview),
        "duplicates": duplicates,
        "new_count": len(preview) - duplicates,
        "pnl_total": round(sum(float(x["pnl"]) for x in preview), 2),
        "errors": errors,
        "items": preview,
    }

@app.post("/api/import-csv/{key:path}")
async def import_csv(
    key: str,
    file: UploadFile = File(...),
    normalize_symbol: str = Form("true"),
    skip_duplicates: str = Form("true"),
):
    check_key(key)
    data = await file.read()
    trades, errors = _parse_performance_csv(data, _csv_bool(normalize_symbol))
    if errors:
        raise HTTPException(status_code=400, detail={"message": "CSV zawiera błędne wiersze.", "errors": errors[:20]})
    if not trades:
        raise HTTPException(status_code=400, detail="CSV nie zawiera żadnych transakcji.")

    existing_r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"select": "trade_time,instrument,side,entry,exit,qty", "limit": "5000"},
    )
    existing = {_trade_fingerprint(x) for x in existing_r.json()}
    do_skip = _csv_bool(skip_duplicates)
    payloads = []
    skipped = 0
    for t in trades:
        fp = _trade_fingerprint(t)
        if do_skip and fp in existing:
            skipped += 1
            continue
        payloads.append({
            "instrument": t["instrument"],
            "side": t["side"],
            "trade_time": t["trade_time"],
            "exit_time": t["exit_time"],
            "setup": "",
            "entry": t["entry"],
            "exit": t["exit"],
            "qty": t["qty"],
            "pnl": t["pnl"],
            "rating": None,
            "tags": "CSV import",
            "notes": "",
            "lesson": "",
            "screenshot_path": None,
        })
        existing.add(fp)

    if payloads:
        await sb_request(
            "POST",
            "/rest/v1/trades",
            json=payloads,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )

    return {
        "ok": True,
        "imported": len(payloads),
        "skipped": skipped,
        "net_total_imported": round(sum(float(x["pnl"]) for x in payloads), 2),
    }

@app.get("/api/analytics/{key:path}")
async def analytics(
    key: str,
    period: str = Query(default="all", pattern="^(today|week|month|all)$"),
):
    check_key(key)
    r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={
            "select": "id,trade_time,instrument,side,setup,pnl,tags,rating",
            "order": "trade_time.asc",
            "limit": "5000",
        },
    )
    rows = filter_period(r.json(), period)

    pnls = [float(x.get("pnl") or 0) for x in rows]
    trades = len(pnls)
    total = sum(pnls)
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x < 0)

    equity = []
    running = 0.0
    for row in rows:
        running += float(row.get("pnl") or 0)
        equity.append({"time": row.get("trade_time"), "value": running})

    def group_by(field: str):
        groups = {}
        for row in rows:
            name = str(row.get(field) or "—").strip() or "—"
            g = groups.setdefault(name, {"name": name, "trades": 0, "pnl": 0.0, "wins": 0})
            p = float(row.get("pnl") or 0)
            g["trades"] += 1
            g["pnl"] += p
            if p > 0:
                g["wins"] += 1
        result = []
        for g in groups.values():
            g["win_rate"] = (g["wins"] / g["trades"] * 100) if g["trades"] else 0
            result.append(g)
        return sorted(result, key=lambda x: (x["pnl"], x["trades"]), reverse=True)

    daily = {}
    for row in rows:
        raw = row.get("trade_time")
        try:
            day = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
        except Exception:
            continue
        d = daily.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
        p = float(row.get("pnl") or 0)
        d["pnl"] += p
        d["trades"] += 1
        if p > 0:
            d["wins"] += 1

    ratings = [int(x["rating"]) for x in rows if x.get("rating") is not None]

    return {
        "summary": {
            "trades": trades,
            "total_pnl": total,
            "win_rate": (wins / trades * 100) if trades else 0,
            "avg_trade": (total / trades) if trades else 0,
            "wins": wins,
            "losses": losses,
            "avg_rating": (sum(ratings) / len(ratings)) if ratings else None,
        },
        "equity": equity,
        "by_setup": group_by("setup"),
        "by_instrument": group_by("instrument"),
        "by_side": group_by("side"),
        "daily": sorted(daily.values(), key=lambda x: x["date"], reverse=True),
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
    exit_time: str = Form(""),
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
    if exit_time.strip():
        payload["exit_time"] = exit_time

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
    exit_time: str = Form(""),
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
    if exit_time.strip():
        payload["exit_time"] = exit_time

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
<title>Trading Journal v2.5</title>
<style>
:root{--bg:#080d12;--panel:#101820;--panel2:#151f29;--line:#27333f;--text:#edf3f8;--muted:#82909d;--green:#48dc8a;--red:#ff7070;--blue:#2377f4;--yellow:#e7bd58}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}
.app{max-width:1240px;margin:auto;min-height:100vh}.header{position:sticky;top:0;z-index:30;background:rgba(8,13,18,.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.header-row{padding:14px 18px;display:flex;align-items:center;gap:12px}.brand{font-weight:850;font-size:22px}.sub{font-size:11px;color:var(--muted);margin-top:2px}.spacer{flex:1}
.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:9px 12px;border-radius:10px}.btn.primary{background:var(--blue);border-color:var(--blue)}
.tabs{padding:0 18px 12px;display:flex;gap:7px;overflow:auto}.tab{white-space:nowrap;border:1px solid var(--line);background:transparent;color:#aeb9c4;padding:7px 12px;border-radius:999px}.tab.active{background:#1a2734;color:white;border-color:#3a4b5c}
.filters{padding:12px 18px;display:grid;grid-template-columns:1fr 150px 145px 170px 150px;gap:8px;border-bottom:1px solid var(--line)}
.filters input,.filters select,.quick-grid input,.quick-grid select,.quick-grid textarea,.form-grid input,.form-grid select,.form-grid textarea{width:100%;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:16px 18px 10px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px}.stat .k{font-size:10px;color:var(--muted);font-weight:750}.stat .v{font-size:22px;font-weight:850;margin-top:5px}
.analytics{padding:0 18px 14px;display:grid;grid-template-columns:1.5fr 1fr;gap:10px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px}.panel-title{font-weight:800;font-size:13px;margin-bottom:10px}
.chart-wrap{height:220px}.chart-wrap canvas{width:100%;height:100%}.mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.mini{background:#0d141b;border:1px solid #202c38;border-radius:10px;padding:10px}.mini .n{font-size:13px;font-weight:850}.mini .m{font-size:10px;color:var(--muted);margin-top:4px}
.breakdown{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:0 18px 14px}.table-panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px}.rows{display:flex;flex-direction:column;gap:7px}.row{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:center;font-size:12px}.row .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row .wr{color:var(--muted)}
.daily{padding:0 18px 14px}.day-list{display:grid;grid-template-columns:repeat(7,1fr);gap:7px}.day{background:#0d141b;border:1px solid #202c38;border-radius:9px;padding:8px;min-height:64px}.day .d{font-size:10px;color:var(--muted)}.day .p{font-weight:800;margin-top:5px}.day .t{font-size:10px;color:var(--muted);margin-top:4px}
.feed{padding:0 18px 80px}.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;overflow:hidden;margin-bottom:15px}.card-head{padding:13px 15px;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line)}
.symbol{font-size:18px;font-weight:850}.badge{font-size:11px;font-weight:800;padding:4px 8px;border-radius:999px;background:#202a34;color:#cad4dd}.badge.long{background:rgba(72,220,138,.12);color:var(--green)}.badge.short{background:rgba(255,112,112,.12);color:var(--red)}
.pnl{margin-left:auto;font-weight:850}.pos{color:var(--green)}.neg{color:var(--red)}.card-body{display:grid;grid-template-columns:minmax(280px,440px) 1fr;gap:16px;padding:15px}.shot{width:100%;aspect-ratio:16/9;object-fit:cover;background:#0d1218;border:1px solid var(--line);border-radius:11px}
.meta{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.meta span{font-size:11px;color:#bec8d1;background:#19212a;border:1px solid var(--line);border-radius:999px;padding:4px 7px}.note{white-space:pre-wrap;line-height:1.5}.lesson{margin-top:13px;padding-top:11px;border-top:1px solid var(--line)}.empty{text-align:center;color:var(--muted);padding:90px 15px}
dialog{width:min(760px,95vw);padding:0;border:1px solid var(--line);border-radius:15px;background:#0f151c;color:var(--text)}dialog::backdrop{background:rgba(0,0,0,.68)}.modal-head,.modal-foot{padding:14px 16px;display:flex;align-items:center;border-bottom:1px solid var(--line)}.modal-foot{border-top:1px solid var(--line);border-bottom:0;justify-content:flex-end;gap:8px}
.quick-grid{padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:11px}.full{grid-column:1/-1}label{display:block;font-size:10px;color:var(--muted);font-weight:750;margin-bottom:5px}textarea{min-height:95px;resize:vertical}
.details{grid-column:1/-1;border:1px solid var(--line);border-radius:11px;background:#0c131a}.details summary{cursor:pointer;padding:11px 12px;font-size:12px;font-weight:800;color:#c8d3dd}.details .form-grid{padding:0 12px 12px;display:grid;grid-template-columns:1fr 1fr;gap:10px}.preview{max-width:100%;max-height:250px;border-radius:10px;border:1px solid var(--line);display:none}.import-wrap{padding:16px}.import-controls{display:grid;grid-template-columns:1fr 180px;gap:10px;align-items:end}.import-options{display:flex;flex-wrap:wrap;gap:14px;margin:12px 0;color:#c7d0d9;font-size:12px}.import-options label{display:flex;align-items:center;gap:7px;margin:0;font-size:12px}.import-options input{width:auto}.import-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}.import-kpi{background:#0c131a;border:1px solid var(--line);border-radius:10px;padding:10px}.import-kpi .k{font-size:10px;color:var(--muted)}.import-kpi .v{font-weight:850;margin-top:4px}.import-table-wrap{max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:10px}.import-table{width:100%;border-collapse:collapse;font-size:11px}.import-table th,.import-table td{padding:8px;border-bottom:1px solid #202c38;text-align:left;white-space:nowrap}.import-table th{position:sticky;top:0;background:#151f29;z-index:1}.dup{opacity:.48}.import-errors{color:#ffaaaa;font-size:12px;white-space:pre-wrap;margin-top:10px}.hint{font-size:11px;color:var(--muted);line-height:1.45}
@media(max-width:900px){.filters{grid-template-columns:1fr 1fr 1fr}.filters input{grid-column:1/-1}.analytics{grid-template-columns:1fr}.breakdown{grid-template-columns:1fr}.day-list{grid-template-columns:repeat(4,1fr)}}
@media(max-width:700px){.import-controls{grid-template-columns:1fr}.import-summary{grid-template-columns:1fr 1fr}.header-row{padding:12px}.brand{font-size:19px}.tabs{padding:0 12px 10px}.filters{padding:10px 12px;grid-template-columns:1fr 1fr}.stats{padding:12px;grid-template-columns:1fr 1fr}.analytics,.breakdown,.daily,.feed{padding-left:12px;padding-right:12px}.card-body{grid-template-columns:1fr}.quick-grid{grid-template-columns:1fr}.full{grid-column:auto}.details{grid-column:auto}.details .form-grid{grid-template-columns:1fr}.day-list{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<div class="app">
<header class="header">
 <div class="header-row">
  <div><div class="brand">Trading Journal v2.5</div><div class="sub">Feed · statystyki · equity · setupy · każde urządzenie</div></div><div class="spacer"></div>
  <button class="btn" onclick="openImport()">Import CSV</button>
  <button class="btn primary" onclick="openNew()">+ Dodaj trade</button>
 </div>
 <div class="tabs"><button class="tab active" data-period="today">Today</button><button class="tab" data-period="week">Week</button><button class="tab" data-period="month">Month</button><button class="tab" data-period="all">All</button></div>
</header>
<section class="filters">
 <input id="search" placeholder="Szukaj po opisie, setupie, tagach...">
 <select id="instrumentFilter"><option value="">Wszystkie instrumenty</option></select>
 <select id="sideFilter"><option value="">LONG + SHORT</option><option>LONG</option><option>SHORT</option></select>
 <select id="setupFilter"><option value="">Wszystkie setupy</option></select>
 <select id="tagFilter"><option value="">Wszystkie tagi</option></select>
</section>
<section class="stats">
 <div class="stat"><div class="k">TRADES</div><div class="v" id="sTrades">0</div></div>
 <div class="stat"><div class="k">TOTAL PNL</div><div class="v" id="sPnl">$0.00</div></div>
 <div class="stat"><div class="k">WIN RATE</div><div class="v" id="sWin">0%</div></div>
 <div class="stat"><div class="k">AVG TRADE</div><div class="v" id="sAvg">$0.00</div></div>
</section>
<section class="analytics">
 <div class="panel"><div class="panel-title">Equity curve</div><div class="chart-wrap"><canvas id="equityCanvas"></canvas></div></div>
 <div class="panel"><div class="panel-title">Szybki obraz okresu</div><div class="mini-grid">
  <div class="mini"><div class="n" id="miniWins">0</div><div class="m">Wins</div></div>
  <div class="mini"><div class="n" id="miniLosses">0</div><div class="m">Losses</div></div>
  <div class="mini"><div class="n" id="miniRating">—</div><div class="m">Avg setup rating</div></div>
  <div class="mini"><div class="n" id="miniBest">—</div><div class="m">Best setup</div></div>
 </div></div>
</section>
<section class="breakdown">
 <div class="table-panel"><div class="panel-title">Setupy</div><div id="setupRows" class="rows"></div></div>
 <div class="table-panel"><div class="panel-title">Instrumenty</div><div id="instrumentRows" class="rows"></div></div>
 <div class="table-panel"><div class="panel-title">LONG vs SHORT</div><div id="sideRows" class="rows"></div></div>
</section>
<section class="daily"><div class="panel"><div class="panel-title">Daily PnL</div><div id="dayList" class="day-list"></div></div></section>
<main id="feed" class="feed"><div class="empty">Ładowanie...</div></main>
</div>

<dialog id="dlg">
 <div class="modal-head"><strong id="dlgTitle">Dodaj trade</strong><div class="spacer"></div><button class="btn" onclick="dlg.close()">Zamknij</button></div>
 <div class="quick-grid">
  <div><label>Instrument</label><input id="instrument" list="instrumentList" value="MNQ"><datalist id="instrumentList"><option>MNQ</option><option>NQ</option><option>MES</option><option>ES</option><option>MCL</option><option>CL</option></datalist></div>
  <div><label>Kierunek</label><select id="side"><option>LONG</option><option>SHORT</option></select></div>
  <div><label>PnL</label><input id="pnl" type="number" step="any" placeholder="np. 250 albo -120"></div>
  <div><label>Setup</label><input id="setup" list="setupList" placeholder="np. ORB retest"><datalist id="setupList"></datalist></div>
  <div class="full"><label>Screenshot</label><input id="screenshot" type="file" accept="image/*"><img id="preview" class="preview"></div>
  <div class="full"><label>Opis</label><textarea id="notes" placeholder="Co widziałem i dlaczego wszedłem?"></textarea></div>
  <details class="details"><summary>Więcej szczegółów</summary><div class="form-grid">
   <div><label>Wejście — data i czas</label><input id="trade_time" type="datetime-local"></div><div><label>Wyjście — data i czas</label><input id="exit_time" type="datetime-local"></div>
   <div><label>Qty</label><input id="qty" type="number" step="1"></div><div><label>Entry</label><input id="entry" type="number" step="any"></div>
   <div><label>Exit</label><input id="exit" type="number" step="any"></div>
   <div><label>Rating setupu</label><select id="rating"><option value="">—</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></div>
   <div><label>Tagi</label><input id="tags" placeholder="A+, trend, FOMO"></div><div class="full"><label>Wniosek</label><textarea id="lesson" placeholder="Co powtórzyć / czego nie robić następnym razem?"></textarea></div>
  </div></details>
 </div>
 <div class="modal-foot"><button class="btn" id="deleteBtn" style="display:none;background:#36171a;color:#ffb0b0" onclick="deleteCurrent()">Usuń</button><button class="btn" onclick="dlg.close()">Anuluj</button><button class="btn primary" onclick="saveTrade()">Zapisz trade</button></div>
</dialog>

<dialog id="importDlg" style="width:min(1050px,97vw)">
 <div class="modal-head"><strong>Import transakcji z CSV</strong><div class="spacer"></div><button class="btn" onclick="importDlg.close()">Zamknij</button></div>
 <div class="import-wrap">
  <div class="import-controls">
   <div><label>Plik Performance CSV</label><input id="csvFile" type="file" accept=".csv,text/csv"></div>
  </div>
  <div class="import-options">
   <label><input id="csvNormalize" type="checkbox" checked> Zamień np. MNQU6 na MNQ</label>
   <label><input id="csvSkipDup" type="checkbox" checked> Pomijaj duplikaty</label>
  </div>
  <div class="hint">PnL jest automatycznie przeliczany i zapisywany po kosztach. Czasy z CSV są interpretowane jako czas UK (Europe/London).</div>
  <div style="margin-top:12px"><button class="btn" onclick="previewCsv()">Sprawdź plik</button></div>
  <div id="importSummary" style="display:none">
   <div class="import-summary">
    <div class="import-kpi"><div class="k">TRANSAKCJE</div><div class="v" id="impCount">0</div></div>
    <div class="import-kpi"><div class="k">PNL PO KOSZTACH</div><div class="v" id="impPnl">$0.00</div></div>
   </div>
   <div id="dupInfo" class="hint"></div>
   <div class="import-table-wrap"><table class="import-table"><thead><tr><th>#</th><th>Wejście UK</th><th>Wyjście UK</th><th>Instrument</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>PnL po kosztach</th></tr></thead><tbody id="importRows"></tbody></table></div>
   <div id="importErrors" class="import-errors"></div>
  </div>
 </div>
 <div class="modal-foot"><button class="btn" onclick="importDlg.close()">Anuluj</button><button class="btn primary" id="doImportBtn" onclick="doImportCsv()" disabled>Importuj transakcje</button></div>
</dialog>

<script>
const key=__SAFE_KEY__,dlg=document.getElementById('dlg'),importDlg=document.getElementById('importDlg');let editingId=null,currentPeriod='today',lastImportPreview=null;
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const money=v=>{const n=Number(v)||0;return(n>=0?'+':'-')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})};
function nowLocal(){const d=new Date(),o=d.getTimezoneOffset();return new Date(d.getTime()-o*60000).toISOString().slice(0,16)}function isoFromLocal(v){return v?new Date(v).toISOString():new Date().toISOString()}function localInputFromIso(v){if(!v)return nowLocal();const d=new Date(v),o=d.getTimezoneOffset();return new Date(d.getTime()-o*60000).toISOString().slice(0,16)}
async function loadOptions(){const r=await fetch(`/api/options/${encodeURIComponent(key)}`,{cache:'no-store'}),d=await r.json();const set=(id,first,vals)=>{const s=$(id),cur=s.value;s.innerHTML=`<option value="">${first}</option>`+vals.map(x=>`<option>${esc(x)}</option>`).join('');s.value=cur};set('instrumentFilter','Wszystkie instrumenty',d.instruments);set('setupFilter','Wszystkie setupy',d.setups);set('tagFilter','Wszystkie tagi',d.tags);$('setupList').innerHTML=d.setups.map(x=>`<option>${esc(x)}</option>`).join('')}
function drawEquity(points){const c=$('equityCanvas'),box=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1;c.width=Math.max(300,box.width*dpr);c.height=Math.max(160,box.height*dpr);const x=c.getContext('2d');x.scale(dpr,dpr);const w=box.width,h=box.height;x.clearRect(0,0,w,h);x.strokeStyle='#26323d';for(let i=1;i<4;i++){const y=h*i/4;x.beginPath();x.moveTo(0,y);x.lineTo(w,y);x.stroke()}if(!points.length){x.fillStyle='#82909d';x.font='12px system-ui';x.fillText('Brak danych',12,24);return}const vals=points.map(p=>Number(p.value)||0),min=Math.min(0,...vals),max=Math.max(0,...vals),span=(max-min)||1,xy=(v,i)=>[points.length===1?w/2:i/(points.length-1)*w,h-((v-min)/span)*(h-20)-10];x.strokeStyle='#6aa7ff';x.lineWidth=2;x.beginPath();vals.forEach((v,i)=>{const[a,b]=xy(v,i);i?x.lineTo(a,b):x.moveTo(a,b)});x.stroke();const last=vals.at(-1),[lx,ly]=xy(last,vals.length-1);x.fillStyle=last>=0?'#48dc8a':'#ff7070';x.beginPath();x.arc(lx,ly,4,0,Math.PI*2);x.fill()}
function renderRows(id,rows){$(id).innerHTML=(rows||[]).slice(0,6).map(r=>`<div class="row"><div class="name">${esc(r.name)}</div><div class="${r.pnl>0?'pos':r.pnl<0?'neg':''}">${money(r.pnl)}</div><div class="wr">${Math.round(r.win_rate)}%</div></div>`).join('')||'<div style="color:var(--muted);font-size:12px">Brak danych</div>'}
async function loadAnalytics(){const r=await fetch(`/api/analytics/${encodeURIComponent(key)}?period=${currentPeriod}`,{cache:'no-store'}),d=await r.json(),s=d.summary;$('sTrades').textContent=s.trades;$('sPnl').textContent=money(s.total_pnl).replace('+','');$('sPnl').className='v '+(s.total_pnl>0?'pos':s.total_pnl<0?'neg':'');$('sWin').textContent=Math.round(s.win_rate)+'%';$('sAvg').textContent=money(s.avg_trade).replace('+','');$('miniWins').textContent=s.wins;$('miniLosses').textContent=s.losses;$('miniRating').textContent=s.avg_rating==null?'—':s.avg_rating.toFixed(1)+'/5';$('miniBest').textContent=d.by_setup?.[0]?.name||'—';renderRows('setupRows',d.by_setup);renderRows('instrumentRows',d.by_instrument);renderRows('sideRows',d.by_side);$('dayList').innerHTML=(d.daily||[]).slice(0,21).map(v=>`<div class="day"><div class="d">${esc(v.date)}</div><div class="p ${v.pnl>0?'pos':v.pnl<0?'neg':''}">${money(v.pnl)}</div><div class="t">${v.trades} trades</div></div>`).join('')||'<div style="color:var(--muted);font-size:12px">Brak danych</div>';drawEquity(d.equity||[])}
async function loadFeed(){const p=new URLSearchParams({period:currentPeriod});if($('search').value)p.set('q',$('search').value);if($('instrumentFilter').value)p.set('instrument',$('instrumentFilter').value);if($('sideFilter').value)p.set('side',$('sideFilter').value);if($('setupFilter').value)p.set('setup',$('setupFilter').value);if($('tagFilter').value)p.set('tag',$('tagFilter').value);const r=await fetch(`/api/trades/${encodeURIComponent(key)}?${p}`,{cache:'no-store'}),d=await r.json();if(!d.items.length){$('feed').innerHTML='<div class="empty">Brak trejdów w tym widoku.</div>';return}$('feed').innerHTML=d.items.map(t=>{const pnl=Number(t.pnl)||0,date=new Date(t.trade_time).toLocaleString('pl-PL',{dateStyle:'medium',timeStyle:'short'});return`<article class="card"><div class="card-head"><div class="symbol">${esc(t.instrument)}</div><span class="badge ${t.side==='LONG'?'long':'short'}">${esc(t.side)}</span>${t.setup?`<span class="badge">${esc(t.setup)}</span>`:''}<div class="pnl ${pnl>0?'pos':pnl<0?'neg':''}">${money(pnl)}</div></div><div class="card-body"><div>${t.screenshot_url?`<img class="shot" src="${t.screenshot_url}" loading="lazy">`:`<div class="shot" style="display:grid;place-items:center;color:var(--muted)">Brak screenshotu</div>`}</div><div><div style="font-size:12px;color:var(--muted)">${esc(date)}</div><div class="meta">${t.exit_time?`<span>Wyjście ${esc(new Date(t.exit_time).toLocaleTimeString('pl-PL',{hour:'2-digit',minute:'2-digit'}))}</span>`:''}${t.entry!=null?`<span>Entry ${esc(t.entry)}</span>`:''}${t.exit!=null?`<span>Exit ${esc(t.exit)}</span>`:''}${t.qty!=null?`<span>Qty ${esc(t.qty)}</span>`:''}${t.rating?`<span>${'★'.repeat(Number(t.rating))}</span>`:''}${t.tags?`<span>${esc(t.tags)}</span>`:''}</div><div class="note">${esc(t.notes||'')}</div>${t.lesson?`<div class="lesson"><b>Wniosek:</b><div class="note">${esc(t.lesson)}</div></div>`:''}<div style="margin-top:13px"><button class="btn" onclick='editTrade(${JSON.stringify(t).replaceAll("'","&#39;")})'>Edytuj</button></div></div></div></article>`}).join('')}
async function refresh(){await Promise.all([loadAnalytics(),loadFeed()])}
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',async()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');currentPeriod=b.dataset.period;await refresh()}))
function openNew(){editingId=null;$('instrument').value='MNQ';$('side').value='LONG';$('pnl').value='';$('setup').value='';$('notes').value='';['entry','exit','qty','rating','tags','lesson'].forEach(id=>$(id).value='');$('trade_time').value=nowLocal();$('exit_time').value='';$('screenshot').value='';$('preview').style.display='none';$('preview').src='';$('deleteBtn').style.display='none';$('dlgTitle').textContent='Dodaj trade';dlg.showModal()}
window.editTrade=t=>{editingId=t.id;$('instrument').value=t.instrument||'';$('side').value=t.side||'LONG';$('pnl').value=t.pnl??'';$('setup').value=t.setup||'';$('notes').value=t.notes||'';$('trade_time').value=localInputFromIso(t.trade_time);$('exit_time').value=t.exit_time?localInputFromIso(t.exit_time):'';['entry','exit','qty','rating','tags','lesson'].forEach(id=>$(id).value=t[id]??'');$('screenshot').value='';const p=$('preview');if(t.screenshot_url){p.src=t.screenshot_url;p.style.display='block'}else{p.src='';p.style.display='none'}$('deleteBtn').style.display='inline-block';$('dlgTitle').textContent='Edytuj trade';dlg.showModal()}
$('screenshot').addEventListener('change',e=>{const f=e.target.files[0];if(!f)return;const p=$('preview');p.src=URL.createObjectURL(f);p.style.display='block'})
async function saveTrade(){if(!$('instrument').value.trim())return alert('Podaj instrument.');const fd=new FormData();fd.append('instrument',$('instrument').value.trim());fd.append('side',$('side').value);fd.append('trade_time',isoFromLocal($('trade_time').value));fd.append('exit_time',$('exit_time').value?isoFromLocal($('exit_time').value):'');['setup','entry','exit','qty','pnl','rating','tags','notes','lesson'].forEach(id=>fd.append(id,$(id).value));const file=$('screenshot').files[0];if(file)fd.append('screenshot',file);let method='POST';if(editingId){method='PUT';fd.append('trade_id',editingId)}const r=await fetch(`/api/trades/${encodeURIComponent(key)}`,{method,body:fd});if(!r.ok){alert(await r.text());return}dlg.close();await loadOptions();await refresh()}
function openImport(){lastImportPreview=null;$('csvFile').value='';$('csvNormalize').checked=true;$('csvSkipDup').checked=true;$('importSummary').style.display='none';$('importRows').innerHTML='';$('importErrors').textContent='';$('doImportBtn').disabled=true;importDlg.showModal()}
function importFd(){const f=$('csvFile').files[0];if(!f){alert('Wybierz plik CSV.');return null}const fd=new FormData();fd.append('file',f);fd.append('normalize_symbol',$('csvNormalize').checked?'true':'false');return fd}
function fmtDt(v){return new Date(v).toLocaleString('pl-PL',{timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'})}
async function previewCsv(){const fd=importFd();if(!fd)return;$('doImportBtn').disabled=true;const r=await fetch(`/api/import-csv-preview/${encodeURIComponent(key)}`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok){alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}lastImportPreview=d;$('importSummary').style.display='block';$('impCount').textContent=d.count;$('impPnl').textContent=money(d.pnl_total);$('impPnl').className='v '+(d.pnl_total>0?'pos':d.pnl_total<0?'neg':'');$('dupInfo').textContent=d.duplicates?`${d.duplicates} z ${d.count} wygląda na już istniejące. Przy włączonym „Pomijaj duplikaty” zostaną pominięte.`:`Nie wykryto duplikatów. Do importu: ${d.new_count}.`;$('importRows').innerHTML=d.items.map((t,i)=>`<tr class="${t.duplicate?'dup':''}"><td>${i+1}${t.duplicate?' · DUP':''}</td><td>${esc(fmtDt(t.trade_time))}</td><td>${esc(fmtDt(t.exit_time))}</td><td>${esc(t.instrument)}</td><td>${esc(t.side)}</td><td>${esc(t.qty)}</td><td>${esc(t.entry)}</td><td>${esc(t.exit)}</td><td class="${t.pnl>0?'pos':t.pnl<0?'neg':''}">${money(t.pnl)}</td></tr>`).join('');$('importErrors').textContent=(d.errors||[]).join('\n');$('doImportBtn').disabled=!!(d.errors||[]).length||!d.count}
async function doImportCsv(){if(!lastImportPreview){alert('Najpierw kliknij „Sprawdź plik”.');return}const fd=importFd();if(!fd)return;fd.append('skip_duplicates',$('csvSkipDup').checked?'true':'false');$('doImportBtn').disabled=true;const r=await fetch(`/api/import-csv/${encodeURIComponent(key)}`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok){$('doImportBtn').disabled=false;alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}importDlg.close();await loadOptions();await refresh();alert(`Zaimportowano: ${d.imported}\nPominięto duplikatów: ${d.skipped}\nPnL netto zaimportowanych: ${money(d.net_total_imported)}`)}
async function deleteCurrent(){if(!editingId||!confirm('Usunąć ten trade?'))return;const r=await fetch(`/api/trades/${encodeURIComponent(key)}?trade_id=${encodeURIComponent(editingId)}`,{method:'DELETE'});if(!r.ok){alert(await r.text());return}dlg.close();await loadOptions();await refresh()}
['search','instrumentFilter','sideFilter','setupFilter','tagFilter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',loadFeed));window.addEventListener('resize',()=>drawEquity([]));
(async()=>{await loadOptions();await refresh();setInterval(loadFeed,15000)})();
</script>
</body>
</html>
"""
print("FINAL ROUTES:", [getattr(r, "path", "?") for r in app.routes], flush=True)
