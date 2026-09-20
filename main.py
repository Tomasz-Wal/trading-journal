
import os
import json
import secrets
import uuid
import csv
import io
import re
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Tomasz Trading Journal v2.9 Sync")

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
    return {"ok": True, "app": "Tomasz Trading Journal v2.9 Sync", "open": "/journal/YOUR_JOURNAL_KEY"}

@app.get("/health")
def health():
    return {"ok": True, "version": "2.9", "backup_reset": True, "orbit_transfer": 2, "media_transfer": True}

UK_TZ = ZoneInfo("Europe/London")

def range_start(period: str):
    # Zakresy journala liczymy według czasu UK, a do porównań zamieniamy granicę na UTC.
    now_uk = datetime.now(UK_TZ)
    if period == "today":
        start_uk = datetime(now_uk.year, now_uk.month, now_uk.day, tzinfo=UK_TZ)
    elif period == "week":
        monday = now_uk - timedelta(days=now_uk.weekday())
        start_uk = datetime(monday.year, monday.month, monday.day, tzinfo=UK_TZ)
    elif period == "month":
        start_uk = datetime(now_uk.year, now_uk.month, 1, tzinfo=UK_TZ)
    else:
        return None
    return start_uk.astimezone(timezone.utc)

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
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UK_TZ).astimezone(timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            if dt >= start:
                out.append(row)
        except ValueError:
            pass
    return out

# Rzeczywisty koszt round-trip z konta Tradify / Cash History.
# Importer zapisuje w journalu wyłącznie PnL po kosztach.
ROUND_TRIP_FEES = {"MNQ": 1.90}

# Backupy journala są przechowywane jako JSON w tym samym prywatnym bucketcie
# Supabase Storage co screenshoty. Reset nigdy nie usuwa screenshotów; backup
# zachowuje ich ścieżki, dzięki czemu po restore karty odzyskują obrazy.
BACKUP_PREFIX = "journal-backups"

def _backup_path_ok(path: str) -> bool:
    path = str(path or "").strip()
    return bool(path.startswith(BACKUP_PREFIX + "/") and path.endswith(".json") and ".." not in path)

async def _fetch_all_trades() -> list[dict]:
    r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"select": "*", "order": "trade_time.asc", "limit": "10000"},
    )
    return r.json()

async def _upload_backup(rows: list[dict], reason: str) -> dict:
    now_uk = datetime.now(UK_TZ)
    safe_reason = re.sub(r"[^a-z0-9_-]+", "-", str(reason or "backup").lower()).strip("-") or "backup"
    filename = f"{safe_reason}_{now_uk.strftime('%Y%m%d_%H%M%S')}_{len(rows)}entries_{uuid.uuid4().hex[:8]}.json"
    path = f"{BACKUP_PREFIX}/{filename}"
    backup = {
        "format": "tomasz-trading-journal-backup",
        "version": "2.9",
        "created_at_uk": now_uk.isoformat(),
        "reason": safe_reason,
        "entry_count": len(rows),
        "trades": rows,
    }
    data = json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")
    await sb_request(
        "POST",
        f"/storage/v1/object/{STORAGE_BUCKET}/{path}",
        content=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "x-upsert": "false",
        },
    )
    return {"path": path, "filename": filename, "entry_count": len(rows), "created_at_uk": now_uk.isoformat(), "reason": safe_reason}

async def _create_journal_backup(reason: str = "manual") -> dict:
    rows = await _fetch_all_trades()
    return await _upload_backup(rows, reason)

async def _delete_all_trades():
    await sb_request(
        "DELETE",
        "/rest/v1/trades",
        params={"id": "not.is.null"},
        headers={"Prefer": "return=minimal"},
    )

async def _insert_trade_rows(rows: list[dict]):
    if not rows:
        return
    # Mniejsze paczki są bezpieczniejsze dla PostgREST przy większych journalach.
    for start in range(0, len(rows), 250):
        batch = rows[start:start + 250]
        await sb_request(
            "POST",
            "/rest/v1/trades",
            json=batch,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )

async def _read_backup(path: str) -> dict:
    if not _backup_path_ok(path):
        raise HTTPException(status_code=400, detail="Nieprawidłowa ścieżka backupu.")
    r = await sb_request("GET", f"/storage/v1/object/{STORAGE_BUCKET}/{path}")
    try:
        data = json.loads(r.content.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Backup jest uszkodzony lub nie jest plikiem JSON.") from exc
    if data.get("format") != "tomasz-trading-journal-backup" or not isinstance(data.get("trades"), list):
        raise HTTPException(status_code=400, detail="Nieobsługiwany format backupu.")
    return data

def _csv_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}

def _optional_float(value):
    raw = str(value or "").strip().replace(",", ".")
    return float(raw) if raw else None

def calc_result_points(side, entry, exit_price, explicit=None):
    """Manual result_points wins; otherwise derive signed points from Entry/Exit."""
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    if entry is None or exit_price is None:
        return None
    try:
        e = float(entry)
        x = float(exit_price)
    except (TypeError, ValueError):
        return None
    return (x - e) if str(side or "").upper() == "LONG" else (e - x)

def calc_r(result_points, risk_points):
    try:
        pts = float(result_points)
        risk = float(risk_points)
    except (TypeError, ValueError):
        return None
    if risk <= 0:
        return None
    return pts / risk

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
                "result_points": round((exit_price - entry) if side == "LONG" else (entry - exit_price), 8),
                "risk_points": None,
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


TRANSFER_FORMAT = "orbit-journal-transfer"
TRANSFER_VERSION = 2
TRANSFER_MAX_BYTES = 80 * 1024 * 1024
TRANSFER_MAX_MEDIA_BYTES = 60 * 1024 * 1024
TRANSFER_MAX_MEDIA_ITEMS = 500


def _clean_text(value) -> str:
    return str(value or "").strip()


def _as_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _as_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _record_type_from_row(row: dict) -> tuple[str, str]:
    kind = _clean_text(row.get("entry_type") or "TRADE").upper()
    if kind == "TRADE":
        return ("taken" if row.get("taken") is not False else "missed"), ""
    if kind not in {"SESSION", "WEEK", "MONTH", "NOTE"}:
        kind = "SESSION"
    return "post", kind


def _normalize_transfer_type(raw: dict) -> tuple[str, str]:
    value = _clean_text(raw.get("record_type", raw.get("type", raw.get("entry_type", "taken")))).lower()
    if value in {"summary", "session", "week", "month", "note", "post"}:
        post_type = _clean_text(raw.get("post_type") or raw.get("entry_type") or ("SESSION" if value in {"summary", "post"} else value)).upper()
        if post_type not in {"SESSION", "WEEK", "MONTH", "NOTE"}:
            post_type = "SESSION"
        return "post", post_type
    return ("missed" if value == "missed" else "taken"), ""


def _canonical_transfer_time(value) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        pass
    return raw


def normalize_transfer_record(raw: dict) -> dict:
    raw = raw or {}
    record_type, post_type = _normalize_transfer_type(raw)
    side = _clean_text(raw.get("side", raw.get("direction"))).upper()
    entry = _as_float(raw.get("entry", raw.get("entry_price")))
    exit_price = _as_float(raw.get("exit", raw.get("exit_price")))
    risk_points = _as_float(raw.get("risk_points", raw.get("riskPoints")))
    if risk_points is not None and risk_points <= 0:
        risk_points = None
    result_points = _as_float(raw.get("result_points", raw.get("resultPoints", raw.get("points"))))
    result_r = _as_float(raw.get("result_r", raw.get("resultR", raw.get("r"))))
    if result_points is None and result_r is not None and risk_points is not None:
        result_points = result_r * risk_points
    if result_points is None and record_type == "taken":
        result_points = calc_result_points(side, entry, exit_price)
    if risk_points is None and result_points is not None and result_r not in (None, 0):
        risk_points = abs(result_points / result_r)
    if record_type == "post":
        result_points = risk_points = result_r = None
    refs = raw.get("media_refs") or raw.get("mediaRefs") or []
    if not isinstance(refs, list): refs = []
    return {
        "transfer_id": _clean_text(raw.get("transfer_id", raw.get("transferId"))),
        "record_type": record_type,
        "post_type": post_type,
        "trade_number": _as_int(raw.get("trade_number", raw.get("tradeNumber", raw.get("number")))) if record_type == "taken" else None,
        "instrument": _clean_text(raw.get("instrument", raw.get("symbol"))).upper(),
        "side": side,
        "trade_time": raw.get("trade_time", raw.get("tradeTime", raw.get("timestamp", raw.get("date")))),
        "exit_time": raw.get("exit_time", raw.get("exitTime")),
        "setup": _clean_text(raw.get("setup", raw.get("setupName", raw.get("title")))),
        "entry": entry,
        "exit": exit_price,
        "qty": _as_int(raw.get("qty", raw.get("quantity"))),
        "pnl": _as_float(raw.get("pnl", raw.get("pnl_usd", raw.get("netPnl"))), 0.0),
        "result_points": result_points,
        "risk_points": risk_points,
        "result_r": result_r,
        "rating": _as_int(raw.get("rating")),
        "tags": _clean_text(raw.get("tags")),
        "notes": _clean_text(raw.get("notes", raw.get("description", raw.get("reason", raw.get("body"))))),
        "lesson": _clean_text(raw.get("lesson", raw.get("conclusion"))),
        "media_refs": refs[:20],
        "source": raw.get("source") if isinstance(raw.get("source"), dict) else {},
    }


def transfer_fingerprint(record: dict) -> str:
    r = normalize_transfer_record(record)
    stable = {
        "record_type": r["record_type"], "post_type": r["post_type"], "trade_number": r["trade_number"],
        "instrument": r["instrument"], "side": r["side"], "trade_time": _canonical_transfer_time(r["trade_time"]),
        "exit_time": _canonical_transfer_time(r.get("exit_time")), "setup": r["setup"], "entry": r["entry"], "exit": r["exit"],
        "qty": r["qty"], "pnl": r["pnl"], "result_points": r["result_points"], "risk_points": r["risk_points"],
        "tags": r["tags"], "notes": r["notes"], "lesson": r["lesson"],
    }
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _row_media_descriptors(row: dict) -> list[dict]:
    out = []
    raw = row.get("media_paths")
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except Exception: raw = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip(): out.append({"path": item.strip(), "role": "primary"})
            elif isinstance(item, dict) and _clean_text(item.get("path")): out.append({"path": _clean_text(item.get("path")), "role": _clean_text(item.get("role") or "primary").lower()})
    primary = _clean_text(row.get("screenshot_path"))
    if primary and not any(x["path"] == primary for x in out): out.insert(0, {"path": primary, "role": "primary"})
    return out[:20]


