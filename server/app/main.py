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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .analytics import summarise_day
from .db import Database

BASE = Path(__file__).parent
DB_PATH = os.environ.get("WHOOP_DB", "/data/whoop.db")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
MAX_HR = float(os.environ.get("MAX_HR", "190"))
SLEEP_NEED_H = float(os.environ.get("SLEEP_NEED_HOURS", "8"))
BASELINE_DAYS = int(os.environ.get("BASELINE_DAYS", "30"))
TZ_OFFSET_H = float(os.environ.get("TZ_OFFSET_HOURS", "0"))

app = FastAPI(title="Whoop Server", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(BASE / "templates"))
db = Database(DB_PATH)


class Batch(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


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
    return {"ok": True, "received": received, "inserted": inserted,
            "duplicates": received - inserted}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, **db.stats()}


def _day_bounds(day: datetime) -> tuple[int, int]:
    """Local-day boundaries, expressed as unix seconds."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    start_unix = int(start.timestamp() - TZ_OFFSET_H * 3600)
    return start_unix, start_unix + 86400


def _baselines(before: datetime, days: int) -> tuple[float | None, float | None]:
    """Rolling personal baselines: median HRV and resting HR over `days`."""
    hrvs, rhrs = [], []
    for i in range(1, days + 1):
        lo, hi = _day_bounds(before - timedelta(days=i))
        s = summarise_day(db.range(lo, hi), max_hr=MAX_HR, sleep_need_h=SLEEP_NEED_H)
        if not s.get("has_data"):
            continue
        if s["hrv"]["rmssd_ms"]:
            hrvs.append(s["hrv"]["rmssd_ms"])
        if s["heart_rate"]["resting"]:
            rhrs.append(s["heart_rate"]["resting"])
    med = lambda xs: round(sorted(xs)[len(xs) // 2], 1) if xs else None
    return med(hrvs), med(rhrs)


def _summarise(day: datetime) -> dict[str, Any]:
    lo, hi = _day_bounds(day)
    hrv_base, rhr_base = _baselines(day, BASELINE_DAYS)
    s = summarise_day(db.range(lo, hi), max_hr=MAX_HR, sleep_need_h=SLEEP_NEED_H,
                      hrv_baseline=hrv_base, rhr_baseline=rhr_base)
    s["date"] = day.strftime("%Y-%m-%d")
    s["baselines"] = {"hrv_rmssd_ms": hrv_base, "resting_hr": rhr_base,
                      "window_days": BASELINE_DAYS}
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> Any:
    return templates.TemplateResponse(request, "dashboard.html")
