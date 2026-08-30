"""FastAPI server: ingest from the bridge, compute metrics, serve the dashboard.

Auth model:
  browser      A signed-in session. The first visit creates the owner account;
               after that it is invite only. Cloudflare Access still sits in
               front at the edge, so this is the second of two layers.
  /ingest      A device token, issued when a laptop is paired. Each laptop has
               its own, so revoking one does not lock out the others, and the
               token identifies whose data the records are.
  /pair/claim  A short-lived pairing code, typed on the laptop. The only
               unauthenticated write, rate limited and single use.

Each account's records live in their own SQLite file (see store.py). There is
no query in this app that could return another person's data, because their
rows are not in the file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import (Cookie, Depends, FastAPI, Form, Header, HTTPException,
                     Query, Request)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .accounts import Accounts
from .advice import suggest
from .analytics import build_epochs, summarise_day
from .auth import (RateLimiter, email_problem, normalise_email,
                   password_problem)
from .bridge import cached_zip, pushed_config, release
from .bundle import build_zip, bundle_status, download_allowed, public_base_url
from .circadian import hr_trough, recovery_velocity
from .clock import Clock
from .device import describe
from .efficiency import efficiency_index
from .monitor import channel, summarise as summarise_channels
from .norms import (estimate_vo2max, fitness_age, trend as fitness_trend,
                    vo2max_trend)
from .stress import raw_series, stress_day
from .hrv_advanced import dfa_alpha1, sample_entropy
from .store import STRESS_BASELINE_DAYS, StoreRegistry, UserStore
from .substances import CAFFEINE_MG, ALCOHOL_UNITS, curve, overlay
from .workload import acwr, payback_plan, sleep_debt, strain_target
from .insights import analyse
from .ml import MODEL_NAME, ActivityClassifier, classify
from .readiness import activity_learning_status, insight_learning_status
from .segment import find_bouts

log = logging.getLogger("whoop.server")

BASE = Path(__file__).parent

# Where the databases live. accounts.db holds identity; data-<id>.db holds one
# person's records. WHOOP_DB is the pre-accounts single-file path and is
# adopted as the owner's data the first time someone signs up.
DATA_DIR = Path(os.environ.get("WHOOP_DATA_DIR")
                or Path(os.environ.get("WHOOP_DB", "/data/whoop.db")).parent)
LEGACY_DB = Path(os.environ.get("WHOOP_DB", "/data/whoop.db"))

# Server-wide fallbacks, used only where an account has not set its own.
FALLBACK_TZ = os.environ.get("TZ_NAME", "").strip()
FALLBACK_MAX_HR = float(os.environ["MAX_HR"]) if os.environ.get("MAX_HR") else None
FALLBACK_SLEEP_NEED = (float(os.environ["SLEEP_NEED_HOURS"])
                       if os.environ.get("SLEEP_NEED_HOURS") else None)
# Honoured for anyone upgrading: their existing bridge posts with this until it
# is re-paired. New installs never set it.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")

SESSION_COOKIE = "strap_session"
CLOCK = Clock()          # server-wide, for anything not tied to an account

accounts = Accounts(DATA_DIR / "accounts.db")
stores = StoreRegistry(DATA_DIR, fallback_zone=FALLBACK_TZ,
                       fallback_max_hr=FALLBACK_MAX_HR,
                       fallback_sleep_need=FALLBACK_SLEEP_NEED)

login_limiter = RateLimiter(limit=8, window=300, penalty=300)
pair_limiter = RateLimiter(limit=10, window=300, penalty=300)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Warm each account's recent days in the background before requests land."""
    def warm() -> None:
        try:
            accounts.prune()
            for user in accounts.users():
                if not user["disabled"]:
                    stores.for_user(user).warm()
        except Exception:                      # noqa: BLE001 - never block boot
            log.debug("cache warm-up skipped", exc_info=True)

    threading.Thread(target=warm, name="cache-warmup", daemon=True).start()
    yield


app = FastAPI(title="Whoop Server", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE / "templates"))


# --- who is asking ----------------------------------------------------------
def signed_in(strap_session: str = Cookie(default="")) -> dict[str, Any]:
    """The signed-in account, or 401. Every browser endpoint depends on this."""
    user = accounts.session_user(strap_session)
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    return user