def transfer_record_from_row(row: dict) -> dict:
    record_type, post_type = _record_type_from_row(row)
    result_points = calc_result_points(row.get("side"), row.get("entry"), row.get("exit"), row.get("result_points")) if record_type == "taken" else None
    result_r = calc_r(result_points, row.get("risk_points")) if record_type == "taken" else None
    record_id = _clean_text(row.get("id"))
    external_id = _clean_text(row.get("external_id"))
    return {
        "transfer_id": external_id or (f"trading-journal-v2:{record_id}" if record_id else ""),
        "record_type": record_type, "post_type": post_type,
        "trade_number": _as_int(row.get("trade_number")) if record_type == "taken" else None,
        "instrument": _clean_text(row.get("instrument")).upper(), "side": _clean_text(row.get("side")).upper() if record_type != "post" else "",
        "trade_time": row.get("trade_time"), "exit_time": row.get("exit_time"),
        "setup": (_clean_text(row.get("setup")) if record_type != "post" else (_clean_text(row.get("setup")) or {"SESSION":"Podsumowanie sesji","WEEK":"Podsumowanie tygodnia","MONTH":"Podsumowanie miesiąca","NOTE":"Własny wpis"}.get(post_type,"Podsumowanie"))),
        "entry": _as_float(row.get("entry")) if record_type != "post" else None,
        "exit": _as_float(row.get("exit")) if record_type != "post" else None,
        "qty": _as_int(row.get("qty")) if record_type != "post" else None,
        "pnl": _as_float(row.get("pnl"), 0.0) if record_type == "taken" else 0.0,
        "result_points": _as_float(result_points), "risk_points": _as_float(row.get("risk_points")) if record_type == "taken" else None,
        "result_r": _as_float(result_r), "rating": _as_int(row.get("rating")), "tags": _clean_text(row.get("tags")),
        "notes": _clean_text(row.get("notes")), "lesson": _clean_text(row.get("lesson")),
        "source": {"app": _clean_text(row.get("source_app")) or "trading-journal-v2", "record_id": record_id or None},
        "_storage_media": _row_media_descriptors(row),
    }


async def _build_transfer_package(rows: list[dict]) -> dict:
    records = [transfer_record_from_row(row) for row in rows]
    media_by_sha = {}
    total = 0
    for record in records:
        refs = []
        for item in record.pop("_storage_media", []):
            path = item["path"]
            try:
                r = await sb_request("GET", f"/storage/v1/object/{STORAGE_BUCKET}/{path}")
            except Exception:
                continue
            data = r.content
            if len(data) > 12 * 1024 * 1024: continue
            sha = hashlib.sha256(data).hexdigest()
            media_id = f"sha256:{sha}"
            if sha not in media_by_sha:
                total += len(data)
                if total > TRANSFER_MAX_MEDIA_BYTES: raise HTTPException(status_code=413, detail="Obrazy w transferze przekraczają 60 MB.")
                if len(media_by_sha) >= TRANSFER_MAX_MEDIA_ITEMS: raise HTTPException(status_code=413, detail="Za dużo obrazów w transferze.")
                ext = os.path.splitext(path)[1].lower() or ".jpg"
                mime = {".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".gif":"image/gif"}.get(ext, r.headers.get("content-type", "image/jpeg"))
                media_by_sha[sha] = {"id": media_id, "filename": os.path.basename(path), "mime_type": mime, "sha256": sha, "data_base64": base64.b64encode(data).decode("ascii")}
            refs.append({"id": media_id, "role": item.get("role") or "primary"})
        record["media_refs"] = refs
    return {"format": TRANSFER_FORMAT, "version": TRANSFER_VERSION, "source_app": "trading-journal-v2.9", "exported_at": datetime.now(timezone.utc).isoformat(), "record_count": len(records), "media_count": len(media_by_sha), "records": records, "media": list(media_by_sha.values())}


async def _restore_transfer_media(doc: dict) -> dict[str, dict]:
    media = doc.get("media") or []
    if not isinstance(media, list) or len(media) > TRANSFER_MAX_MEDIA_ITEMS: raise HTTPException(status_code=400, detail="Nieprawidłowa lista media.")
    total = 0
    restored = {}
    for item in media:
        if not isinstance(item, dict): raise HTTPException(status_code=400, detail="Nieprawidłowy wpis media.")
        media_id = _clean_text(item.get("id"))
        try: data = base64.b64decode(_clean_text(item.get("data_base64")), validate=True)
        except Exception: raise HTTPException(status_code=400, detail="Nieprawidłowy base64 obrazu.")
        if len(data) > 12 * 1024 * 1024: raise HTTPException(status_code=413, detail="Pojedynczy obraz jest większy niż 12 MB.")
        total += len(data)
        if total > TRANSFER_MAX_MEDIA_BYTES: raise HTTPException(status_code=413, detail="Obrazy w transferze przekraczają 60 MB.")
        sha = hashlib.sha256(data).hexdigest()
        claimed = _clean_text(item.get("sha256")).lower()
        if claimed and claimed != sha: raise HTTPException(status_code=400, detail="Błędna suma kontrolna obrazu.")
        mime = _clean_text(item.get("mime_type"))
        ext = {"image/png":".png","image/jpeg":".jpg","image/webp":".webp","image/gif":".gif"}.get(mime, os.path.splitext(_clean_text(item.get("filename")))[1].lower() or ".jpg")
        if ext not in {".png",".jpg",".jpeg",".webp",".gif"}: ext = ".jpg"
        path = f"transfer-media/{sha[:2]}/{sha}{ext}"
        await sb_request("POST", f"/storage/v1/object/{STORAGE_BUCKET}/{path}", content=data, headers={"Content-Type": mime or "application/octet-stream", "x-upsert": "true"})
        restored[media_id] = {"path": path, "sha256": sha}
    return restored


def _transfer_to_row(raw: dict, media_map: dict[str, dict]) -> dict:
    t = normalize_transfer_record(raw)
    refs = []
    for ref in t.get("media_refs") or []:
        if isinstance(ref, str): media_id, role = ref, "primary"
        elif isinstance(ref, dict): media_id, role = _clean_text(ref.get("id")), _clean_text(ref.get("role") or "primary").lower()
        else: continue
        found = media_map.get(media_id)
        if found: refs.append({"role": role, "path": found["path"], "sha256": found["sha256"]})
    primary = next((x["path"] for x in refs if x["role"] in {"primary","entry"}), refs[0]["path"] if refs else None)
    if t["record_type"] == "post":
        entry_type = t["post_type"] or "SESSION"
        instrument, side, taken = "SUMMARY", "LONG", True
        entry = exit_price = qty = result_points = risk_points = None
        pnl = 0.0
    else:
        entry_type = "TRADE"; instrument = t["instrument"] or "MNQ"; side = t["side"] or "LONG"; taken = t["record_type"] == "taken"
        entry, exit_price, qty, pnl = t["entry"], t["exit"], t["qty"], t["pnl"]
        result_points, risk_points = t["result_points"], t["risk_points"]
    return {
        "external_id": t["transfer_id"] or None,
        "source_app": _clean_text(t.get("source", {}).get("app")) or None,
        "instrument": instrument, "side": side, "trade_time": t["trade_time"], "exit_time": t.get("exit_time"),
        "setup": t["setup"], "entry": entry, "exit": exit_price, "qty": qty, "pnl": pnl,
        "result_points": result_points, "risk_points": risk_points, "rating": t["rating"], "tags": t["tags"], "notes": t["notes"], "lesson": t["lesson"],
        "screenshot_path": primary, "media_paths": refs, "entry_type": entry_type, "taken": taken,
    }

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
    entry_type: str = Query(default=""),
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
    if entry_type:
        params["entry_type"] = f"eq.{entry_type}"

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
                str(t.get("entry_type", "TRADE")),
            ]).lower()
            if text_q not in blob:
                return False
        return True

    items = [x for x in items if matches(x)][:limit]

    for item in items:
        media = _row_media_descriptors(item)
        item["media_urls"] = [f"/journal-image/{key}?path={quote(x['path'], safe='')}" for x in media]
        item["screenshot_url"] = item["media_urls"][0] if item["media_urls"] else None
    return {"items": items}

@app.get("/api/options/{key:path}")
async def options(key: str):
    check_key(key)
    r = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"select": "instrument,setup,tags,entry_type", "limit": "5000"},
    )
    rows = r.json()
    trade_rows = [x for x in rows if str(x.get("entry_type") or "TRADE").upper() == "TRADE"]
    instruments = sorted({str(x.get("instrument")).strip() for x in trade_rows if x.get("instrument")})
    setups = sorted({str(x.get("setup")).strip() for x in trade_rows if x.get("setup")})
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
        buy_fill_id = _clean_text(t.get("source_buy_fill_id"))
        sell_fill_id = _clean_text(t.get("source_sell_fill_id"))
        external_id = (f"performance:{buy_fill_id}:{sell_fill_id}" if (buy_fill_id or sell_fill_id) else f"performance:{hashlib.sha256(repr(fp).encode('utf-8')).hexdigest()}")
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
            "result_points": t.get("result_points"),
            "risk_points": None,
            "rating": None,
            "tags": "CSV import",
            "notes": "",
            "lesson": "",
            "screenshot_path": None,
            "media_paths": [],
            "external_id": external_id,
            "source_app": "performance-csv",
            "entry_type": "TRADE",
            "taken": True,
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

@app.get("/api/backups/{key:path}")
async def list_backups(key: str):
    check_key(key)
    r = await sb_request(
        "POST",
        f"/storage/v1/object/list/{STORAGE_BUCKET}",
        json={
            "prefix": BACKUP_PREFIX,
            "limit": 100,
            "offset": 0,
            "sortBy": {"column": "created_at", "order": "desc"},
        },
        headers={"Content-Type": "application/json"},
    )
    items = []
    for obj in r.json() if isinstance(r.json(), list) else []:
        name = str(obj.get("name") or "")
        if not name.endswith(".json"):
            continue
        path = name if name.startswith(BACKUP_PREFIX + "/") else f"{BACKUP_PREFIX}/{name}"
        match = re.search(r"_(\d+)entries_", name)
        metadata = obj.get("metadata") or {}
        items.append({
            "path": path,
            "name": name.split("/")[-1],
            "created_at": obj.get("created_at") or obj.get("updated_at"),
            "entry_count": int(match.group(1)) if match else None,
            "size": metadata.get("size"),
        })
    return {"items": items}

