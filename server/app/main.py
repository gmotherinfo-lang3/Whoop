"""FastAPI server: ingest from the bridge, compute metrics, serve the dashboard.

Auth model:
  /ingest      Bearer token (INGEST_TOKEN). The bridge is not a browser, so it
               cannot pass a Cloudflare Access login -- see DEPLOY.md for the
               two supported ways to let it through (service token, or posting
               to the server directly on the LAN).
  everything   Cloudflare Access at the edge, before traffic reaches this app.
  else         There is deliberately no app-level login to get wrong.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .advice import suggest
from .analytics import summarise_day
from .bridge import cached_zip, pushed_config, release
from .bundle import build_zip, bundle_status, download_allowed, public_base_url
from .device import describe
from .db import Database
from .insights import analyse
from .ml import MODEL_NAME, ActivityClassifier, classify
from .readiness import activity_learning_status, insight_learning_status
from .segment import find_bouts

BASE = Path(__file__).parent
DB_PATH = os.environ.get("WHOOP_DB", "/data/whoop.db")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
MAX_HR = float(os.environ.get("MAX_HR", "190"))
SLEEP_NEED_H = float(os.environ.get("SLEEP_NEED_HOURS", "8"))
BASELINE_DAYS = int(os.environ.get("BASELINE_DAYS", "30"))
TZ_OFFSET_H = float(os.environ.get("TZ_OFFSET_HOURS", "0"))

from .segment import FEATURE_NAMES as _FEATURES

app = FastAPI(title="Whoop Server", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(BASE / "templates"))
db = Database(DB_PATH)


class Batch(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


class JournalEntry(BaseModel):
    tags: list[str] = Field(default_factory=list)
    amounts: dict[str, float] = Field(default_factory=dict)
    notes: str = ""


class ActivityEdit(BaseModel):
    confirmed_type: str | None = None
    start_unix: int | None = None
    end_unix: int | None = None
    note: str | None = None


class ManualActivity(BaseModel):
    start_unix: int
    end_unix: int
    activity_type: str
    note: str = ""


def _load_model() -> ActivityClassifier | None:
    stored = db.load_model(MODEL_NAME)
    return ActivityClassifier.from_payload(stored["payload"]) if stored else None


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


def require_token(authorization: str = Header(default="")) -> None:
    if not INGEST_TOKEN:
        raise HTTPException(500, "server has no INGEST_TOKEN configured")
    expected = f"Bearer {INGEST_TOKEN}"
    # Constant-time compare so the token can't be recovered by timing.
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "bad or missing token")


@app.post("/ingest")
def ingest(batch: Batch, _: None = Depends(require_token)) -> dict[str, Any]:
    received, inserted = db.insert_records(batch.records)
    if inserted:
        _invalidate_cache()
    return {"ok": True, "received": received, "inserted": inserted,
            "duplicates": received - inserted}


@app.post("/status")
def device_status(payload: dict[str, Any], _: None = Depends(require_token)) -> dict[str, Any]:
    """Heartbeat from the laptop bridge. Same token as /ingest."""
    db.put_device_status(payload)
    return {"ok": True}


@app.get("/api/device")
def api_device() -> dict[str, Any]:
    return describe(db.get_device_status())


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, **db.stats()}


def _day_bounds(day: datetime) -> tuple[int, int]:
    """Local-day boundaries, expressed as unix seconds."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    start_unix = int(start.timestamp() - TZ_OFFSET_H * 3600)
    return start_unix, start_unix + 86400


# Day summaries are pure functions of the stored records, so they are cached
# and the cache is dropped whenever new records land. Without this, computing a
# day meant recomputing its 30-day baseline from scratch, so a 90-day request
# performed ~2800 full day-summaries and timed out in the browser.
_CACHE_LIMIT = 2048
_raw_cache: dict[str, dict[str, Any]] = {}
_final_cache: dict[str, dict[str, Any]] = {}


def _invalidate_cache() -> None:
    _raw_cache.clear()
    _final_cache.clear()


def _trim_cache(cache: dict[str, Any]) -> None:
    while len(cache) > _CACHE_LIMIT:
        cache.pop(next(iter(cache)))