def signed_in_owner(user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    if user.get("role") != "owner":
        raise HTTPException(403, "Only the account owner can do that.")
    return user


def my_store(user: dict[str, Any] = Depends(signed_in)) -> UserStore:
    return stores.for_user(user)


def posting_device(authorization: str = Header(default="")) -> UserStore:
    """The account a bridge is posting for, from its device token.

    A device token names one account, so records land in that person's file
    without the bridge having to say whose they are. INGEST_TOKEN is still
    accepted for an existing single-user install that has not re-paired yet.
    """
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(401, "bad or missing token")
    user = accounts.device_user(token)
    if user is None and INGEST_TOKEN and _constant_eq(token, INGEST_TOKEN):
        user = _legacy_owner()
    if user is None:
        raise HTTPException(401, "bad or missing token")
    return stores.for_user(user)


def _constant_eq(a: str, b: str) -> bool:
    import hmac as _hmac
    return _hmac.compare_digest(a, b)


def _legacy_owner() -> dict[str, Any] | None:
    """The owner, for a bridge still posting with the old shared token."""
    for user in accounts.users():
        if user["role"] == "owner" and not user["disabled"]:
            return user
    return None


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


class IntakeEntry(BaseModel):
    at: str
    substance: str
    amount: float
    label: str = ""


class ManualActivity(BaseModel):
    start_unix: int
    end_unix: int
    activity_type: str
    note: str = ""


def _load_model() -> ActivityClassifier | None:
    stored = db.load_model(MODEL_NAME)
    return ActivityClassifier.from_payload(stored["payload"]) if stored else None


def _parse_date(value: str, store: UserStore | None = None) -> datetime:
    """A YYYY-MM-DD from the app, as local midnight in that account's zone."""
    try:
        naive = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return naive.replace(tzinfo=(store.clock.zone if store else CLOCK.zone))


@app.post("/ingest")
def ingest(batch: Batch, store: UserStore = Depends(posting_device)) -> dict[str, Any]:
    received, inserted = store.db.insert_records(batch.records)
    if inserted:
        store.invalidate(store.touched_dates(batch.records))
    return {"ok": True, "received": received, "inserted": inserted,
            "duplicates": received - inserted}


@app.post("/status")
def device_status(payload: dict[str, Any], store: UserStore = Depends(posting_device)) -> dict[str, Any]:
    """Heartbeat from the laptop bridge. Same token as /ingest."""
    store.db.put_device_status(payload)
    return {"ok": True}


@app.get("/api/device")
def api_device(store: UserStore = Depends(my_store)) -> dict[str, Any]:
    return describe(store.db.get_device_status())


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, **store.db.stats()}