@app.post("/api/backups/{key:path}/create")
async def create_backup(key: str):
    check_key(key)
    info = await _create_journal_backup("manual")
    return {"ok": True, "backup": info}

@app.get("/api/backups/{key:path}/download")
async def download_backup(key: str, path: str = Query(...)):
    check_key(key)
    if not _backup_path_ok(path):
        raise HTTPException(status_code=400, detail="Nieprawidłowa ścieżka backupu.")
    r = await sb_request("GET", f"/storage/v1/object/{STORAGE_BUCKET}/{path}")
    filename = path.rsplit("/", 1)[-1]
    return Response(
        content=r.content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/api/journal-reset/{key:path}")
async def reset_journal(key: str, confirmation: str = Form(...)):
    check_key(key)
    if str(confirmation or "").strip().upper() != "RESET":
        raise HTTPException(status_code=400, detail="Reset wymaga potwierdzenia RESET.")

    # Najpierw pełny snapshot. Jeżeli jego zapis się nie powiedzie, funkcja rzuci
    # błąd i żaden rekord nie zostanie usunięty.
    rows = await _fetch_all_trades()
    backup = await _upload_backup(rows, "reset")
    await _delete_all_trades()
    return {"ok": True, "deleted": len(rows), "backup": backup}

@app.post("/api/backups/{key:path}/restore")
async def restore_backup(
    key: str,
    path: str = Form(...),
    confirmation: str = Form(...),
):
    check_key(key)
    if str(confirmation or "").strip().upper() != "RESTORE":
        raise HTTPException(status_code=400, detail="Przywrócenie wymaga potwierdzenia RESTORE.")

    target = await _read_backup(path)
    target_rows = target.get("trades") or []

    # Backup stanu bieżącego wykonywany automatycznie także przed restore.
    current_rows = await _fetch_all_trades()
    safety_backup = await _upload_backup(current_rows, "pre-restore")

    await _delete_all_trades()
    try:
        await _insert_trade_rows(target_rows)
    except Exception:
        # Best-effort rollback do stanu sprzed restore. Kopia JSON pozostaje
        # niezależnie od wyniku tej próby.
        try:
            await _delete_all_trades()
            await _insert_trade_rows(current_rows)
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "restored": len(target_rows),
        "source_backup": path,
        "safety_backup": safety_backup,
    }


@app.get("/api/transfer/export/{key:path}")
async def export_transfer(key: str):
    check_key(key)
    rows = await _fetch_all_trades()
    return await _build_transfer_package(rows)


