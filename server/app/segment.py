"""Detect bouts of activity from sensor data and describe them numerically.

A "bout" is a contiguous stretch that stands out from the day: elevated heart
rate, sustained movement, or both. Detection here is deliberately dumb and
recall-oriented -- it finds candidate bouts, and the classifier in ml.py
decides what each one *is*. Keeping the two separate means a mislabelled bout
can be corrected without re-running detection.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from .analytics import EPOCH_SECONDS, Epoch, build_epochs, detect_sleep, rmssd

# A bout must be at least this long, and gaps shorter than this are bridged
# (catching your breath mid-workout should not split it in two).
MIN_BOUT_MIN = 8
MERGE_GAP_MIN = 5

# An epoch counts as "active" above either threshold.
HR_RESERVE_ACTIVE = 0.30
MOTION_ACTIVE = 0.18

FEATURE_NAMES = (
    "duration_min", "hr_mean", "hr_max", "hr_reserve_mean", "hr_reserve_max",
    "hr_rise", "motion_mean", "motion_std", "motion_p90",
    "hour_sin", "hour_cos", "on_wrist_frac", "rmssd_ms", "hr_recovery",
)


def _p(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def bout_features(window: list[Epoch], resting_hr: float, max_hr: float,
                  after: list[Epoch] | None = None) -> dict[str, float]:
    """Describe one bout as a fixed-length numeric vector."""
    hrs = [e.hr for e in window if e.hr is not None]
    motions = [e.motion for e in window]
    reserve_span = max(max_hr - resting_hr, 1.0)
    reserves = [min(max((h - resting_hr) / reserve_span, 0.0), 1.5) for h in hrs]

    rr: list[float] = []
    for e in window:
        rr.extend(e.rr)

    # Time of day as a cyclic pair, so 23:00 and 01:00 are near each other.
    hour = (window[0].unix % 86400) / 3600.0
    angle = 2 * math.pi * hour / 24.0

    # How fast heart rate falls in the minutes after the bout -- a strong
    # signal for real exertion versus incidental movement.
    hr_recovery = 0.0
    if after and hrs:
        post = [e.hr for e in after[:5] if e.hr is not None]
        if post:
            hr_recovery = max(0.0, hrs[-1] - min(post))

    return {
        "duration_min": float(len(window)),
        "hr_mean": round(statistics.mean(hrs), 2) if hrs else 0.0,
        "hr_max": float(max(hrs)) if hrs else 0.0,
        "hr_reserve_mean": round(statistics.mean(reserves), 4) if reserves else 0.0,
        "hr_reserve_max": round(max(reserves), 4) if reserves else 0.0,
        "hr_rise": round(max(hrs) - hrs[0], 2) if hrs else 0.0,
        "motion_mean": round(statistics.mean(motions), 4) if motions else 0.0,
        "motion_std": round(statistics.pstdev(motions), 4) if len(motions) > 1 else 0.0,
        "motion_p90": round(_p(motions, 0.9), 4),
        "hour_sin": round(math.sin(angle), 4),
        "hour_cos": round(math.cos(angle), 4),
        "on_wrist_frac": round(sum(1 for e in window if e.on_wrist) / len(window), 3),
        "rmssd_ms": rmssd(rr) or 0.0,
        "hr_recovery": round(hr_recovery, 2),
    }


def to_vector(features: dict[str, float]) -> list[float]:
    """Fixed ordering, so stored vectors stay comparable across versions."""
    return [float(features.get(n, 0.0)) for n in FEATURE_NAMES]


def find_bouts(records: list[dict[str, Any]], resting_hr: float | None,
               max_hr: float = 190.0) -> list[dict[str, Any]]:
    """Find candidate activity bouts, plus any sleep blocks, in one day."""
    epochs = build_epochs(records)
    if not epochs:
        return []
    rest = resting_hr or 55.0
    reserve_span = max(max_hr - rest, 1.0)

    active = [
        ((e.hr - rest) / reserve_span >= HR_RESERVE_ACTIVE if e.hr is not None else False)
        or e.motion >= MOTION_ACTIVE
        for e in epochs
    ]

    bouts: list[dict[str, Any]] = []
    for lo, hi in _runs(active, MERGE_GAP_MIN):
        if hi - lo + 1 < MIN_BOUT_MIN:
            continue
        window = epochs[lo:hi + 1]
        bouts.append({
            "start_unix": window[0].unix,
            "end_unix": window[-1].unix + EPOCH_SECONDS,
            "features": bout_features(window, rest, max_hr, after=epochs[hi + 1:hi + 6]),
        })

    # Sleep is detected by its own (inverse) logic, not the activity rule.
    for block in detect_sleep(epochs, resting_hr):
        lo_u = _unix(block["start"])
        hi_u = _unix(block["end"])
        window = [e for e in epochs if lo_u <= e.unix < hi_u]
        if window:
            bouts.append({
                "start_unix": lo_u, "end_unix": hi_u,
                "features": bout_features(window, rest, max_hr),
                "hint": "sleep",
            })

    return sorted(bouts, key=lambda b: b["start_unix"])


def _runs(flags: list[bool], merge_gap: int) -> list[tuple[int, int]]:
    """Contiguous True runs, bridging gaps up to `merge_gap` long."""
    runs: list[tuple[int, int]] = []
    start, gap = None, 0
    for i, flag in enumerate(flags):
        if flag:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > merge_gap:
                runs.append((start, i - gap))
                start, gap = None, 0
    if start is not None:
        runs.append((start, len(flags) - 1 - (gap if gap else 0)))
    return runs


def rule_classify(features: dict[str, float], hint: str | None = None) -> tuple[str, float]:
    """Fallback classifier used until there are enough user labels to learn from.

    Intentionally conservative: it returns low confidence so the dashboard asks
    you to confirm, which is what generates the training data.
    """
    if hint == "sleep":
        return "sleep", 0.80

    reserve = features.get("hr_reserve_mean", 0.0)
    motion = features.get("motion_mean", 0.0)
    duration = features.get("duration_min", 0.0)

    if reserve >= 0.65:
        return "hard_workout", 0.55
    if reserve >= 0.40:
        return ("workout", 0.50) if motion >= 0.20 else ("cardio", 0.45)
    if motion >= 0.30 and reserve >= 0.15:
        return "walk", 0.45
    if duration >= 30 and motion < 0.10:
        return "sedentary", 0.40
    return "other", 0.30


def _unix(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp())