@app.get("/api/day/{date}")
def api_day(date: str, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    day = _parse_date(date, store)
    return store.summarise(day)


@app.get("/api/summary")
def api_summary(days: int = Query(7, ge=1, le=90), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    today = store.today()
    out = [store.summarise(today - timedelta(days=i)) for i in range(days)]
    return {"days": out, "stats": store.db.stats(),
            # The server owns the day boundary, so it also owns what "today"
            # is. A phone in another zone would otherwise disagree by a day.
            "today": today.strftime("%Y-%m-%d"),
            "timezone": CLOCK.name,
            "disclaimer": "Locally computed approximations, not WHOOP's values. "
                          "Not medical measurements."}


@app.get("/api/series")
def api_series(date: str, field: str = "heart_rate", store: UserStore = Depends(my_store)) -> dict[str, Any]:
    allowed = {"heart_rate", "skin_temp_raw", "spo2_red", "spo2_ir",
               "resp_rate_raw", "ambient_light", "signal_quality"}
    if field not in allowed:
        raise HTTPException(400, f"field must be one of {sorted(allowed)}")
    day = _parse_date(date, store)
    lo, hi = store.bounds(day)
    points = [{"t": r["device_unix"], "v": r[field]}
              for r in store.db.range(lo, hi) if r.get(field) is not None]
    return {"date": date, "field": field, "points": points,
            "raw_adc": field in {"skin_temp_raw", "spo2_red", "spo2_ir", "resp_rate_raw"}}


# --- journal ----------------------------------------------------------------
@app.get("/api/journal/{date}")
def api_get_journal(date: str, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    _parse_date(date, store)
    return store.db.get_journal(date) or {"date": date, "tags": [], "amounts": {}, "notes": ""}


@app.put("/api/journal/{date}")
def api_put_journal(date: str, entry: JournalEntry, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    _parse_date(date, store)
    store.db.put_journal(date, entry.tags, entry.amounts, entry.notes)
    return {"ok": True, **(store.db.get_journal(date) or {})}


@app.get("/api/journal")
def api_journal_range(days: int = Query(30, ge=1, le=365), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    today = store.today()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    return {"entries": store.db.journal_range(start, today.strftime("%Y-%m-%d")),
            "known_tags": store.db.all_tags()}


# --- activities -------------------------------------------------------------
def _detect_for_day(day: datetime) -> int:
    """Run detection for a day and store any new bouts. Idempotent."""
    lo, hi = store.bounds(day)
    records = store.db.range(lo, hi)
    if not records:
        return 0
    summary = summarise_day(records, max_hr=store.max_hr, sleep_need_h=store.sleep_need_h)
    resting = summary.get("heart_rate", {}).get("resting") if summary.get("has_data") else None
    model = _load_model()
    found = 0
    for bout in find_bouts(records, resting, store.max_hr):
        label, confidence, _ = classify(bout["features"], bout.get("hint"), model)
        store.db.upsert_activity(bout["start_unix"], bout["end_unix"], label,
                           confidence, bout["features"])
        found += 1
    return found


@app.get("/api/activities")
def api_activities(date: str, detect: bool = True, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    day = _parse_date(date, store)
    if detect:
        _detect_for_day(day)
    lo, hi = store.bounds(day)
    model = _load_model()
    return {
        "date": date,
        "activities": store.db.activities_range(lo, hi),
        "source": "model" if (model and model.weights is not None
                              and (model.accuracy or 0) >= 0.60) else "rules",
    }


@app.post("/api/activities/detect")
def api_detect(date: str, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    return {"ok": True, "date": date, "bouts": _detect_for_day(_parse_date(date, store))}


@app.post("/api/activities")
def api_add_activity(activity: ManualActivity, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    if activity.end_unix <= activity.start_unix:
        raise HTTPException(400, "end_unix must be after start_unix")
    aid = store.db.add_manual_activity(activity.start_unix, activity.end_unix,
                                 activity.activity_type, activity.note)
    return {"ok": True, "id": aid}


@app.patch("/api/activities/{activity_id}")
def api_edit_activity(activity_id: int, edit: ActivityEdit, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    if edit.start_unix is not None and edit.end_unix is not None \
            and edit.end_unix <= edit.start_unix:
        raise HTTPException(400, "end_unix must be after start_unix")
    if not store.db.update_activity(activity_id, confirmed_type=edit.confirmed_type,
                              start_unix=edit.start_unix, end_unix=edit.end_unix,
                              note=edit.note):
        raise HTTPException(404, "activity not found, deleted, or nothing to change")
    return {"ok": True, "id": activity_id}


@app.delete("/api/activities/{activity_id}")
def api_delete_activity(activity_id: int, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    if not store.db.delete_activity(activity_id):
        raise HTTPException(404, "activity not found")
    return {"ok": True, "id": activity_id, "restorable": True}


@app.post("/api/activities/{activity_id}/restore")
def api_restore_activity(activity_id: int, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    if not store.db.restore_activity(activity_id):
        raise HTTPException(404, "activity not found")
    return {"ok": True, "id": activity_id}


# --- learning ---------------------------------------------------------------
@app.post("/api/model/train")
def api_train(store: UserStore = Depends(my_store)) -> dict[str, Any]:
    labelled = store.db.labelled_activities()
    samples = [(list(a["features"].get(n, 0.0) for n in _FEATURES), a["confirmed_type"])
               for a in labelled if a["features"]]
    model = ActivityClassifier()
    report = model.train(samples)
    if report.get("trained"):
        store.db.save_model(MODEL_NAME, model.to_payload(), model.n_samples, model.accuracy)
    return report


@app.get("/api/learning")
def api_learning(days: int = Query(30, ge=7, le=365), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    today = store.today()
    lo, _ = store.bounds(today - timedelta(days=days))
    _, hi = store.bounds(today)

    stored = store.db.load_model(MODEL_NAME)
    detected = len(store.db.activities_range(lo, hi))
    if detected == 0:
        # Nothing detected yet means no basis for a rate estimate. Run detection
        # over a bounded recent window so the first visit shows a real ETA.
        for i in range(1, 8):
            _detect_for_day(today - timedelta(days=i))
        detected = len(store.db.activities_range(lo, hi))
    activity = activity_learning_status(
        store.db.labelled_activities(), detected, days,
        stored["accuracy"] if stored else None)

    entries = store.db.journal_range((today - timedelta(days=days)).strftime("%Y-%m-%d"),
                               today.strftime("%Y-%m-%d"))
    tag_counts: dict[str, int] = {}
    for e in entries:
        for t in e["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    summaries = [store.summarise(today - timedelta(days=i)) for i in range(days)]
    pairs = sum(1 for e in entries
                if any(s["date"] == e["date"] and s.get("has_data") for s in summaries))
    insights = insight_learning_status(pairs, tag_counts, len(entries), days)

    return {"activity_recognition": activity, "lifestyle_insights": insights}


@app.get("/api/insights")
def api_insights(days: int = Query(60, ge=14, le=365),
                 lag: int = Query(1, ge=0, le=3), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    today = store.today()
    summaries = [store.summarise(today - timedelta(days=i)) for i in range(days)]
    entries = store.db.journal_range((today - timedelta(days=days)).strftime("%Y-%m-%d"),
                               today.strftime("%Y-%m-%d"))
    return analyse(summaries, entries, lag_days=lag)


@app.get("/api/advice")
def api_advice(date: str | None = None,
               baseline_days: int = Query(30, ge=14, le=120), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    day = _parse_date(date, store) if date else store.today()
    today = store.summarise(day)
    history = [store.summarise(day - timedelta(days=i)) for i in range(1, baseline_days + 1)]
    return {"date": day.strftime("%Y-%m-%d"), **suggest(today, history)}


# --- detail views -----------------------------------------------------------
@app.get("/api/health-monitor")
def api_health_monitor(date: str | None = None,
                       days: int = Query(30, ge=7, le=120), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    day = _parse_date(date, store) if date else store.today()
    lo, hi = store.bounds(day)
    summary = store.summarise(day)
    records = store.db.range(lo, hi)
    epochs = build_epochs(records)
    sensors = summary.get("sensors") or {}

    channels = [
        channel("Resting HR", "bpm", (summary.get("heart_rate") or {}).get("resting"),
                store.series(day, days, "heart_rate", "resting"), decimals=0),
        channel("HRV", "ms", (summary.get("hrv") or {}).get("rmssd_ms"),
                store.series(day, days, "hrv", "rmssd_ms"), decimals=0),
        channel("Respiratory rate", "", sensors.get("resp_rate_raw"),
                store.series(day, days, "sensors", "resp_rate_raw"), raw=True),
        channel("Blood oxygen", "", sensors.get("spo2_red"),
                store.series(day, days, "sensors", "spo2_red"), raw=True),
        channel("Skin temperature", "", sensors.get("skin_temp_raw"),
                store.series(day, days, "sensors", "skin_temp_raw"), raw=True),
    ]

    hr_points = [{"t": e.unix, "v": e.hr} for e in epochs if e.hr is not None]
    latest = hr_points[-1]["v"] if hr_points else None
    resting = (summary.get("heart_rate") or {}).get("resting")
    zone = None
    if latest is not None and resting:
        reserve = (latest - resting) / max(store.max_hr - resting, 1)
        zone = max(0, min(5, int(reserve * 5) + (1 if reserve > 0.1 else 0)))

    return {
        "date": day.strftime("%Y-%m-%d"),
        "heart_rate": {"latest": latest, "zone": zone,
                       "points": hr_points[-720:],   # cap the payload
                       "min": min((p["v"] for p in hr_points), default=None),
                       "max": max((p["v"] for p in hr_points), default=None)},
        "channels": channels,
        "summary": summarise_channels(channels),
        "baseline_days": days,
        "note": ("Ranges are the 10th-90th percentile of your own recent history, "
                 "not a clinical reference interval. Respiratory rate, blood oxygen "
                 "and skin temperature arrive as raw sensor counts with no "
                 "real-world unit, so a personal range is the only valid comparison "
                 "for them."),
    }


@app.get("/api/stress")
def api_stress(date: str | None = None,
               days: int = Query(STRESS_BASELINE_DAYS, ge=3, le=60), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    day = _parse_date(date, store) if date else store.today()
    lo, hi = store.bounds(day)
    summary = store.summarise(day)
    epochs = build_epochs(store.db.range(lo, hi))
    resting = (summary.get("heart_rate") or {}).get("resting")
    hrv_base = (summary.get("baselines") or {}).get("hrv_rmssd_ms")

    # Build the personal distribution the scale is expressed against.
    reference: list[float] = []
    for i in range(days, 0, -1):
        reference.extend(store.stress_reference_day(day - timedelta(days=i), hrv_base))

    out = stress_day(epochs, resting, store.max_hr, hrv_base, reference)
    out["date"] = day.strftime("%Y-%m-%d")
    out["baseline_days"] = days
    return out


@app.get("/api/fitness-age")
def api_fitness_age(days: int = Query(60, ge=14, le=180), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    today = store.today()
    summary = store.summarise(today)
    resting = (summary.get("baselines") or {}).get("resting_hr") \
        or (summary.get("heart_rate") or {}).get("resting")

    estimate = estimate_vo2max(store.max_hr, resting) if resting else \
        {"usable": False, "reason": "no resting heart rate yet"}

    out: dict[str, Any] = {
        "estimate": estimate,
        "chronological_age": store.age,
        "sex_reference": store.sex,
        "max_hr": store.max_hr,
        "max_hr_measured": bool(os.environ.get("MAX_HR_MEASURED")),
    }
    if estimate.get("usable"):
        out["age"] = fitness_age(estimate["vo2max"], store.sex, store.age)

        # The same estimate applied to each past day, for the trend.
        history = []
        for i in range(days, 0, -1):
            past = store.raw_summary(today - timedelta(days=i))
            rest = (past.get("heart_rate") or {}).get("resting")
            if not rest:
                continue
            e = estimate_vo2max(store.max_hr, rest)
            if e.get("usable"):
                fa = fitness_age(e["vo2max"], store.sex)
                # "edge" travels with the point so the trend can refuse to read a
                # slope through values pinned to the end of the reference table.
                history.append({"days_ago": i, "fitness_age": fa["fitness_age"],
                                "edge": fa["edge"], "vo2max": e["vo2max"]})
        out["trend"] = fitness_trend(history)
        # VO2 max keeps moving even where fitness age is clamped, so it is the
        # honest fallback trend for the very fit and the very unfit.
        out["vo2max_trend"] = vo2max_trend(history)
        out["history"] = history[-days:]
    if not store.age:
        out["needs_age"] = ("Set store.age (and store.sex) on the server to compare "
                            "this against your actual age.")
    out["limits"] = [
        "An estimate from your heart-rate ratio, not a lab test. The method was "
        "validated in well-trained men (r = 0.92); it is weakest for people unlike "
        "that group.",
        ("Maximum heart rate is the 220-age rule unless you have measured yours, "
         "and that rule carries roughly +/- 10-12 bpm of individual scatter, which "
         "passes straight into this number.")
        if not os.environ.get("MAX_HR_MEASURED") else
        "Using your measured maximum heart rate.",
        "This is cardiorespiratory fitness age. It is not biological or epigenetic "
        "age, and no pace-of-ageing multiplier is shown, because nothing derived "
        "from heart rate can support one. The trend below is your own movement.",
    ]
    return out


# --- intake log -------------------------------------------------------------
@app.post("/api/intake")
def api_add_intake(entry: IntakeEntry, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    if entry.substance not in ("caffeine", "alcohol"):
        raise HTTPException(400, "substance must be 'caffeine' or 'alcohol'")
    if entry.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    try:
        datetime.fromisoformat(entry.at)
    except ValueError:
        raise HTTPException(400, "at must be an ISO8601 timestamp")
    return {"ok": True, "id": store.db.add_intake(entry.at, entry.substance,
                                            entry.amount, entry.label)}


@app.get("/api/intake")
def api_intake(date: str, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    day = _parse_date(date, store)
    lo, hi = store.bounds(day)
    return {
        "date": date,
        "entries": store.db.intake_between(
            datetime.fromtimestamp(lo - 86400, timezone.utc).isoformat(),
            datetime.fromtimestamp(hi, timezone.utc).isoformat()),
        "presets": {"caffeine_mg": CAFFEINE_MG, "alcohol_units": ALCOHOL_UNITS},
    }


@app.delete("/api/intake/{intake_id}")
def api_delete_intake(intake_id: int, store: UserStore = Depends(my_store)) -> dict[str, Any]:
    if not store.db.delete_intake(intake_id):
        raise HTTPException(404, "not found")
    return {"ok": True}


# --- advanced analytics -----------------------------------------------------
def _sleep_window(summary: dict[str, Any]) -> tuple[int, int] | None:
    blocks = (summary.get("sleep") or {}).get("blocks") or []
    if not blocks:
        return None
    longest = max(blocks, key=lambda b: b["minutes"])
    return (int(datetime.fromisoformat(longest["start"]).timestamp()),
            int(datetime.fromisoformat(longest["end"]).timestamp()))


@app.get("/api/advanced")
def api_advanced(date: str | None = None,
                 days: int = Query(60, ge=14, le=180), store: UserStore = Depends(my_store)) -> dict[str, Any]:
    """Non-linear HRV, circadian phase, recovery velocity, load and targets."""
    day = _parse_date(date, store) if date else store.today()
    lo, hi = store.bounds(day)
    records = store.db.range(lo, hi)
    summary = store.summarise(day)
    epochs = build_epochs(records)
    out: dict[str, Any] = {"date": day.strftime("%Y-%m-%d")}

    # --- non-linear HRV over the night, where it is least contaminated ---
    window = _sleep_window(summary)
    night_rr: list[float] = []
    if window:
        for e in epochs:
            if window[0] <= e.unix < window[1]:
                night_rr.extend(e.rr)
    if not night_rr:
        for e in epochs:
            night_rr.extend(e.rr)
    out["hrv_nonlinear"] = {
        "source": "sleep" if window else "whole_day",
        "dfa_alpha1": dfa_alpha1(night_rr),
        "sample_entropy": sample_entropy(night_rr),
    }

    # --- circadian phase ---
    out["circadian"] = (hr_trough(epochs, window[0], window[1], CLOCK.offset_hours(day))
                        if window else
                        {"usable": False, "reason": "no sleep block detected"})

    # --- recovery velocity, from the day's hardest bout ---
    activities = store.db.activities_range(lo, hi)
    hardest = max((a for a in activities
                   if (a.get("features") or {}).get("hr_reserve_mean", 0) > 0.35),
                  key=lambda a: a["features"]["hr_reserve_mean"], default=None)
    if hardest:
        out["recovery_velocity"] = {
            "after": hardest.get("confirmed_type") or hardest.get("detected_type"),
            "ended": datetime.fromtimestamp(hardest["end_unix"], timezone.utc).isoformat(),
            **recovery_velocity(epochs, hardest["end_unix"],
                                (summary.get("heart_rate") or {}).get("resting")),
        }
    else:
        out["recovery_velocity"] = {"usable": False,
                                    "reason": "no hard effort detected on this day"}

    # --- load and sleep debt over the window ---
    history = [store.summarise(day - timedelta(days=i)) for i in range(days)][::-1]
    with_data = [h for h in history if h.get("has_data")]
    out["acwr"] = acwr([h["strain"]["score"] for h in with_data
                        if h.get("strain", {}).get("score") is not None])

    need = (summary.get("sleep") or {}).get("need_hours", 8.0) * 60
    nights = [h["sleep"]["total_minutes"] for h in with_data[-21:]
              if h.get("sleep", {}).get("total_minutes") is not None]
    debt = sleep_debt(nights, need)
    out["sleep_debt"] = {"nights_used": len(nights), **payback_plan(debt, need)}

    # --- strain target from your own next-day response ---
    pairs = []
    for i in range(len(with_data) - 1):
        a, b = with_data[i], with_data[i + 1]
        if (a.get("recovery", {}).get("score") is not None
                and a.get("strain", {}).get("score") is not None
                and b.get("recovery", {}).get("score") is not None):
            pairs.append({"recovery": a["recovery"]["score"],
                          "strain": a["strain"]["score"],
                          "next_recovery": b["recovery"]["score"]})
    out["strain_target"] = strain_target(
        (summary.get("recovery") or {}).get("score"), pairs)

    # --- efficiency index over the window ---
    span_lo, _ = store.bounds(day - timedelta(days=days))
    out["efficiency"] = efficiency_index(store.db.activities_range(span_lo, hi),
                                         (summary.get("heart_rate") or {}).get("resting"))

    # --- substances against last night ---
    if window:
        onset = datetime.fromtimestamp(window[0], timezone.utc)
        intakes = store.db.intake_between(
            (onset - timedelta(hours=24)).isoformat(), onset.isoformat())
        out["substances"] = overlay(intakes, onset.isoformat(), summary)
        out["substances"]["curve"] = curve(
            intakes, (onset - timedelta(hours=12)).isoformat(), hours=24)
    else:
        out["substances"] = {"flags": [], "at_sleep_onset": None,
                             "reason": "no sleep block to compare against"}

    return out


# --- bridge update channel --------------------------------------------------
# These use the ingest token rather than the Cloudflare Access check that
# guards /setup: the bridge is not a browser and cannot complete an Access
# login, and the token is exactly the "this is my bridge" credential.
@app.get("/api/bridge/release")
def api_bridge_release(_: UserStore = Depends(posting_device)) -> dict[str, Any]:
    if not bundle_status()["ready"]:
        raise HTTPException(503, "this image has no bridge code to serve")
    return release()


@app.get("/api/bridge/bundle.zip")
def api_bridge_bundle(_: UserStore = Depends(posting_device)) -> Response:
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
def api_bridge_config(_: UserStore = Depends(posting_device)) -> dict[str, Any]:
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


# --- signing in -------------------------------------------------------------
class Credentials(BaseModel):
    email: str = ""
    password: str = ""


class Registration(BaseModel):
    email: str = ""
    password: str = ""
    display_name: str = ""
    timezone: str = ""
    invite: str = ""


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    timezone: str | None = None
    max_hr: float | None = None
    age: float | None = None
    sex: str | None = None
    sleep_need_h: float | None = None


class PasswordChange(BaseModel):
    current: str = ""
    replacement: str = ""


def _is_secure(request: Request) -> bool:
    """Whether this request really arrived over HTTPS.

    Behind the tunnel cloudflared sets X-Forwarded-Proto; on the LAN it is a
    plain HTTP connection. It matters because a Secure cookie is discarded by
    the browser over plain HTTP -- marking it Secure unconditionally would make
    signing in from the LAN silently impossible, with no error to explain it.
    """
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (forwarded or request.url.scheme) == "https"


def _set_session(request: Request, response: Response, token: str) -> None:
    """HttpOnly so script cannot read it, Lax so a cross-site form post cannot
    ride on it, Secure whenever the connection can carry it."""
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=_is_secure(request), max_age=90 * 86400, path="/")


def _client_key(request: Request) -> str:
    return (request.client.host if request.client else "unknown")


@app.get("/api/me")
def api_me(user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    store = stores.for_user(user)
    return {"user": {k: user[k] for k in
                     ("id", "email", "display_name", "timezone", "role",
                      "max_hr", "age", "sex", "sleep_need_h")},
            "timezone": store.clock.name,
            "today": store.today().strftime("%Y-%m-%d"),
            "is_owner": user["role"] == "owner"}


@app.get("/api/session")
def api_session(strap_session: str = Cookie(default="")) -> dict[str, Any]:
    """Who is signed in, if anyone. Never 401s -- the login page asks this."""
    user = accounts.session_user(strap_session)
    return {"signed_in": user is not None,
            "needs_owner": accounts.needs_owner(),
            "display_name": (user or {}).get("display_name", ""),
            "role": (user or {}).get("role", "")}


@app.post("/api/login")
def api_login(creds: Credentials, request: Request, response: Response) -> dict[str, Any]:
    key = normalise_email(creds.email) or _client_key(request)
    wait = login_limiter.blocked_for(key)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {int(wait) // 60 + 1} minute(s).")
    user = accounts.authenticate(creds.email, creds.password)
    if user is None:
        login_limiter.record_failure(key)
        # One message for every failure: a different one for "no such account"
        # would tell anyone who asked which addresses are registered here.
        raise HTTPException(401, "That email and password do not match.")
    login_limiter.record_success(key)
    _set_session(request, response, accounts.start_session(
        user["id"], request.headers.get("user-agent", "")))
    stores.for_user(user)
    return {"ok": True, "display_name": user["display_name"], "role": user["role"]}


@app.post("/api/logout")
def api_logout(response: Response, strap_session: str = Cookie(default="")) -> dict[str, Any]:
    accounts.end_session(strap_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/register")
def api_register(reg: Registration, request: Request, response: Response) -> dict[str, Any]:
    """The first account, or one created from an invite. Nothing else."""
    problem = email_problem(reg.email) or password_problem(reg.password)
    if problem:
        raise HTTPException(400, problem)

    first = accounts.needs_owner()
    if not first and not reg.invite:
        raise HTTPException(403, "This server is invite only. Ask the owner for a link.")

    try:
        if first:
            user = accounts.create_user(reg.email, reg.password,
                                        display_name=reg.display_name,
                                        timezone_name=reg.timezone, role="owner")
            _adopt_legacy_data(user)
        else:
            wait = pair_limiter.blocked_for(_client_key(request))
            if wait:
                raise HTTPException(429, "Too many attempts. Try again shortly.")
            user = accounts.redeem_invite(reg.invite, reg.email, reg.password,
                                          display_name=reg.display_name,
                                          timezone_name=reg.timezone)
    except ValueError as exc:
        if not first:
            pair_limiter.record_failure(_client_key(request))
        raise HTTPException(400, str(exc))

    _set_session(request, response, accounts.start_session(
        user["id"], request.headers.get("user-agent", "")))
    stores.for_user(user)
    return {"ok": True, "display_name": user["display_name"], "role": user["role"]}


def _adopt_legacy_data(user: dict[str, Any]) -> None:
    """Hand the pre-accounts database to the first account that signs up.

    Someone upgrading has months of records in whoop.db. Leaving them behind
    and starting empty would be the worst possible upgrade, so the owner's
    file is that database, moved into place.
    """
    target = stores.path_for(user["id"])
    if target.exists() or not LEGACY_DB.exists() or LEGACY_DB == target:
        return
    try:
        LEGACY_DB.replace(target)
        for suffix in ("-wal", "-shm"):
            side = LEGACY_DB.with_name(LEGACY_DB.name + suffix)
            if side.exists():
                side.replace(target.with_name(target.name + suffix))
        log.info("adopted %s as the owner's data", LEGACY_DB)
    except OSError:
        log.warning("could not adopt %s; starting empty", LEGACY_DB, exc_info=True)


@app.post("/api/password")
def api_change_password(change: PasswordChange,
                        user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    if accounts.authenticate(user["email"], change.current) is None:
        raise HTTPException(401, "That is not your current password.")
    problem = password_problem(change.replacement)
    if problem:
        raise HTTPException(400, problem)
    accounts.set_password(user["id"], change.replacement)
    return {"ok": True, "note": "Signed out everywhere. Sign in again with the new password."}


@app.patch("/api/me")
def api_update_profile(update: ProfileUpdate,
                       user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if "timezone" in fields and fields["timezone"]:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(fields["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(400, "That is not a timezone name. Try America/Chicago.")
    updated = accounts.update_profile(user["id"], **fields)
    stores.for_user(updated)          # picks up a new zone or max heart rate
    return {"ok": True, "user": updated}


# --- invites ----------------------------------------------------------------
@app.get("/api/invites")
def api_invites(user: dict[str, Any] = Depends(signed_in_owner)) -> dict[str, Any]:
    return {"invites": accounts.invites(user["id"]),
            "members": [{k: u[k] for k in ("id", "email", "display_name", "role", "disabled")}
                        for u in accounts.users()]}


@app.post("/api/invites")
def api_create_invite(request: Request, label: str = Form(default=""),
                      user: dict[str, Any] = Depends(signed_in_owner)) -> dict[str, Any]:
    token = accounts.create_invite(user["id"], label)
    base = public_base_url(dict(request.headers), _fallback_base())
    return {"ok": True, "url": f"{base}/join/{token}",
            "note": "Anyone with this link can create an account. It works once "
                    "and expires in a week."}


@app.post("/api/members/{member_id}/disable")
def api_disable_member(member_id: int, disabled: bool = True,
                       user: dict[str, Any] = Depends(signed_in_owner)) -> dict[str, Any]:
    if member_id == user["id"]:
        raise HTTPException(400, "You cannot disable your own account.")
    accounts.set_disabled(member_id, disabled)
    return {"ok": True}


# --- pairing a laptop -------------------------------------------------------
class PairClaim(BaseModel):
    code: str = ""
    device_name: str = ""


@app.post("/api/pair/start")
def api_pair_start(user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    code, expires = accounts.start_pairing(user["id"])
    return {"code": code, "expires_at": expires,
            "note": "Type this into the setup app on your laptop. It works once."}


@app.post("/pair/claim")
def api_pair_claim(claim: PairClaim, request: Request) -> dict[str, Any]:
    """Exchange a pairing code for this laptop's own token.

    The one write that cannot require a session: the laptop has no credential
    yet, which is the whole point. Single use, short lived, rate limited, and
    a wrong code says nothing about whether any code is live.
    """
    key = _client_key(request)
    wait = pair_limiter.blocked_for(key)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {int(wait) // 60 + 1} minute(s).")
    got = accounts.claim_pairing(claim.code, claim.device_name)
    if got is None:
        pair_limiter.record_failure(key)
        raise HTTPException(400, "That code is not valid, has been used, or has expired.")
    pair_limiter.record_success(key)
    user = got["user"]
    stores.for_user(user)
    return {"ok": True, "token": got["token"], "device_id": got["device_id"],
            "account": user["email"], "display_name": user["display_name"],
            "ingest_url": public_base_url(dict(request.headers), _fallback_base()) + "/ingest"}


@app.get("/api/devices")
def api_devices(user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    return {"devices": accounts.devices(user["id"])}


@app.delete("/api/devices/{device_id}")
def api_revoke_device(device_id: int,
                      user: dict[str, Any] = Depends(signed_in)) -> dict[str, Any]:
    if not accounts.revoke_device(user["id"], device_id):
        raise HTTPException(404, "No such laptop on your account.")
    return {"ok": True}


# --- pages ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, strap_session: str = Cookie(default="")) -> Any:
    """The app, or the way in. Never a blank page behind a 401."""
    if accounts.session_user(strap_session) is None:
        return RedirectResponse("/welcome" if accounts.needs_owner() else "/signin",
                                status_code=303)
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request, strap_session: str = Cookie(default="")) -> Any:
    if accounts.needs_owner():
        return RedirectResponse("/welcome", status_code=303)
    if accounts.session_user(strap_session) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "auth.html", {
        "mode": "login", "heading": "Welcome back",
        "lede": "Sign in to see your recovery, sleep and strain.",
        "action": "Sign in", "invite": "", "inviter": ""})


@app.get("/welcome", response_class=HTMLResponse)
def welcome_page(request: Request) -> Any:
    """First run. Whoever gets here first becomes the owner."""
    if not accounts.needs_owner():
        return RedirectResponse("/signin", status_code=303)
    return templates.TemplateResponse(request, "auth.html", {
        "mode": "owner", "heading": "Set up your server",
        "lede": "Create the first account. It becomes the owner of this server.",
        "action": "Create account", "invite": "", "inviter": ""})


@app.get("/join/{token}", response_class=HTMLResponse)
def join_page(request: Request, token: str) -> Any:
    if accounts.needs_owner():
        return RedirectResponse("/welcome", status_code=303)
    ok, why = accounts.invite_status(token)
    if not ok:
        return templates.TemplateResponse(request, "auth.html", {
            "mode": "login", "heading": "That link did not work",
            "lede": why + " If you already have an account, sign in below.",
            "action": "Sign in", "invite": "", "inviter": ""}, status_code=400)
    owner = next((u for u in accounts.users() if u["role"] == "owner"), None)
    return templates.TemplateResponse(request, "auth.html", {
        "mode": "join", "heading": "Create your account",
        "lede": "Your own account on this server, with your own data.",
        "action": "Create account", "invite": token,
        "inviter": (owner or {}).get("display_name") or "this"})


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    """Makes Add to Home Screen give a real icon and launch without Safari's
    chrome, which is as close to an installed app as iOS allows a web app."""
    icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='0 0 512 512'%3E%3Crect width='512' height='512' rx='114' "
            "fill='%23000'/%3E%3Cpath d='M80 304h67l34-96 54 160 53-208 42 144h102' "
            "fill='none' stroke='%2300FF87' stroke-width='34' stroke-linecap='round' "
            "stroke-linejoin='round'/%3E%3C/svg%3E")
    return Response(
        content=json.dumps({
            "name": "Strap", "short_name": "Strap",
            "description": "Your strap's data, on your own server.",
            "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#000000", "theme_color": "#000000",
            "orientation": "portrait",
            "icons": [{"src": icon, "sizes": "512x512", "type": "image/svg+xml",
                       "purpose": "any maskable"}],
        }),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"})