@app.post("/api/transfer/import/{key:path}")
async def import_transfer(key: str, transfer_file: UploadFile = File(...)):
    check_key(key)
    raw_bytes = await transfer_file.read()
    if len(raw_bytes) > TRANSFER_MAX_BYTES: raise HTTPException(status_code=413, detail="Plik transferu jest większy niż 80 MB.")
    try: doc = json.loads(raw_bytes.decode("utf-8-sig"))
    except Exception: raise HTTPException(status_code=400, detail="Nieprawidłowy JSON transferu.")
    if isinstance(doc, list): doc = {"records": doc, "version": 1}
    if not isinstance(doc, dict): raise HTTPException(status_code=400, detail="Nieprawidłowy transfer.")
    if doc.get("format") and doc.get("format") != TRANSFER_FORMAT: raise HTTPException(status_code=400, detail="Nieobsługiwany format transferu.")
    if _as_int(doc.get("version"), 1) > TRANSFER_VERSION: raise HTTPException(status_code=400, detail="Transfer pochodzi z nowszej wersji.")
    records = doc.get("records") or doc.get("trades") or doc.get("items") or []
    if not isinstance(records, list) or len(records) > 10000: raise HTTPException(status_code=400, detail="Nieprawidłowa lista rekordów.")
    media_map = await _restore_transfer_media(doc)
    existing_rows = await _fetch_all_trades()
    fp_to_row = {transfer_fingerprint(transfer_record_from_row(row)): row for row in existing_rows}
    ext_to_row = {_clean_text(row.get("external_id")): row for row in existing_rows if _clean_text(row.get("external_id"))}
    imported = skipped = updated_media = 0
    errors = []
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, dict): errors.append({"index": index, "error": "Wpis nie jest obiektem."}); continue
        row = _transfer_to_row(raw, media_map)
        if not row.get("trade_time"): errors.append({"index": index, "error": "Brak trade_time."}); continue
        fp = transfer_fingerprint(raw); ext = _clean_text(row.get("external_id"))
        existing = ext_to_row.get(ext) if ext else fp_to_row.get(fp)
        if existing:
            incoming_media = _row_media_descriptors(row)
            existing_media = _row_media_descriptors(existing)
            if incoming_media:
                known_paths = {x["path"] for x in existing_media}
                merged_media = list(existing_media)
                for media in incoming_media:
                    if media["path"] not in known_paths:
                        merged_media.append(media)
                        known_paths.add(media["path"])
                if len(merged_media) != len(existing_media):
                    primary = next((x["path"] for x in merged_media if x.get("role") in {"primary", "entry"}), merged_media[0]["path"] if merged_media else None)
                    patch = {"screenshot_path": primary, "media_paths": merged_media[:20], "updated_at": datetime.now(timezone.utc).isoformat()}
                    if not _clean_text(existing.get("external_id")) and ext:
                        patch["external_id"] = ext
                    if not _clean_text(existing.get("source_app")) and row.get("source_app"):
                        patch["source_app"] = row.get("source_app")
                    await sb_request("PATCH", "/rest/v1/trades", params={"id": f"eq.{existing['id']}"}, json=patch, headers={"Content-Type":"application/json","Prefer":"return=minimal"})
                    updated_media += 1
            skipped += 1
            continue
        try:
            r = await sb_request("POST", "/rest/v1/trades", json=row, headers={"Content-Type":"application/json","Prefer":"return=representation"})
            saved = (r.json() or [row])[0]
            fp_to_row[fp] = saved
            if ext: ext_to_row[ext] = saved
            imported += 1
        except Exception as exc: errors.append({"index": index, "error": str(exc)[:300]})
    return {"ok": len(errors) == 0, "imported": imported, "skipped_duplicates": skipped, "updated_media": updated_media, "errors": errors[:50], "error_count": len(errors)}

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
            "select": "id,trade_time,instrument,side,setup,pnl,tags,rating,entry_type,taken,entry,exit,risk_points,result_points",
            "order": "trade_time.asc",
            "limit": "5000",
        },
    )
    rows = filter_period(r.json(), period)
    # Tylko wykonane TRADE wpływają na statystyki. Podsumowania i niewzięte setupy zostają w feedzie.
    rows = [
        x for x in rows
        if str(x.get("entry_type") or "TRADE").upper() == "TRADE"
        and x.get("taken") is not False
    ]

    def row_points(row):
        return calc_result_points(row.get("side"), row.get("entry"), row.get("exit"), row.get("result_points"))

    def row_r(row):
        return calc_r(row_points(row), row.get("risk_points"))

    def outcome_value(row):
        # Punktowy wynik transakcji najlepiej odzwierciedla win/loss; dla starych wpisów fallback do PnL.
        pts = row_points(row)
        return pts if pts is not None else float(row.get("pnl") or 0)

    pnls = [float(x.get("pnl") or 0) for x in rows]
    trades = len(rows)
    total = sum(pnls)
    wins = sum(1 for x in rows if outcome_value(x) > 0)
    losses = sum(1 for x in rows if outcome_value(x) < 0)

    point_values = [row_points(x) for x in rows]
    point_values = [x for x in point_values if x is not None]
    net_points = sum(point_values)
    won_points = sum(x for x in point_values if x > 0)
    lost_points = abs(sum(x for x in point_values if x < 0))

    r_values = [row_r(x) for x in rows]
    r_values = [x for x in r_values if x is not None]
    total_r = sum(r_values)
    avg_r = (total_r / len(r_values)) if r_values else None

    equity = []
    running = 0.0
    equity_r = []
    running_r = 0.0
    for row in rows:
        running += float(row.get("pnl") or 0)
        equity.append({"time": row.get("trade_time"), "value": running})
        rv = row_r(row)
        if rv is not None:
            running_r += rv
            equity_r.append({"time": row.get("trade_time"), "value": running_r})

    def group_by(field: str):
        groups = {}
        for row in rows:
            name = str(row.get(field) or "—").strip() or "—"
            g = groups.setdefault(name, {
                "name": name, "trades": 0, "pnl": 0.0, "wins": 0,
                "points": 0.0, "point_trades": 0, "total_r": 0.0, "r_trades": 0,
            })
            p = float(row.get("pnl") or 0)
            pts = row_points(row)
            rv = row_r(row)
            g["trades"] += 1
            g["pnl"] += p
            if outcome_value(row) > 0:
                g["wins"] += 1
            if pts is not None:
                g["points"] += pts
                g["point_trades"] += 1
            if rv is not None:
                g["total_r"] += rv
                g["r_trades"] += 1
        result = []
        for g in groups.values():
            g["win_rate"] = (g["wins"] / g["trades"] * 100) if g["trades"] else 0
            g["avg_r"] = (g["total_r"] / g["r_trades"]) if g["r_trades"] else None
            result.append(g)
        return sorted(
            result,
            key=lambda x: (x["total_r"] if x["r_trades"] else -10**9, x["points"] if x["point_trades"] else x["pnl"], x["trades"]),
            reverse=True,
        )

    daily = {}
    for row in rows:
        raw = row.get("trade_time")
        try:
            day = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
        except Exception:
            continue
        d = daily.setdefault(day, {
            "date": day, "pnl": 0.0, "points": 0.0, "point_trades": 0,
            "total_r": 0.0, "r_trades": 0, "trades": 0, "wins": 0,
        })
        p = float(row.get("pnl") or 0)
        pts = row_points(row)
        rv = row_r(row)
        d["pnl"] += p
        d["trades"] += 1
        if outcome_value(row) > 0:
            d["wins"] += 1
        if pts is not None:
            d["points"] += pts
            d["point_trades"] += 1
        if rv is not None:
            d["total_r"] += rv
            d["r_trades"] += 1

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
            "net_points": net_points,
            "won_points": won_points,
            "lost_points": lost_points,
            "point_trades": len(point_values),
            "total_r": total_r,
            "avg_r": avg_r,
            "r_trades": len(r_values),
        },
        "equity": equity,
        "equity_r": equity_r,
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
    instrument: str = Form(""),
    side: str = Form("LONG"),
    trade_time: str = Form(...),
    exit_time: str = Form(""),
    setup: str = Form(""),
    entry: str = Form(""),
    exit: str = Form(""),
    qty: str = Form(""),
    pnl: str = Form("0"),
    result_points: str = Form(""),
    risk_points: str = Form(""),
    rating: str = Form(""),
    tags: str = Form(""),
    notes: str = Form(""),
    lesson: str = Form(""),
    entry_type: str = Form("TRADE"),
    taken: str = Form("true"),
    screenshot: Optional[UploadFile] = File(default=None),
    screenshots: list[UploadFile] = File(default=[]),
):
    check_key(key)

    kind = str(entry_type or "TRADE").strip().upper()
    if kind not in {"TRADE", "SESSION", "WEEK", "MONTH", "NOTE"}:
        raise HTTPException(status_code=400, detail="Nieprawidłowy typ wpisu.")
    is_trade = kind == "TRADE"
    if is_trade and not instrument.strip():
        raise HTTPException(status_code=400, detail="Podaj instrument.")
    clean_side = side.strip().upper() if is_trade else "LONG"
    if is_trade and clean_side not in {"LONG", "SHORT"}:
        raise HTTPException(status_code=400, detail="Nieprawidłowy kierunek.")

    uploaded_paths = []
    if screenshot and screenshot.filename:
        uploaded_paths.append(await upload_screenshot(screenshot))
    for image in (screenshots or [])[:10]:
        if image and image.filename:
            uploaded_paths.append(await upload_screenshot(image))
    # Deduplikacja ścieżek (np. klient zgodności wysłał ten sam plik w obu polach).
    uploaded_paths = list(dict.fromkeys(uploaded_paths))
    screenshot_path = uploaded_paths[0] if uploaded_paths else None
    media_paths = [{"role": "primary" if i == 0 else "extra", "path": path} for i, path in enumerate(uploaded_paths)]

    parsed_entry = _optional_float(entry) if is_trade else None
    parsed_exit = _optional_float(exit) if is_trade else None
    parsed_result_points = _optional_float(result_points) if is_trade else None
    parsed_risk_points = _optional_float(risk_points) if is_trade else None
    if is_trade and parsed_result_points is None:
        parsed_result_points = calc_result_points(clean_side, parsed_entry, parsed_exit)
    if parsed_risk_points is not None and parsed_risk_points <= 0:
        parsed_risk_points = None

    payload = {
        "instrument": instrument.strip().upper() if is_trade else "SUMMARY",
        "side": clean_side,
        "trade_time": trade_time,
        "exit_time": exit_time if (is_trade and exit_time.strip()) else None,
        "setup": setup.strip(),
        "entry": parsed_entry,
        "exit": parsed_exit,
        "qty": int(qty) if (is_trade and qty.strip()) else None,
        "pnl": float(pnl or 0) if is_trade else 0,
        "result_points": parsed_result_points,
        "risk_points": parsed_risk_points,
        "rating": int(rating) if (is_trade and rating.strip()) else None,
        "tags": tags.strip(),
        "notes": notes.strip(),
        "lesson": lesson.strip(),
        "screenshot_path": screenshot_path,
        "media_paths": media_paths,
        "entry_type": kind,
        "taken": _csv_bool(taken) if is_trade else True,
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
    instrument: str = Form(""),
    side: str = Form("LONG"),
    trade_time: str = Form(...),
    exit_time: str = Form(""),
    setup: str = Form(""),
    entry: str = Form(""),
    exit: str = Form(""),
    qty: str = Form(""),
    pnl: str = Form("0"),
    result_points: str = Form(""),
    risk_points: str = Form(""),
    rating: str = Form(""),
    tags: str = Form(""),
    notes: str = Form(""),
    lesson: str = Form(""),
    entry_type: str = Form("TRADE"),
    taken: str = Form("true"),
    screenshot: Optional[UploadFile] = File(default=None),
    screenshots: list[UploadFile] = File(default=[]),
):
    check_key(key)

    existing = await sb_request(
        "GET",
        "/rest/v1/trades",
        params={"id": f"eq.{trade_id}", "select": "*", "limit": "1"},
    )
    rows = existing.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Wpis nie istnieje")
    old = rows[0]
    screenshot_path = old.get("screenshot_path")
    media_paths = old.get("media_paths") if isinstance(old.get("media_paths"), list) else ([{"role":"primary","path":screenshot_path}] if screenshot_path else [])

    kind = str(entry_type or "TRADE").strip().upper()
    if kind not in {"TRADE", "SESSION", "WEEK", "MONTH", "NOTE"}:
        raise HTTPException(status_code=400, detail="Nieprawidłowy typ wpisu.")
    is_trade = kind == "TRADE"
    if is_trade and not instrument.strip():
        raise HTTPException(status_code=400, detail="Podaj instrument.")
    clean_side = side.strip().upper() if is_trade else "LONG"
    if is_trade and clean_side not in {"LONG", "SHORT"}:
        raise HTTPException(status_code=400, detail="Nieprawidłowy kierunek.")

    new_paths = []
    if screenshot and screenshot.filename:
        new_paths.append(await upload_screenshot(screenshot))
    for image in (screenshots or [])[:10]:
        if image and image.filename:
            new_paths.append(await upload_screenshot(image))
    new_paths = list(dict.fromkeys(new_paths))
    if new_paths:
        existing_paths = [x for x in _row_media_descriptors(old) if x.get("path")]
        known = {x["path"] for x in existing_paths}
        for new_path in new_paths:
            if new_path not in known:
                existing_paths.append({"role": "extra" if existing_paths else "primary", "path": new_path})
                known.add(new_path)
        media_paths = existing_paths[:20]
        screenshot_path = next((x["path"] for x in media_paths if x.get("role") in {"primary", "entry"}), media_paths[0]["path"] if media_paths else None)

    parsed_entry = _optional_float(entry) if is_trade else None
    parsed_exit = _optional_float(exit) if is_trade else None
    parsed_result_points = _optional_float(result_points) if is_trade else None
    parsed_risk_points = _optional_float(risk_points) if is_trade else None
    if is_trade and parsed_result_points is None:
        parsed_result_points = calc_result_points(clean_side, parsed_entry, parsed_exit)
    if parsed_risk_points is not None and parsed_risk_points <= 0:
        parsed_risk_points = None

    payload = {
        "instrument": instrument.strip().upper() if is_trade else "SUMMARY",
        "side": clean_side,
        "trade_time": trade_time,
        "exit_time": exit_time if (is_trade and exit_time.strip()) else None,
        "setup": setup.strip(),
        "entry": parsed_entry,
        "exit": parsed_exit,
        "qty": int(qty) if (is_trade and qty.strip()) else None,
        "pnl": float(pnl or 0) if is_trade else 0,
        "result_points": parsed_result_points,
        "risk_points": parsed_risk_points,
        "rating": int(rating) if (is_trade and rating.strip()) else None,
        "tags": tags.strip(),
        "notes": notes.strip(),
        "lesson": lesson.strip(),
        "screenshot_path": screenshot_path,
        "media_paths": media_paths,
        "entry_type": kind,
        "taken": _csv_bool(taken) if is_trade else True,
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
        params={"id": f"eq.{trade_id}", "select": "screenshot_path,media_paths", "limit": "1"},
    )
    rows = existing.json()
    if rows:
        for media in _row_media_descriptors(rows[0]):
            try:
                await delete_screenshot(media["path"])
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
<title>Trading Journal v2.9 Sync</title>
<style>
:root{--bg:#080d12;--panel:#101820;--panel2:#151f29;--line:#27333f;--text:#edf3f8;--muted:#82909d;--green:#48dc8a;--red:#ff7070;--blue:#2377f4;--yellow:#e7bd58}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}
.app{max-width:1240px;margin:auto;min-height:100vh}.header{position:sticky;top:0;z-index:30;background:rgba(8,13,18,.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.header-row{padding:14px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.brand{font-weight:850;font-size:22px}.sub{font-size:11px;color:var(--muted);margin-top:2px}.spacer{flex:1}
.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:9px 12px;border-radius:10px}.btn.primary{background:var(--blue);border-color:var(--blue)}
.tabs{padding:0 18px 12px;display:flex;gap:7px;overflow:auto}.tab{white-space:nowrap;border:1px solid var(--line);background:transparent;color:#aeb9c4;padding:7px 12px;border-radius:999px}.tab.active{background:#1a2734;color:white;border-color:#3a4b5c}
.filters{padding:12px 18px;display:grid;grid-template-columns:1fr 145px 150px 145px 170px 150px;gap:8px;border-bottom:1px solid var(--line)}
.filters input,.filters select,.quick-grid input,.quick-grid select,.quick-grid textarea,.form-grid input,.form-grid select,.form-grid textarea{width:100%;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px}
.stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;padding:16px 18px 10px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px}.stat .k{font-size:10px;color:var(--muted);font-weight:750}.stat .v{font-size:22px;font-weight:850;margin-top:5px}
.analytics{padding:0 18px 14px;display:grid;grid-template-columns:1.5fr 1fr;gap:10px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px}.panel-title{font-weight:800;font-size:13px;margin-bottom:10px}
.chart-wrap{height:220px}.chart-wrap canvas{width:100%;height:100%}.mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.mini{background:#0d141b;border:1px solid #202c38;border-radius:10px;padding:10px}.mini .n{font-size:13px;font-weight:850}.mini .m{font-size:10px;color:var(--muted);margin-top:4px}
.breakdown{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:0 18px 14px}.table-panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px}.rows{display:flex;flex-direction:column;gap:7px}.row{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:center;font-size:12px}.row .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row .wr{color:var(--muted)}
.daily{padding:0 18px 14px}.day-list{display:grid;grid-template-columns:repeat(7,1fr);gap:7px}.day{background:#0d141b;border:1px solid #202c38;border-radius:9px;padding:8px;min-height:64px}.day .d{font-size:10px;color:var(--muted)}.day .p{font-weight:800;margin-top:5px}.day .t{font-size:10px;color:var(--muted);margin-top:4px}
.feed{padding:0 18px 80px}.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;overflow:hidden;margin-bottom:15px}.card-head{padding:13px 15px;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line)}
.symbol{font-size:18px;font-weight:850}.badge{font-size:11px;font-weight:800;padding:4px 8px;border-radius:999px;background:#202a34;color:#cad4dd}.badge.long{background:rgba(72,220,138,.12);color:var(--green)}.badge.short{background:rgba(255,112,112,.12);color:var(--red)}.badge.missed{background:rgba(231,189,88,.13);color:var(--yellow)}.badge.summary{background:rgba(106,167,255,.13);color:#8dbaff}.trade-hidden{display:none!important}.checkline{display:flex;align-items:center;gap:9px;padding:9px 0;color:#d5dee6;font-size:12px}.checkline input{width:auto!important}
.pnl{margin-left:auto;font-weight:850}.trade-result{margin-left:auto;text-align:right;min-width:110px}.trade-result .r-main{font-size:18px;font-weight:900}.trade-result .pts-sub{font-size:11px;font-weight:750;margin-top:2px}.trade-result .cash-sub{font-size:10px;color:var(--muted);margin-top:2px}.r-preview{grid-column:1/-1;background:#0d141b;border:1px solid #202c38;border-radius:10px;padding:10px 12px;font-size:12px;color:#c9d5df}.r-preview strong{font-size:16px}.pos{color:var(--green)}.neg{color:var(--red)}.card-body{display:grid;grid-template-columns:minmax(280px,440px) 1fr;gap:16px;padding:15px}.shot{width:100%;aspect-ratio:16/9;object-fit:cover;background:#0d1218;border:1px solid var(--line);border-radius:11px}.post-images{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:9px}.post-images:empty{display:none}.post-images .shot{aspect-ratio:16/9}.preview-shot{width:100%;max-height:240px;object-fit:cover;background:#0d1218;border:1px solid var(--line);border-radius:10px}
.meta{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.meta span{font-size:11px;color:#bec8d1;background:#19212a;border:1px solid var(--line);border-radius:999px;padding:4px 7px}.note{white-space:pre-wrap;line-height:1.5}.lesson{margin-top:13px;padding-top:11px;border-top:1px solid var(--line)}.empty{text-align:center;color:var(--muted);padding:90px 15px}
dialog{width:min(760px,95vw);padding:0;border:1px solid var(--line);border-radius:15px;background:#0f151c;color:var(--text)}dialog::backdrop{background:rgba(0,0,0,.68)}.modal-head,.modal-foot{padding:14px 16px;display:flex;align-items:center;border-bottom:1px solid var(--line)}.modal-foot{border-top:1px solid var(--line);border-bottom:0;justify-content:flex-end;gap:8px}
.quick-grid{padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:11px}.full{grid-column:1/-1}label{display:block;font-size:10px;color:var(--muted);font-weight:750;margin-bottom:5px}textarea{min-height:95px;resize:vertical}
.details{grid-column:1/-1;border:1px solid var(--line);border-radius:11px;background:#0c131a}.details summary{cursor:pointer;padding:11px 12px;font-size:12px;font-weight:800;color:#c8d3dd}.details .form-grid{padding:0 12px 12px;display:grid;grid-template-columns:1fr 1fr;gap:10px}.preview{max-width:100%;max-height:250px;border-radius:10px;border:1px solid var(--line);display:none}.import-wrap{padding:16px}.import-controls{display:grid;grid-template-columns:1fr 180px;gap:10px;align-items:end}.import-options{display:flex;flex-wrap:wrap;gap:14px;margin:12px 0;color:#c7d0d9;font-size:12px}.import-options label{display:flex;align-items:center;gap:7px;margin:0;font-size:12px}.import-options input{width:auto}.import-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}.import-kpi{background:#0c131a;border:1px solid var(--line);border-radius:10px;padding:10px}.import-kpi .k{font-size:10px;color:var(--muted)}.import-kpi .v{font-weight:850;margin-top:4px}.import-table-wrap{max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:10px}.import-table{width:100%;border-collapse:collapse;font-size:11px}.import-table th,.import-table td{padding:8px;border-bottom:1px solid #202c38;text-align:left;white-space:nowrap}.import-table th{position:sticky;top:0;background:#151f29;z-index:1}.dup{opacity:.48}.import-errors{color:#ffaaaa;font-size:12px;white-space:pre-wrap;margin-top:10px}.hint{font-size:11px;color:var(--muted);line-height:1.45}
.btn.danger{background:#3a171a;border-color:#6f2a31;color:#ffc0c5}.btn.danger:hover{background:#521e23}.backup-wrap{padding:16px}.backup-actions{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin-bottom:14px}.backup-warning{background:#28161a;border:1px solid #653039;color:#ffc5ca;padding:12px;border-radius:11px;line-height:1.45;font-size:12px;margin:12px 0}.backup-list{display:flex;flex-direction:column;gap:8px;max-height:390px;overflow:auto}.backup-row{display:grid;grid-template-columns:1fr auto auto auto;gap:8px;align-items:center;background:#0c131a;border:1px solid var(--line);border-radius:10px;padding:10px}.backup-name{font-size:12px;font-weight:800;overflow:hidden;text-overflow:ellipsis}.backup-meta{font-size:10px;color:var(--muted);margin-top:3px}.backup-empty{color:var(--muted);font-size:12px;padding:22px;text-align:center;border:1px dashed var(--line);border-radius:10px}
@media(max-width:900px){.stats{grid-template-columns:repeat(3,1fr)}.filters{grid-template-columns:1fr 1fr 1fr}.filters input{grid-column:1/-1}.analytics{grid-template-columns:1fr}.breakdown{grid-template-columns:1fr}.day-list{grid-template-columns:repeat(4,1fr)}}
@media(max-width:700px){.backup-row{grid-template-columns:1fr 1fr}.backup-row>div:first-child{grid-column:1/-1}.import-controls{grid-template-columns:1fr}.import-summary{grid-template-columns:1fr 1fr}.header-row{padding:12px}.brand{font-size:19px}.tabs{padding:0 12px 10px}.filters{padding:10px 12px;grid-template-columns:1fr 1fr}.stats{padding:12px;grid-template-columns:1fr 1fr}.analytics,.breakdown,.daily,.feed{padding-left:12px;padding-right:12px}.card-body{grid-template-columns:1fr}.quick-grid{grid-template-columns:1fr}.full{grid-column:auto}.details{grid-column:auto}.details .form-grid{grid-template-columns:1fr}.day-list{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<div class="app">
<header class="header">
 <div class="header-row">
  <div><div class="brand">Trading Journal v2.9 Sync</div><div class="sub">Feed · R-multiple · PnL · Tradovate · ORBIT Sync · obrazy · backup</div></div><div class="spacer"></div>
  <button class="btn" onclick="exportOrbitTransfer()">Eksport ORBIT</button>
  <button class="btn" onclick="chooseOrbitImport()">Import ORBIT</button>
  <input id="orbitTransferFile" type="file" accept=".json,application/json" style="display:none" onchange="importOrbitTransfer(this)">
  <button class="btn" onclick="openBackups()">Backupy / Reset</button>
  <button class="btn" onclick="openImport()">Import Tradovate CSV</button>
  <button class="btn primary" onclick="openNew()">+ Dodaj wpis</button>
 </div>
 <div class="tabs"><button class="tab active" data-period="today">Today</button><button class="tab" data-period="week">Week</button><button class="tab" data-period="month">Month</button><button class="tab" data-period="all">All</button></div>
</header>
<section class="filters">
 <input id="search" placeholder="Szukaj po opisie, setupie, tagach...">
 <select id="typeFilter"><option value="">Wszystkie wpisy</option><option value="TRADE">Trade</option><option value="SESSION">Podsumowanie sesji</option><option value="WEEK">Podsumowanie tygodnia</option><option value="MONTH">Podsumowanie miesiąca</option><option value="NOTE">Własny wpis</option></select>
 <select id="instrumentFilter"><option value="">Wszystkie instrumenty</option></select>
 <select id="sideFilter"><option value="">LONG + SHORT</option><option>LONG</option><option>SHORT</option></select>
 <select id="setupFilter"><option value="">Wszystkie setupy</option></select>
 <select id="tagFilter"><option value="">Wszystkie tagi</option></select>
</section>
<section class="stats">
 <div class="stat"><div class="k">TRADES</div><div class="v" id="sTrades">0</div></div>
 <div class="stat"><div class="k">TOTAL R</div><div class="v" id="sTotalR">—</div></div>
 <div class="stat"><div class="k">AVG R</div><div class="v" id="sAvgR">—</div></div>
 <div class="stat"><div class="k">NET POINTS</div><div class="v" id="sPoints">—</div></div>
 <div class="stat"><div class="k">WIN RATE</div><div class="v" id="sWin">0%</div></div>
 <div class="stat"><div class="k">TOTAL PNL</div><div class="v" id="sPnl">$0.00</div></div>
</section>
<section class="analytics">
 <div class="panel"><div class="panel-title">Cumulative R</div><div class="chart-wrap"><canvas id="equityCanvas"></canvas></div></div>
 <div class="panel"><div class="panel-title">Szybki obraz okresu</div><div class="mini-grid">
  <div class="mini"><div class="n" id="miniWins">0</div><div class="m">Wins</div></div>
  <div class="mini"><div class="n" id="miniLosses">0</div><div class="m">Losses</div></div>
  <div class="mini"><div class="n" id="miniWonPts">—</div><div class="m">Won points</div></div>
  <div class="mini"><div class="n" id="miniLostPts">—</div><div class="m">Lost points</div></div>
 </div></div>
</section>
<section class="breakdown">
 <div class="table-panel"><div class="panel-title">Setupy</div><div id="setupRows" class="rows"></div></div>
 <div class="table-panel"><div class="panel-title">Instrumenty</div><div id="instrumentRows" class="rows"></div></div>
 <div class="table-panel"><div class="panel-title">LONG vs SHORT</div><div id="sideRows" class="rows"></div></div>
</section>
<section class="daily"><div class="panel"><div class="panel-title">Daily R / Points / PnL</div><div id="dayList" class="day-list"></div></div></section>
<main id="feed" class="feed"><div class="empty">Ładowanie...</div></main>
</div>

<dialog id="dlg">
 <div class="modal-head"><strong id="dlgTitle">Dodaj wpis</strong><div class="spacer"></div><button class="btn" onclick="dlg.close()">Zamknij</button></div>
 <div class="quick-grid">
  <div><label>Typ wpisu</label><select id="entry_type" onchange="toggleEntryType()"><option value="TRADE">Trade</option><option value="SESSION">Podsumowanie sesji</option><option value="WEEK">Podsumowanie tygodnia</option><option value="MONTH">Podsumowanie miesiąca</option><option value="NOTE">Własny wpis</option></select></div>
  <div><label>Data i czas</label><input id="trade_time" type="datetime-local"></div>
  <div class="trade-only"><label>Instrument</label><input id="instrument" list="instrumentList" value="MNQ"><datalist id="instrumentList"><option>MNQ</option><option>NQ</option><option>MES</option><option>ES</option><option>MCL</option><option>CL</option></datalist></div>
  <div class="trade-only"><label>Kierunek</label><select id="side" onchange="updateRPreview()"><option>LONG</option><option>SHORT</option></select></div>
  <div class="trade-only"><label>PnL po kosztach</label><input id="pnl" type="number" step="any" placeholder="np. 250 albo -120"></div>
  <div class="trade-only"><label>Wynik w pkt (+ zysk / − strata)</label><input id="result_points" type="number" step="0.25" placeholder="wyliczy się z Entry/Exit" oninput="updateRPreview()"></div>
  <div class="trade-only"><label>Ryzyko 1R w pkt</label><input id="risk_points" type="number" step="0.25" min="0" placeholder="np. 20" oninput="updateRPreview()"></div>
  <div id="rPreview" class="trade-only r-preview"><strong>R: —</strong><br><span>Wpisz ryzyko 1R; wynik pkt może wyliczyć się z Entry/Exit.</span></div>
  <div><label id="setupLabel">Setup</label><input id="setup" list="setupList" placeholder="np. ORB retest"><datalist id="setupList"></datalist></div>
  <div class="trade-only full"><label class="checkline"><input id="taken" type="checkbox" checked> Trade wykonany — licz do statystyk i PnL</label></div>
  <div class="full"><label>Screenshoty</label><input id="screenshot" type="file" accept="image/*" multiple><div id="previews" class="post-images"></div></div>
  <div class="full"><label id="notesLabel">Opis</label><textarea id="notes" placeholder="Co widziałem i dlaczego wszedłem?"></textarea></div>
  <details class="details"><summary>Więcej szczegółów</summary><div class="form-grid">
   <div class="trade-only"><label>Wyjście — data i czas</label><input id="exit_time" type="datetime-local"></div>
   <div class="trade-only"><label>Qty</label><input id="qty" type="number" step="1"></div>
   <div class="trade-only"><label>Entry</label><input id="entry" type="number" step="any" oninput="updateRPreview()"></div>
   <div class="trade-only"><label>Exit</label><input id="exit" type="number" step="any" oninput="updateRPreview()"></div>
   <div class="trade-only"><label>Rating setupu</label><select id="rating"><option value="">—</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></div>
   <div><label>Tagi</label><input id="tags" placeholder="A+, trend, FOMO"></div>
   <div class="full"><label>Wniosek</label><textarea id="lesson" placeholder="Co powtórzyć / czego nie robić następnym razem?"></textarea></div>
  </div></details>
 </div>
 <div class="modal-foot"><button class="btn" id="deleteBtn" style="display:none;background:#36171a;color:#ffb0b0" onclick="deleteCurrent()">Usuń</button><button class="btn" onclick="dlg.close()">Anuluj</button><button class="btn primary" id="saveBtn" onclick="saveTrade()">Zapisz wpis</button></div>
</dialog>

<dialog id="importDlg" style="width:min(1050px,97vw)">
 <div class="modal-head"><strong>Import Tradovate / Performance CSV</strong><div class="spacer"></div><button class="btn" onclick="importDlg.close()">Zamknij</button></div>
 <div class="import-wrap">
  <div class="import-controls">
   <div><label>Plik Tradovate / Performance CSV</label><input id="csvFile" type="file" accept=".csv,text/csv"></div>
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

<dialog id="backupDlg" style="width:min(900px,96vw)">
 <div class="modal-head"><strong>Backupy i reset journala</strong><div class="spacer"></div><button class="btn" onclick="backupDlg.close()">Zamknij</button></div>
 <div class="backup-wrap">
  <div class="backup-actions">
   <button class="btn" onclick="createManualBackup()">Utwórz backup teraz</button>
   <button class="btn" onclick="loadBackups()">Odśwież listę</button>
   <div class="spacer"></div>
   <button class="btn danger" onclick="resetJournal()">Reset Journal</button>
  </div>
  <div class="hint">Backup zawiera wszystkie wpisy i ścieżki do screenshotów. Screenshoty pozostają w Supabase Storage. Restore zastępuje bieżący journal zawartością wybranego backupu, a przed przywróceniem automatycznie tworzy dodatkową kopię bezpieczeństwa.</div>
  <div class="backup-warning"><b>Reset Journal</b> najpierw automatycznie zapisuje pełny backup JSON. Dopiero po udanym zapisie usuwa wszystkie wpisy z tabeli trades. Jeśli backup się nie uda, reset nie nastąpi.</div>
  <div id="backupStatus" class="hint" style="margin:8px 0"></div>
  <div id="backupList" class="backup-list"><div class="backup-empty">Ładowanie backupów...</div></div>
 </div>
</dialog>

<script>
const key=__SAFE_KEY__,dlg=document.getElementById('dlg'),importDlg=document.getElementById('importDlg'),backupDlg=document.getElementById('backupDlg');let editingId=null,currentPeriod='today',lastImportPreview=null,lastEquityPoints=[];
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const money=v=>{const n=Number(v)||0;return(n>=0?'+':'-')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})};
function nowLocal(){const d=new Date(),o=d.getTimezoneOffset();return new Date(d.getTime()-o*60000).toISOString().slice(0,16)}function isoFromLocal(v){return v?new Date(v).toISOString():new Date().toISOString()}function localInputFromIso(v){if(!v)return nowLocal();const d=new Date(v),o=d.getTimezoneOffset();return new Date(d.getTime()-o*60000).toISOString().slice(0,16)}
function signed(v,d=2){if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v);return `${n>0?'+':''}${n.toFixed(d)}`}
function pointsOf(t){if(t?.result_points!=null&&Number.isFinite(Number(t.result_points)))return Number(t.result_points);const e=Number(t?.entry),x=Number(t?.exit);if(!Number.isFinite(e)||!Number.isFinite(x))return null;return String(t?.side).toUpperCase()==='LONG'?x-e:e-x}
function rOf(t){const p=pointsOf(t),risk=Number(t?.risk_points);return p!=null&&Number.isFinite(risk)&&risk>0?p/risk:null}
function updateRPreview(){
 const box=$('rPreview');if(!box)return;
 let pts=$('result_points').value.trim()===''?null:Number($('result_points').value);
 if(pts==null){const e=Number($('entry').value),x=Number($('exit').value);if(Number.isFinite(e)&&Number.isFinite(x)&&$('entry').value!==''&&$('exit').value!=='')pts=$('side').value==='LONG'?x-e:e-x}
 const risk=Number($('risk_points').value),rv=pts!=null&&Number.isFinite(risk)&&risk>0?pts/risk:null;
 box.innerHTML=`<strong class="${rv>0?'pos':rv<0?'neg':''}">R: ${rv==null?'—':signed(rv,2)+'R'}</strong><br><span>${pts==null?'Wynik pkt: —':'Wynik pkt: '+signed(pts,2)} · ${Number.isFinite(risk)&&risk>0?'1R = '+risk.toFixed(2)+' pkt':'Ryzyko 1R: —'}</span>`;
}
async function loadOptions(){const r=await fetch(`/api/options/${encodeURIComponent(key)}`,{cache:'no-store'}),d=await r.json();const set=(id,first,vals)=>{const s=$(id),cur=s.value;s.innerHTML=`<option value="">${first}</option>`+vals.map(x=>`<option>${esc(x)}</option>`).join('');s.value=cur};set('instrumentFilter','Wszystkie instrumenty',d.instruments);set('setupFilter','Wszystkie setupy',d.setups);set('tagFilter','Wszystkie tagi',d.tags);$('setupList').innerHTML=d.setups.map(x=>`<option>${esc(x)}</option>`).join('')}
function drawEquity(points){const c=$('equityCanvas'),box=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1;c.width=Math.max(300,box.width*dpr);c.height=Math.max(160,box.height*dpr);const x=c.getContext('2d');x.scale(dpr,dpr);const w=box.width,h=box.height;x.clearRect(0,0,w,h);x.strokeStyle='#26323d';for(let i=1;i<4;i++){const y=h*i/4;x.beginPath();x.moveTo(0,y);x.lineTo(w,y);x.stroke()}if(!points.length){x.fillStyle='#82909d';x.font='12px system-ui';x.fillText('Brak danych',12,24);return}const vals=points.map(p=>Number(p.value)||0),min=Math.min(0,...vals),max=Math.max(0,...vals),span=(max-min)||1,xy=(v,i)=>[points.length===1?w/2:i/(points.length-1)*w,h-((v-min)/span)*(h-20)-10];x.strokeStyle='#6aa7ff';x.lineWidth=2;x.beginPath();vals.forEach((v,i)=>{const[a,b]=xy(v,i);i?x.lineTo(a,b):x.moveTo(a,b)});x.stroke();const last=vals.at(-1),[lx,ly]=xy(last,vals.length-1);x.fillStyle=last>=0?'#48dc8a':'#ff7070';x.beginPath();x.arc(lx,ly,4,0,Math.PI*2);x.fill()}
function renderRows(id,rows){$(id).innerHTML=(rows||[]).slice(0,6).map(r=>{const hasR=Number(r.r_trades)>0,rv=hasR?Number(r.total_r):null,pts=Number(r.point_trades)>0?Number(r.points):null;return `<div class="row"><div class="name">${esc(r.name)}</div><div class="${rv>0?'pos':rv<0?'neg':''}" title="${pts==null?'':signed(pts,1)+' pkt'}">${rv==null?'— R':signed(rv,2)+'R'}</div><div class="wr">${Math.round(r.win_rate)}%</div></div>`}).join('')||'<div style="color:var(--muted);font-size:12px">Brak danych</div>'}
async function loadAnalytics(){const r=await fetch(`/api/analytics/${encodeURIComponent(key)}?period=${currentPeriod}`,{cache:'no-store'}),d=await r.json(),s=d.summary;
 $('sTrades').textContent=s.trades;
 $('sTotalR').textContent=s.r_trades?signed(s.total_r,2)+'R':'—';$('sTotalR').className='v '+(s.total_r>0?'pos':s.total_r<0?'neg':'');
 $('sAvgR').textContent=s.avg_r==null?'—':signed(s.avg_r,2)+'R';$('sAvgR').className='v '+(s.avg_r>0?'pos':s.avg_r<0?'neg':'');
 $('sPoints').textContent=s.point_trades?signed(s.net_points,1):'—';$('sPoints').className='v '+(s.net_points>0?'pos':s.net_points<0?'neg':'');
 $('sPnl').textContent=money(s.total_pnl).replace('+','');$('sPnl').className='v '+(s.total_pnl>0?'pos':s.total_pnl<0?'neg':'');
 $('sWin').textContent=(Math.round(s.win_rate*100)/100).toFixed(s.win_rate%1?2:0)+'%';
 $('miniWins').textContent=s.wins;$('miniLosses').textContent=s.losses;$('miniWonPts').textContent=s.point_trades?signed(s.won_points,1):'—';$('miniLostPts').textContent=s.point_trades?'-'+Number(s.lost_points||0).toFixed(1):'—';
 renderRows('setupRows',d.by_setup);renderRows('instrumentRows',d.by_instrument);renderRows('sideRows',d.by_side);
 $('dayList').innerHTML=(d.daily||[]).slice(0,21).map(v=>`<div class="day"><div class="d">${esc(v.date)}</div><div class="p ${v.total_r>0?'pos':v.total_r<0?'neg':''}">${v.r_trades?signed(v.total_r,2)+'R':'— R'}</div><div class="t">${v.point_trades?signed(v.points,1)+' pkt · ':''}${money(v.pnl)} · ${v.trades} trades</div></div>`).join('')||'<div style="color:var(--muted);font-size:12px">Brak danych</div>';
 lastEquityPoints=d.equity_r||[];drawEquity(lastEquityPoints)
}
const TYPE_LABELS={TRADE:'Trade',SESSION:'Podsumowanie sesji',WEEK:'Podsumowanie tygodnia',MONTH:'Podsumowanie miesiąca',NOTE:'Własny wpis'};
function entryTypeOf(t){return String(t.entry_type||'TRADE').toUpperCase()}
function renderFeedCard(t){
 const kind=entryTypeOf(t), date=new Date(t.trade_time).toLocaleString('pl-PL',{dateStyle:'medium',timeStyle:'short'});
 const urls=Array.isArray(t.media_urls)&&t.media_urls.length?t.media_urls:(t.screenshot_url?[t.screenshot_url]:[]); const shot=urls.length?`<div class="post-images">${urls.map(u=>`<img class="shot" src="${esc(u)}" loading="lazy">`).join('')}</div>`:`<div class="shot" style="display:grid;place-items:center;color:var(--muted)">Brak screenshotu</div>`;
 const commonMeta=`${t.tags?`<span>${esc(t.tags)}</span>`:''}`;
 if(kind!=='TRADE'){
  return `<article class="card"><div class="card-head"><div class="symbol">${esc(t.setup||TYPE_LABELS[kind]||'Podsumowanie')}</div><span class="badge summary">${esc(TYPE_LABELS[kind]||'POST')}</span></div><div class="card-body"><div>${shot}</div><div><div style="font-size:12px;color:var(--muted)">${esc(date)}</div><div class="meta">${commonMeta}</div><div class="note">${esc(t.notes||'')}</div>${t.lesson?`<div class="lesson"><b>Wniosek:</b><div class="note">${esc(t.lesson)}</div></div>`:''}<div style="margin-top:13px"><button class="btn" onclick='editTrade(${JSON.stringify(t).replaceAll("'","&#39;")})'>Edytuj</button></div></div></div></article>`;
 }
 const pnl=Number(t.pnl)||0,taken=t.taken!==false,pts=pointsOf(t),rv=rOf(t),risk=t.risk_points!=null?Number(t.risk_points):null;
 const resultHead=taken
  ?`<div class="trade-result"><div class="r-main ${rv>0?'pos':rv<0?'neg':''}">${rv==null?'— R':signed(rv,2)+'R'}</div><div class="pts-sub ${pts>0?'pos':pts<0?'neg':''}">${pts==null?'— pkt':signed(pts,1)+' pkt'}</div><div class="cash-sub">${money(pnl)}</div></div>`
  :`<div class="trade-result"><div class="r-main ${rv>0?'pos':rv<0?'neg':''}">Hip. ${rv==null?'— R':signed(rv,2)+'R'}</div><div class="pts-sub">${pts==null?'— pkt':signed(pts,1)+' pkt'}</div><div class="cash-sub">NIE LICZY SIĘ</div></div>`;
 return `<article class="card"><div class="card-head"><div class="symbol">${esc(t.instrument)}</div><span class="badge ${t.side==='LONG'?'long':'short'}">${esc(t.side)}</span>${!taken?'<span class="badge missed">NIEWZIĘTY</span>':''}${t.setup?`<span class="badge">${esc(t.setup)}</span>`:''}${resultHead}</div><div class="card-body"><div>${shot}</div><div><div style="font-size:12px;color:var(--muted)">${esc(date)}</div><div class="meta">${t.exit_time?`<span>Wyjście ${esc(new Date(t.exit_time).toLocaleTimeString('pl-PL',{hour:'2-digit',minute:'2-digit'}))}</span>`:''}${risk!=null&&Number.isFinite(risk)?`<span>1R = ${risk.toFixed(1)} pkt</span>`:''}${t.entry!=null?`<span>Entry ${esc(t.entry)}</span>`:''}${t.exit!=null?`<span>Exit ${esc(t.exit)}</span>`:''}${t.qty!=null?`<span>Qty ${esc(t.qty)}</span>`:''}${t.rating?`<span>${'★'.repeat(Number(t.rating))}</span>`:''}${commonMeta}</div><div class="note">${esc(t.notes||'')}</div>${t.lesson?`<div class="lesson"><b>Wniosek:</b><div class="note">${esc(t.lesson)}</div></div>`:''}<div style="margin-top:13px"><button class="btn" onclick='editTrade(${JSON.stringify(t).replaceAll("'","&#39;")})'>Edytuj</button></div></div></div></article>`;
}
async function loadFeed(){
 const p=new URLSearchParams({period:currentPeriod});
 if($('search').value)p.set('q',$('search').value);
 if($('typeFilter').value)p.set('entry_type',$('typeFilter').value);
 if($('instrumentFilter').value)p.set('instrument',$('instrumentFilter').value);
 if($('sideFilter').value)p.set('side',$('sideFilter').value);
 if($('setupFilter').value)p.set('setup',$('setupFilter').value);
 if($('tagFilter').value)p.set('tag',$('tagFilter').value);
 const r=await fetch(`/api/trades/${encodeURIComponent(key)}?${p}`,{cache:'no-store'}),d=await r.json();
 if(!d.items.length){$('feed').innerHTML='<div class="empty">Brak wpisów w tym widoku.</div>';return}
 $('feed').innerHTML=d.items.map(renderFeedCard).join('');
}
async function refresh(){await Promise.all([loadAnalytics(),loadFeed()])}
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',async()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');currentPeriod=b.dataset.period;await refresh()}))
function toggleEntryType(){
 const kind=$('entry_type').value, isTrade=kind==='TRADE';
 document.querySelectorAll('.trade-only').forEach(el=>el.classList.toggle('trade-hidden',!isTrade));
 $('dlgTitle').textContent=editingId?(isTrade?'Edytuj trade':'Edytuj '+TYPE_LABELS[kind].toLowerCase()):(isTrade?'Dodaj trade':'Dodaj '+TYPE_LABELS[kind].toLowerCase());
 $('saveBtn').textContent=isTrade?'Zapisz trade':'Zapisz wpis';
 $('setupLabel').textContent=isTrade?'Setup':'Tytuł';
 $('setup').placeholder=isTrade?'np. ORB retest':'np. Podsumowanie sesji — cierpliwość i egzekucja';
 $('notesLabel').textContent=isTrade?'Opis':'Podsumowanie';
 $('notes').placeholder=isTrade?'Co widziałem i dlaczego wszedłem?':'Najważniejsze obserwacje, przebieg i wnioski z okresu...';
 if(isTrade)updateRPreview();
}
function openNew(){
 editingId=null;$('entry_type').value='TRADE';$('taken').checked=true;$('instrument').value='MNQ';$('side').value='LONG';$('pnl').value='';$('result_points').value='';$('risk_points').value='';$('setup').value='';$('notes').value='';
 ['entry','exit','qty','rating','tags','lesson'].forEach(id=>$(id).value='');$('trade_time').value=nowLocal();$('exit_time').value='';$('screenshot').value='';$('previews').innerHTML='';$('deleteBtn').style.display='none';toggleEntryType();dlg.showModal();
}
window.editTrade=t=>{
 editingId=t.id;$('entry_type').value=entryTypeOf(t);$('taken').checked=t.taken!==false;$('instrument').value=(t.instrument==='SUMMARY'?'MNQ':t.instrument)||'MNQ';$('side').value=t.side||'LONG';$('pnl').value=t.pnl??'';$('result_points').value=t.result_points??'';$('risk_points').value=t.risk_points??'';$('setup').value=t.setup||'';$('notes').value=t.notes||'';$('trade_time').value=localInputFromIso(t.trade_time);$('exit_time').value=t.exit_time?localInputFromIso(t.exit_time):'';
 ['entry','exit','qty','rating','tags','lesson'].forEach(id=>$(id).value=t[id]??'');$('screenshot').value='';const urls=Array.isArray(t.media_urls)&&t.media_urls.length?t.media_urls:(t.screenshot_url?[t.screenshot_url]:[]);$('previews').innerHTML=urls.map(u=>`<img class="preview-shot" src="${esc(u)}">`).join('');$('deleteBtn').style.display='inline-block';toggleEntryType();dlg.showModal();
}
$('screenshot').addEventListener('change',e=>{const files=[...e.target.files];if(!files.length)return;$('previews').innerHTML=files.map(f=>`<img class="preview-shot" src="${URL.createObjectURL(f)}">`).join('')})
async function saveTrade(){
 const kind=$('entry_type').value;if(kind==='TRADE'&&!$('instrument').value.trim())return alert('Podaj instrument.');
 const fd=new FormData();fd.append('entry_type',kind);fd.append('taken',$('taken').checked?'true':'false');fd.append('instrument',$('instrument').value.trim());fd.append('side',$('side').value);fd.append('trade_time',isoFromLocal($('trade_time').value));fd.append('exit_time',$('exit_time').value?isoFromLocal($('exit_time').value):'');
 ['setup','entry','exit','qty','pnl','result_points','risk_points','rating','tags','notes','lesson'].forEach(id=>fd.append(id,$(id).value));[...$('screenshot').files].forEach(file=>fd.append('screenshots',file));let method='POST';if(editingId){method='PUT';fd.append('trade_id',editingId)}const r=await fetch(`/api/trades/${encodeURIComponent(key)}`,{method,body:fd});if(!r.ok){alert(await r.text());return}dlg.close();await loadOptions();await refresh();
}
function chooseOrbitImport(){$('orbitTransferFile').value='';$('orbitTransferFile').click()}
async function exportOrbitTransfer(){const r=await fetch(`/api/transfer/export/${encodeURIComponent(key)}`,{cache:'no-store'});if(!r.ok){alert(await r.text());return}const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`Trading_Journal_ORBIT_${new Date().toISOString().slice(0,10)}.json`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
async function importOrbitTransfer(input){const file=input.files?.[0];if(!file)return;const fd=new FormData();fd.append('transfer_file',file);const r=await fetch(`/api/transfer/import/${encodeURIComponent(key)}`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok){alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}await loadOptions();await refresh();alert(`Transfer ORBIT zakończony.\nZaimportowano: ${d.imported||0}\nPominięto duplikatów: ${d.skipped_duplicates||0}\nUzupełniono obrazy: ${d.updated_media||0}${d.error_count?`\nBłędy: ${d.error_count}`:''}`)}
function openImport(){lastImportPreview=null;$('csvFile').value='';$('csvNormalize').checked=true;$('csvSkipDup').checked=true;$('importSummary').style.display='none';$('importRows').innerHTML='';$('importErrors').textContent='';$('doImportBtn').disabled=true;importDlg.showModal()}
function importFd(){const f=$('csvFile').files[0];if(!f){alert('Wybierz plik CSV.');return null}const fd=new FormData();fd.append('file',f);fd.append('normalize_symbol',$('csvNormalize').checked?'true':'false');return fd}
function fmtDt(v){return new Date(v).toLocaleString('pl-PL',{timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'})}
async function previewCsv(){const fd=importFd();if(!fd)return;$('doImportBtn').disabled=true;const r=await fetch(`/api/import-csv-preview/${encodeURIComponent(key)}`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok){alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}lastImportPreview=d;$('importSummary').style.display='block';$('impCount').textContent=d.count;$('impPnl').textContent=money(d.pnl_total);$('impPnl').className='v '+(d.pnl_total>0?'pos':d.pnl_total<0?'neg':'');$('dupInfo').textContent=d.duplicates?`${d.duplicates} z ${d.count} wygląda na już istniejące. Przy włączonym „Pomijaj duplikaty” zostaną pominięte.`:`Nie wykryto duplikatów. Do importu: ${d.new_count}.`;$('importRows').innerHTML=d.items.map((t,i)=>`<tr class="${t.duplicate?'dup':''}"><td>${i+1}${t.duplicate?' · DUP':''}</td><td>${esc(fmtDt(t.trade_time))}</td><td>${esc(fmtDt(t.exit_time))}</td><td>${esc(t.instrument)}</td><td>${esc(t.side)}</td><td>${esc(t.qty)}</td><td>${esc(t.entry)}</td><td>${esc(t.exit)}</td><td class="${t.pnl>0?'pos':t.pnl<0?'neg':''}">${money(t.pnl)}</td></tr>`).join('');$('importErrors').textContent=(d.errors||[]).join('\n');$('doImportBtn').disabled=!!(d.errors||[]).length||!d.count}
async function doImportCsv(){if(!lastImportPreview){alert('Najpierw kliknij „Sprawdź plik”.');return}const fd=importFd();if(!fd)return;fd.append('skip_duplicates',$('csvSkipDup').checked?'true':'false');$('doImportBtn').disabled=true;const r=await fetch(`/api/import-csv/${encodeURIComponent(key)}`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok){$('doImportBtn').disabled=false;alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}importDlg.close();await loadOptions();await refresh();alert(`Zaimportowano: ${d.imported}\nPominięto duplikatów: ${d.skipped}\nPnL netto zaimportowanych: ${money(d.net_total_imported)}`)}
function openBackups(){backupDlg.showModal();loadBackups()}
function backupDate(v){if(!v)return '—';try{return new Date(v).toLocaleString('pl-PL',{timeZone:'Europe/London',dateStyle:'medium',timeStyle:'short'})}catch{return String(v)}}
function backupSize(v){const n=Number(v);if(!Number.isFinite(n))return '';if(n<1024)return n+' B';if(n<1024*1024)return (n/1024).toFixed(1)+' KB';return (n/1024/1024).toFixed(1)+' MB'}
async function loadBackups(){
 $('backupStatus').textContent='Ładowanie...';
 const r=await fetch(`/api/backups/${encodeURIComponent(key)}`,{cache:'no-store'});let d;try{d=await r.json()}catch{d={detail:await r.text()}}
 if(!r.ok){$('backupStatus').textContent='Błąd: '+(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}
 $('backupStatus').textContent=`Backupów: ${(d.items||[]).length}`;
 if(!(d.items||[]).length){$('backupList').innerHTML='<div class="backup-empty">Nie ma jeszcze żadnych backupów.</div>';return}
 $('backupList').innerHTML=d.items.map(b=>`<div class="backup-row"><div><div class="backup-name">${esc(b.name)}</div><div class="backup-meta">${esc(backupDate(b.created_at))}${b.entry_count!=null?' · '+esc(b.entry_count)+' wpisów':''}${b.size?' · '+esc(backupSize(b.size)):''}</div></div><button class="btn" onclick='downloadBackup(${JSON.stringify(b.path)})'>Pobierz</button><button class="btn" onclick='restoreBackup(${JSON.stringify(b.path)},${JSON.stringify(b.name)})'>Przywróć</button><span></span></div>`).join('');
}
async function createManualBackup(){
 $('backupStatus').textContent='Tworzę backup...';
 const r=await fetch(`/api/backups/${encodeURIComponent(key)}/create`,{method:'POST'});let d;try{d=await r.json()}catch{d={detail:await r.text()}}
 if(!r.ok){$('backupStatus').textContent='Błąd backupu.';alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}
 $('backupStatus').textContent=`Backup utworzony: ${d.backup.entry_count} wpisów.`;await loadBackups();
}
function downloadBackup(path){window.location.href=`/api/backups/${encodeURIComponent(key)}/download?path=${encodeURIComponent(path)}`}
async function resetJournal(){
 const typed=prompt('To usunie WSZYSTKIE wpisy z bieżącego journala po utworzeniu automatycznego backupu.\n\nAby kontynuować wpisz: RESET');
 if(typed!=='RESET')return;
 $('backupStatus').textContent='Tworzę backup i resetuję journal...';
 const fd=new FormData();fd.append('confirmation','RESET');
 const r=await fetch(`/api/journal-reset/${encodeURIComponent(key)}`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}
 if(!r.ok){$('backupStatus').textContent='Reset NIE został wykonany.';alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}
 $('backupStatus').textContent=`Reset gotowy. Usunięto ${d.deleted} wpisów. Backup zapisany.`;await loadOptions();await refresh();await loadBackups();alert(`Journal zresetowany.\nUsunięto wpisów: ${d.deleted}\nBackup: ${d.backup.filename}`);
}
async function restoreBackup(path,name){
 if(!confirm(`Przywrócić backup „${name}”?\n\nBieżący journal zostanie najpierw automatycznie zbackupowany, a następnie zastąpiony zawartością tej kopii.`))return;
 const typed=prompt('Aby potwierdzić przywrócenie wpisz: RESTORE');if(typed!=='RESTORE')return;
 $('backupStatus').textContent='Tworzę backup bezpieczeństwa i przywracam...';
 const fd=new FormData();fd.append('path',path);fd.append('confirmation','RESTORE');
 const r=await fetch(`/api/backups/${encodeURIComponent(key)}/restore`,{method:'POST',body:fd});let d;try{d=await r.json()}catch{d={detail:await r.text()}}
 if(!r.ok){$('backupStatus').textContent='Przywrócenie nie powiodło się.';alert(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return}
 $('backupStatus').textContent=`Przywrócono ${d.restored} wpisów.`;await loadOptions();await refresh();await loadBackups();alert(`Przywrócono ${d.restored} wpisów.\nPrzed restore utworzono dodatkowy backup bezpieczeństwa.`);
}

async function deleteCurrent(){if(!editingId||!confirm('Usunąć ten wpis?'))return;const r=await fetch(`/api/trades/${encodeURIComponent(key)}?trade_id=${encodeURIComponent(editingId)}`,{method:'DELETE'});if(!r.ok){alert(await r.text());return}dlg.close();await loadOptions();await refresh()}
['search','typeFilter','instrumentFilter','sideFilter','setupFilter','tagFilter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',loadFeed));window.addEventListener('resize',()=>drawEquity(lastEquityPoints));
(async()=>{await loadOptions();await refresh();setInterval(loadFeed,15000)})();
</script>
</body>
</html>
"""
print("FINAL ROUTES:", [getattr(r, "path", "?") for r in app.routes], flush=True)
