"""Derived metrics computed locally from raw strap data.

IMPORTANT: WHOOP computes recovery, strain and sleep in its own cloud using
undisclosed models. Nothing here reproduces those. These are independent
approximations built from published sports-science methods, computed from the
heart rate, RR intervals and accelerometer data the strap actually sends.
They are directionally useful and internally consistent, but they will NOT
match the numbers the official WHOOP app shows, and they are not medical
measurements.

Methods used:
  HRV      RMSSD over RR intervals, with Malik artifact filtering.
  Sleep    Actigraphy-style: low motion + low HR + on-wrist, in 1-minute epochs.
  Strain   Banister TRIMP, mapped onto a 0-21 logarithmic scale.
  Recovery Weighted comparison of today's HRV / resting HR / sleep against a
           rolling personal baseline.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

# Physiologically plausible RR bounds (ms). Anything outside is an artifact.
RR_MIN, RR_MAX = 300, 2000
MALIK_THRESHOLD = 0.20      # reject beats differing >20% from the previous one

EPOCH_SECONDS = 60
SLEEP_MIN_BLOCK_MIN = 20    # ignore blocks shorter than this
SLEEP_MERGE_GAP_MIN = 15    # brief wakes inside a sleep block
# A day still filling up should not present a confident recovery score. Without
# a night's sleep to score against, and with only minutes of daytime data, the
# number is dominated by noise and reads as alarming rather than incomplete.
MIN_EPOCHS_FOR_RECOVERY = 120
DEFAULT_SLEEP_NEED_H = 8.0


# --- HRV --------------------------------------------------------------------
def malik_filter(rr: Sequence[float]) -> list[float]:
    """Drop physiologically implausible beats and Malik-rule outliers."""
    clean = [v for v in rr if RR_MIN <= v <= RR_MAX]
    if len(clean) < 2:
        return clean
    out = [clean[0]]
    for v in clean[1:]:
        if abs(v - out[-1]) <= MALIK_THRESHOLD * out[-1]:
            out.append(v)
    return out


def rmssd(rr: Sequence[float]) -> float | None:
    """Root mean square of successive differences, in ms.

    Exactly zero is not a measurement. A living heart does not produce
    identical consecutive intervals, so a flat series means a stuck or
    synthesised source, and reporting "0.0 ms" would read as catastrophic
    autonomic failure rather than as a sensor problem.
    """
    clean = malik_filter(rr)
    if len(clean) < 3:
        return None
    diffs = [b - a for a, b in zip(clean, clean[1:])]
    value = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return round(value, 1) if value > 0 else None


def sdnn(rr: Sequence[float]) -> float | None:
    clean = malik_filter(rr)
    if len(clean) < 3:
        return None
    return round(statistics.stdev(clean), 1)


def pnn50(rr: Sequence[float]) -> float | None:
    clean = malik_filter(rr)
    if len(clean) < 3:
        return None
    diffs = [abs(b - a) for a, b in zip(clean, clean[1:])]
    return round(100.0 * sum(1 for d in diffs if d > 50) / len(diffs), 1)


# --- epochs -----------------------------------------------------------------
@dataclass
class Epoch:
    unix: int
    hr: float | None
    motion: float          # mean |delta gravity| across the epoch
    on_wrist: bool
    rr: list[float]


def build_epochs(records: list[dict[str, Any]]) -> list[Epoch]:
    """Bucket records into fixed one-minute epochs.

    Motion is the movement of the gravity vector between consecutive samples.
    The delta is carried ACROSS epoch boundaries: the strap may emit only one
    record per minute, and computing deltas within an epoch alone would then
    always yield zero, silently disabling every motion-based rule downstream.
    """
    ordered = sorted((r for r in records if r.get("device_unix")),
                     key=lambda r: r["device_unix"])

    # Per-record motion, measured against the previous record with a vector.
    deltas: dict[int, float] = {}
    prev: tuple[float, float, float] | None = None
    for r in ordered:
        if r.get("gravity_x") is None:
            continue
        vec = (r["gravity_x"], r["gravity_y"], r["gravity_z"])
        if prev is not None:
            deltas[id(r)] = math.dist(prev, vec)
        prev = vec

    buckets: dict[int, list[dict]] = {}
    for r in ordered:
        buckets.setdefault(r["device_unix"] // EPOCH_SECONDS * EPOCH_SECONDS, []).append(r)

    epochs = []
    for unix in sorted(buckets):
        rows = buckets[unix]
        hrs = [r["heart_rate"] for r in rows if r.get("heart_rate")]
        rr: list[float] = []
        for r in rows:
            rr.extend(r.get("rr_intervals_ms") or [])

        moves = [deltas[id(r)] for r in rows if id(r) in deltas]
        contact = [r["skin_contact"] for r in rows if r.get("skin_contact") is not None]
        epochs.append(Epoch(
            unix=unix,
            hr=round(statistics.mean(hrs), 1) if hrs else None,
            motion=round(statistics.mean(moves), 4) if moves else 0.0,
            on_wrist=(statistics.mean(contact) > 0.5) if contact else True,
            rr=rr,
        ))
    return epochs


# --- sleep ------------------------------------------------------------------
def detect_sleep(epochs: list[Epoch], resting_hr: float | None,
                 motion_threshold: float = 0.05) -> list[dict[str, Any]]:
    """Actigraphy-style sleep blocks: on-wrist, low motion, low heart rate."""
    if not epochs:
        return []
    hr_ceiling = (resting_hr * 1.15) if resting_hr else 70.0

    asleep = [
        e.on_wrist and e.motion < motion_threshold and (e.hr is None or e.hr <= hr_ceiling)
        for e in epochs
    ]

    # Collect contiguous runs, allowing short gaps (brief awakenings).
    blocks: list[list[int]] = []
    start = None
    gap = 0
    for i, flag in enumerate(asleep):
        if flag:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > SLEEP_MERGE_GAP_MIN:
                blocks.append([start, i - gap])
                start, gap = None, 0
    if start is not None:
        blocks.append([start, len(asleep) - 1])

    out = []
    for lo, hi in blocks:
        minutes = hi - lo + 1
        if minutes < SLEEP_MIN_BLOCK_MIN:
            continue
        window = epochs[lo:hi + 1]
        moving = sum(1 for e in window if e.motion >= motion_threshold)
        out.append({
            "start": _iso(epochs[lo].unix),
            "end": _iso(epochs[hi].unix + EPOCH_SECONDS),
            "minutes": minutes,
            # Time actually still within the block, as a share of it.
            "efficiency": round(100.0 * (minutes - moving) / minutes, 1),
            "disturbances": moving,
            "avg_hr": _mean([e.hr for e in window if e.hr]),
        })
    return out


# --- strain -----------------------------------------------------------------
def trimp(epochs: list[Epoch], resting_hr: float, max_hr: float) -> float:
    """Banister TRIMP: minutes weighted by exponential heart-rate reserve."""
    if max_hr <= resting_hr:
        return 0.0
    total = 0.0
    for e in epochs:
        if e.hr is None:
            continue
        reserve = (e.hr - resting_hr) / (max_hr - resting_hr)
        reserve = min(max(reserve, 0.0), 1.0)
        total += (EPOCH_SECONDS / 60.0) * reserve * 0.64 * math.exp(1.92 * reserve)
    return total


# Saturation constant for the strain curve, in TRIMP units. Calibrated so a
# sedentary day lands near 9-10, a day with one hard hour near 14, and only a
# genuinely brutal day approaches 21. A plain log mapping was tried first and
# rejected: it compressed so hard that an ordinary day scored 20+/21.
STRAIN_K = 245.0


def strain_score(trimp_value: float, k: float = STRAIN_K) -> float:
    """Map TRIMP onto a 0-21 scale with diminishing returns.

    Exponential saturation: strain = 21 * (1 - e^(-TRIMP/k)). Like WHOOP's
    published scale this is non-linear with diminishing returns at the top,
    but the curve is our own convention, not a reproduction of their model.
    """
    if trimp_value <= 0:
        return 0.0
    return round(21.0 * (1.0 - math.exp(-trimp_value / k)), 1)


# --- recovery ---------------------------------------------------------------
def _ratio_score(value: float | None, baseline: float | None,
                 higher_is_better: bool = True, span: float = 0.30) -> float | None:
    """Score `value` against `baseline` on 0-100, saturating at +/- `span`."""
    if value is None or not baseline:
        return None
    delta = (value - baseline) / baseline
    if not higher_is_better:
        delta = -delta
    return round(min(max(50.0 + 50.0 * (delta / span), 0.0), 100.0), 1)


def recovery_score(hrv: float | None, hrv_baseline: float | None,
                   rhr: float | None, rhr_baseline: float | None,
                   sleep_performance: float | None) -> dict[str, Any]:
    """Weighted 0-100 recovery estimate. Components missing -> reweighted."""
    parts = {
        "hrv": (_ratio_score(hrv, hrv_baseline, True), 0.50),
        "resting_hr": (_ratio_score(rhr, rhr_baseline, False), 0.25),
        "sleep": (sleep_performance, 0.25),
    }
    usable = {k: (v, w) for k, (v, w) in parts.items() if v is not None}
    if not usable:
        return {"score": None, "components": {k: v for k, (v, _) in parts.items()},
                "note": "not enough data yet"}
    total_w = sum(w for _, w in usable.values())
    score = sum(v * w for v, w in usable.values()) / total_w
    return {
        "score": round(score),
        "band": "green" if score >= 67 else "yellow" if score >= 34 else "red",
        "components": {k: v for k, (v, _) in parts.items()},
        "weights_used": {k: round(w / total_w, 3) for k, (_, w) in usable.items()},
    }


# --- day rollup -------------------------------------------------------------
def summarise_day(records: list[dict[str, Any]], *, max_hr: float = 190.0,
                  sleep_need_h: float = DEFAULT_SLEEP_NEED_H,
                  hrv_baseline: float | None = None,
                  rhr_baseline: float | None = None) -> dict[str, Any]:
    """Compute one day's metrics from its raw records."""
    epochs = build_epochs(records)
    if not epochs:
        return {"has_data": False}

    all_rr: list[float] = []
    for e in epochs:
        all_rr.extend(e.rr)

    hrs = [e.hr for e in epochs if e.hr is not None]
    # Resting HR: the 5th percentile of on-wrist epochs is more robust than the
    # single lowest reading, which is often an artifact.
    quiet = sorted(e.hr for e in epochs if e.hr is not None and e.on_wrist)
    resting = round(quiet[max(0, int(len(quiet) * 0.05))], 1) if quiet else None

    # Threshold sleep against the rolling baseline resting HR, not today's.
    # Using today's makes detected sleep duration move with today's physiology:
    # a night with elevated resting HR raises the ceiling and so "detects" more
    # sleep, manufacturing a correlation between anything that raises resting
    # HR and apparent sleep duration. The baseline breaks that feedback loop.
    sleep_blocks = detect_sleep(epochs, rhr_baseline or resting)
    sleep_minutes = sum(b["minutes"] for b in sleep_blocks)
    sleep_perf = round(min(100.0, 100.0 * sleep_minutes / (sleep_need_h * 60)), 1) \
        if sleep_blocks else None

    # HRV is conventionally measured during sleep; fall back to the whole day.
    sleep_rr: list[float] = []
    if sleep_blocks:
        spans = [(_unix(b["start"]), _unix(b["end"])) for b in sleep_blocks]
        for e in epochs:
            if any(lo <= e.unix < hi for lo, hi in spans):
                sleep_rr.extend(e.rr)
    hrv_rr = sleep_rr or all_rr

    tr = trimp(epochs, resting or 50.0, max_hr)

    # Sensor channels the strap reports as raw ADC counts. They cannot be
    # converted to real units without WHOOP's calibration, but a count compared
    # against your OWN baseline is still meaningful, which is what the illness
    # signal uses. Measured over the sleep window where available, since these
    # are far more stable at rest than during the day.
    sleep_spans = [(_unix(b["start"]), _unix(b["end"])) for b in sleep_blocks]
    def _sensor(field: str) -> float | None:
        vals = [r[field] for r in records
                if r.get(field) is not None
                and (not sleep_spans
                     or any(lo <= (r.get("device_unix") or 0) < hi for lo, hi in sleep_spans))]
        return round(statistics.mean(vals), 1) if len(vals) >= 10 else None

    return {
        "has_data": True,
        "epochs": len(epochs),
        "heart_rate": {
            "avg": _mean(hrs), "min": min(hrs) if hrs else None,
            "max": max(hrs) if hrs else None, "resting": resting,
        },
        "hrv": {
            "rmssd_ms": rmssd(hrv_rr), "sdnn_ms": sdnn(hrv_rr),
            "pnn50_pct": pnn50(hrv_rr), "beats_used": len(malik_filter(hrv_rr)),
            "source": "sleep" if sleep_rr else "whole_day",
        },
        "sleep": {
            "blocks": sleep_blocks, "total_minutes": sleep_minutes,
            "performance_pct": sleep_perf, "need_hours": sleep_need_h,
        },
        "strain": {"score": strain_score(tr), "trimp": round(tr, 1), "scale_max": 21},
        "recovery": (recovery_score(rmssd(hrv_rr), hrv_baseline, resting,
                                    rhr_baseline, sleep_perf)
                     if (sleep_blocks or len(epochs) >= MIN_EPOCHS_FOR_RECOVERY)
                     else {"score": None, "components": {},
                           "note": "day still filling up"}),
        "partial": not sleep_blocks and len(epochs) < MIN_EPOCHS_FOR_RECOVERY,
        "sensors": {
            "skin_temp_raw": _sensor("skin_temp_raw"),
            "resp_rate_raw": _sensor("resp_rate_raw"),
            "spo2_red": _sensor("spo2_red"),
            "spo2_ir": _sensor("spo2_ir"),
            "measured_over": "sleep" if sleep_spans else "whole_day",
            "units": "raw ADC counts - comparable to your own baseline only",
        },
        "wear": {
            "on_wrist_pct": round(100.0 * sum(1 for e in epochs if e.on_wrist)
                                  / len(epochs), 1),
        },
    }


# --- helpers ----------------------------------------------------------------
def _iso(unix: int) -> str:
    return datetime.fromtimestamp(unix, timezone.utc).isoformat()


def _unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 1) if vals else None