def _raw_summary(day: datetime) -> dict[str, Any]:
    """Day summary without baselines. Used to build the baseline series."""
    key = day.strftime("%Y-%m-%d")
    cached = _raw_cache.get(key)
    if cached is None:
        lo, hi = _day_bounds(day)
        cached = summarise_day(db.range(lo, hi), max_hr=MAX_HR, sleep_need_h=SLEEP_NEED_H)
        _raw_cache[key] = cached
        _trim_cache(_raw_cache)
    return cached


SENSOR_FIELDS = ("skin_temp_raw", "resp_rate_raw", "spo2_red", "spo2_ir")


def _median(values: list[float]) -> float | None:
    return round(sorted(values)[len(values) // 2], 1) if values else None


def _baselines(before: datetime, days: int) -> tuple[float | None, float | None]:
    """Rolling personal baselines: median HRV and resting HR over `days`."""
    hrvs, rhrs = [], []
    for i in range(1, days + 1):
        s = _raw_summary(before - timedelta(days=i))
        if not s.get("has_data"):
            continue
        if s["hrv"]["rmssd_ms"]:
            hrvs.append(s["hrv"]["rmssd_ms"])
        if s["heart_rate"]["resting"]:
            rhrs.append(s["heart_rate"]["resting"])
    return _median(hrvs), _median(rhrs)


def _sensor_baselines(before: datetime, days: int) -> dict[str, float | None]:
    """Median of each raw sensor channel. These have no real-world unit, so a
    comparison against your own history is the only reading that means anything."""
    collected: dict[str, list[float]] = {f: [] for f in SENSOR_FIELDS}
    for i in range(1, days + 1):
        s = _raw_summary(before - timedelta(days=i))
        if not s.get("has_data"):
            continue
        for field in SENSOR_FIELDS:
            value = (s.get("sensors") or {}).get(field)
            if isinstance(value, (int, float)):
                collected[field].append(float(value))
    return {f: _median(v) for f, v in collected.items()}


def _summarise(day: datetime) -> dict[str, Any]:
    key = day.strftime("%Y-%m-%d")
    cached = _final_cache.get(key)
    if cached is not None:
        return cached
    lo, hi = _day_bounds(day)
    hrv_base, rhr_base = _baselines(day, BASELINE_DAYS)
    s = summarise_day(db.range(lo, hi), max_hr=MAX_HR, sleep_need_h=SLEEP_NEED_H,
                      hrv_baseline=hrv_base, rhr_baseline=rhr_base)
    s["date"] = key
    s["baselines"] = {"hrv_rmssd_ms": hrv_base, "resting_hr": rhr_base,
                      "window_days": BASELINE_DAYS,
                      "sensors": _sensor_baselines(day, BASELINE_DAYS)}
    _final_cache[key] = s
    _trim_cache(_final_cache)
    return s


@app.get("/api/day/{date}")
def api_day(date: str) -> dict[str, Any]:
    try:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return _summarise(day)


@app.get("/api/summary")
def api_summary(days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    today = datetime.now(timezone.utc)
    out = [_summarise(today - timedelta(days=i)) for i in range(days)]
    return {"days": out, "stats": db.stats(),
            "disclaimer": "Locally computed approximations, not WHOOP's values. "
                          "Not medical measurements."}


@app.get("/api/series")
def api_series(date: str, field: str = "heart_rate") -> dict[str, Any]:
    allowed = {"heart_rate", "skin_temp_raw", "spo2_red", "spo2_ir",
               "resp_rate_raw", "ambient_light", "signal_quality"}
    if field not in allowed:
        raise HTTPException(400, f"field must be one of {sorted(allowed)}")
    try:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    lo, hi = _day_bounds(day)
    points = [{"t": r["device_unix"], "v": r[field]}
              for r in db.range(lo, hi) if r.get(field) is not None]
    return {"date": date, "field": field, "points": points,
            "raw_adc": field in {"skin_temp_raw", "spo2_red", "spo2_ir", "resp_rate_raw"}}


# --- journal ----------------------------------------------------------------
@app.get("/api/journal/{date}")
def api_get_journal(date: str) -> dict[str, Any]:
    _parse_date(date)
    return db.get_journal(date) or {"date": date, "tags": [], "amounts": {}, "notes": ""}


@app.put("/api/journal/{date}")
def api_put_journal(date: str, entry: JournalEntry) -> dict[str, Any]:
    _parse_date(date)
    db.put_journal(date, entry.tags, entry.amounts, entry.notes)
    return {"ok": True, **(db.get_journal(date) or {})}


@app.get("/api/journal")
def api_journal_range(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    return {"entries": db.journal_range(start, today.strftime("%Y-%m-%d")),
            "known_tags": db.all_tags()}


# --- activities -------------------------------------------------------------
def _detect_for_day(day: datetime) -> int:
    """Run detection for a day and store any new bouts. Idempotent."""
    lo, hi = _day_bounds(day)
    records = db.range(lo, hi)
    if not records:
        return 0
    summary = summarise_day(records, max_hr=MAX_HR, sleep_need_h=SLEEP_NEED_H)
    resting = summary.get("heart_rate", {}).get("resting") if summary.get("has_data") else None
    model = _load_model()
    found = 0
    for bout in find_bouts(records, resting, MAX_HR):
        label, confidence, _ = classify(bout["features"], bout.get("hint"), model)
        db.upsert_activity(bout["start_unix"], bout["end_unix"], label,
                           confidence, bout["features"])
        found += 1
    return found


@app.get("/api/activities")
def api_activities(date: str, detect: bool = True) -> dict[str, Any]:
    day = _parse_date(date)
    if detect:
        _detect_for_day(day)
    lo, hi = _day_bounds(day)
    model = _load_model()
    return {
        "date": date,
        "activities": db.activities_range(lo, hi),
        "source": "model" if (model and model.weights is not None
                              and (model.accuracy or 0) >= 0.60) else "rules",
    }


@app.post("/api/activities/detect")
def api_detect(date: str) -> dict[str, Any]:
    return {"ok": True, "date": date, "bouts": _detect_for_day(_parse_date(date))}


@app.post("/api/activities")
def api_add_activity(activity: ManualActivity) -> dict[str, Any]:
    if activity.end_unix <= activity.start_unix:
        raise HTTPException(400, "end_unix must be after start_unix")
    aid = db.add_manual_activity(activity.start_unix, activity.end_unix,
                                 activity.activity_type, activity.note)
    return {"ok": True, "id": aid}


@app.patch("/api/activities/{activity_id}")
def api_edit_activity(activity_id: int, edit: ActivityEdit) -> dict[str, Any]:
    if edit.start_unix is not None and edit.end_unix is not None \
            and edit.end_unix <= edit.start_unix:
        raise HTTPException(400, "end_unix must be after start_unix")
    if not db.update_activity(activity_id, confirmed_type=edit.confirmed_type,
                              start_unix=edit.start_unix, end_unix=edit.end_unix,
                              note=edit.note):
        raise HTTPException(404, "activity not found, deleted, or nothing to change")
    return {"ok": True, "id": activity_id}


@app.delete("/api/activities/{activity_id}")
def api_delete_activity(activity_id: int) -> dict[str, Any]:
    if not db.delete_activity(activity_id):
        raise HTTPException(404, "activity not found")
    return {"ok": True, "id": activity_id, "restorable": True}


@app.post("/api/activities/{activity_id}/restore")
def api_restore_activity(activity_id: int) -> dict[str, Any]:
    if not db.restore_activity(activity_id):
        raise HTTPException(404, "activity not found")
    return {"ok": True, "id": activity_id}


# --- learning ---------------------------------------------------------------
@app.post("/api/model/train")
def api_train() -> dict[str, Any]:
    labelled = db.labelled_activities()
    samples = [(list(a["features"].get(n, 0.0) for n in _FEATURES), a["confirmed_type"])
               for a in labelled if a["features"]]
    model = ActivityClassifier()
    report = model.train(samples)
    if report.get("trained"):
        db.save_model(MODEL_NAME, model.to_payload(), model.n_samples, model.accuracy)
    return report


@app.get("/api/learning")
def api_learning(days: int = Query(30, ge=7, le=365)) -> dict[str, Any]:
    today = datetime.now(timezone.utc)
    lo, _ = _day_bounds(today - timedelta(days=days))
    _, hi = _day_bounds(today)

    stored = db.load_model(MODEL_NAME)
    detected = len(db.activities_range(lo, hi))
    if detected == 0:
        # Nothing detected yet means no basis for a rate estimate. Run detection
        # over a bounded recent window so the first visit shows a real ETA.
        for i in range(1, 8):
            _detect_for_day(today - timedelta(days=i))
        detected = len(db.activities_range(lo, hi))
    activity = activity_learning_status(
        db.labelled_activities(), detected, days,
        stored["accuracy"] if stored else None)

    entries = db.journal_range((today - timedelta(days=days)).strftime("%Y-%m-%d"),
                               today.strftime("%Y-%m-%d"))
    tag_counts: dict[str, int] = {}
    for e in entries:
        for t in e["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    summaries = [_summarise(today - timedelta(days=i)) for i in range(days)]
    pairs = sum(1 for e in entries
                if any(s["date"] == e["date"] and s.get("has_data") for s in summaries))
    insights = insight_learning_status(pairs, tag_counts, len(entries), days)

    return {"activity_recognition": activity, "lifestyle_insights": insights}


@app.get("/api/insights")
def api_insights(days: int = Query(60, ge=14, le=365),
                 lag: int = Query(1, ge=0, le=3)) -> dict[str, Any]:
    today = datetime.now(timezone.utc)
    summaries = [_summarise(today - timedelta(days=i)) for i in range(days)]
    entries = db.journal_range((today - timedelta(days=days)).strftime("%Y-%m-%d"),
                               today.strftime("%Y-%m-%d"))
    return analyse(summaries, entries, lag_days=lag)


@app.get("/api/advice")
def api_advice(date: str | None = None,
               baseline_days: int = Query(30, ge=14, le=120)) -> dict[str, Any]:
    day = _parse_date(date) if date else datetime.now(timezone.utc)
    today = _summarise(day)
    history = [_summarise(day - timedelta(days=i)) for i in range(1, baseline_days + 1)]
    return {"date": day.strftime("%Y-%m-%d"), **suggest(today, history)}


# --- bridge update channel --------------------------------------------------
# These use the ingest token rather than the Cloudflare Access check that
# guards /setup: the bridge is not a browser and cannot complete an Access
# login, and the token is exactly the "this is my bridge" credential.
@app.get("/api/bridge/release")
def api_bridge_release(_: None = Depends(require_token)) -> dict[str, Any]:
    if not bundle_status()["ready"]:
        raise HTTPException(503, "this image has no bridge code to serve")
    return release()


@app.get("/api/bridge/bundle.zip")
def api_bridge_bundle(_: None = Depends(require_token)) -> Response:
    if not bundle_status()["ready"]:
        raise HTTPException(503, "this image has no bridge code to serve")
    info = release()
    return Response(
        content=cached_zip(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="whoop-bridge.zip"',
                 "X-Bridge-Version": info["version"],
                 "X-Bridge-Sha256": info["sha256"],
                 "Cache-Control": "no-store"})


@app.get("/api/bridge/config")
def api_bridge_config(_: None = Depends(require_token)) -> dict[str, Any]:
    """Settings the server dictates. Never credentials, never the strap address."""
    return {"config": pushed_config(), "release": release() if
            bundle_status()["ready"] else None}


# --- laptop setup bundle ----------------------------------------------------
def _fallback_base() -> str:
    return "http://127.0.0.1:8000"


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request) -> Any:
    allowed, reason = download_allowed(dict(request.headers),
                                       request.client.host if request.client else None)
    status = bundle_status()
    return templates.TemplateResponse(request, "setup.html", {
        "allowed": allowed,
        "reason": reason if not allowed else "",
        "ready": status["ready"],
        "base_url": public_base_url(dict(request.headers), _fallback_base()),
        "has_token": bool(INGEST_TOKEN),
    })


@app.api_route("/setup/bundle.zip", methods=["GET", "POST"])
def setup_bundle(request: Request,
                 cf_access_client_id: str = Form(default=""),
                 cf_access_client_secret: str = Form(default="")) -> Response:
    allowed, reason = download_allowed(dict(request.headers),
                                       request.client.host if request.client else None)
    if not allowed:
        raise HTTPException(403, reason)
    if not INGEST_TOKEN:
        raise HTTPException(500, "server has no INGEST_TOKEN configured")
    if not bundle_status()["ready"]:
        raise HTTPException(
            500, "laptop files are not in this image. Rebuild from the repository "
                 "root: docker compose up -d --build")

    data = build_zip(public_base_url(dict(request.headers), _fallback_base()),
                     INGEST_TOKEN, cf_access_client_id.strip(),
                     cf_access_client_secret.strip())
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="strap-laptop.zip"',
                 # It contains a credential; never let a proxy or the browser keep it.
                 "Cache-Control": "no-store, private"})


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> Any:
    return templates.TemplateResponse(request, "dashboard.html")
