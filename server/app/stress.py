"""A stress signal on your own 0-3 scale.

WHOOP's Stress Monitor is a proprietary model. This is not it, and does not
claim to be. What it is: the two things that reliably move together under
autonomic arousal -- heart rate rising above your resting level, and
short-term HRV falling below your own baseline -- combined and then expressed
as a position within *your own* historical range.

That last part matters. There is no absolute scale for "stress" from a wrist
sensor, so a raw number would be arbitrary. Scoring against your own trailing
distribution means 0 and 3 mean something concrete: the calm and busy ends of
your own range. It also means the scale is meaningless until there is enough
history to build that distribution from.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Sequence

from .analytics import EPOCH_SECONDS, Epoch, malik_filter

# Weights on the two contributors. Heart rate is the steadier signal on a wrist
# optical sensor, so it carries more.
W_HR, W_HRV = 0.6, 0.4
SMOOTH_MINUTES = 5
MIN_BASELINE_EPOCHS = 600      # roughly ten hours of history before scoring
SCALE_MAX = 3.0
BANDS = ((1.0, "low"), (2.0, "medium"), (SCALE_MAX + 1, "high"))


def _epoch_rmssd(epoch: Epoch) -> float | None:
    clean = malik_filter(epoch.rr)
    if len(clean) < 3:
        return None
    diffs = [b - a for a, b in zip(clean, clean[1:])]
    value = (sum(d * d for d in diffs) / len(diffs)) ** 0.5
    return value if value > 0 else None


def raw_series(epochs: Sequence[Epoch], resting_hr: float, max_hr: float,
               rmssd_baseline: float | None) -> list[tuple[int, float]]:
    """Unscaled arousal per epoch: (unix, raw)."""
    span = max(max_hr - resting_hr, 1.0)
    out: list[tuple[int, float]] = []
    for e in epochs:
        if e.hr is None:
            continue
        hr_part = min(max((e.hr - resting_hr) / span, 0.0), 1.0)
        hrv_part = 0.0
        if rmssd_baseline:
            r = _epoch_rmssd(e)
            if r is not None:
                hrv_part = min(max(1.0 - (r / rmssd_baseline), 0.0), 1.0)
        weight_hr, weight_hrv = (W_HR, W_HRV) if rmssd_baseline else (1.0, 0.0)
        out.append((e.unix, weight_hr * hr_part + weight_hrv * hrv_part))
    return out


def _smooth(series: list[tuple[int, float]], window: int) -> list[tuple[int, float]]:
    out = []
    for i, (unix, _) in enumerate(series):
        lo, hi = max(0, i - window // 2), min(len(series), i + window // 2 + 1)
        out.append((unix, statistics.mean(v for _, v in series[lo:hi])))
    return out


def _band(value: float) -> str:
    for edge, name in BANDS:
        if value < edge:
            return name
    return "high"


def stress_day(epochs: Sequence[Epoch], resting_hr: float | None, max_hr: float,
               rmssd_baseline: float | None,
               baseline_raw: Sequence[float]) -> dict[str, Any]:
    """Today's stress series, current value and time in each band."""
    out: dict[str, Any] = {"usable": False, "scale_max": SCALE_MAX}
    if resting_hr is None:
        out["reason"] = "needs a resting heart rate for today"
        return out
    if len(baseline_raw) < MIN_BASELINE_EPOCHS:
        out["reason"] = (f"needs about {MIN_BASELINE_EPOCHS // 60} hours of history to "
                         f"know your own range; have {len(baseline_raw) // 60}")
        return out

    series = _smooth(raw_series(epochs, resting_hr, max_hr, rmssd_baseline),
                     SMOOTH_MINUTES)
    if not series:
        out["reason"] = "no heart rate recorded for this day"
        return out

    # Percentile within your own distribution, mapped onto 0-3.
    reference = sorted(baseline_raw)

    def scaled(v: float) -> float:
        lo, hi = 0, len(reference)
        while lo < hi:
            mid = (lo + hi) // 2
            if reference[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        return round(SCALE_MAX * lo / len(reference), 2)

    points = [{"t": unix, "v": scaled(v)} for unix, v in series]
    minutes = {"low": 0, "medium": 0, "high": 0}
    for p in points:
        minutes[_band(p["v"])] += EPOCH_SECONDS // 60

    current = points[-1]["v"]
    total = max(1, sum(minutes.values()))
    out.update({
        "usable": True,
        "current": current,
        "band": _band(current),
        "updated": datetime.fromtimestamp(points[-1]["t"], timezone.utc).isoformat(),
        "points": points,
        "minutes": minutes,
        "share": {k: round(100 * v / total) for k, v in minutes.items()},
        "average": round(statistics.mean(p["v"] for p in points), 2),
        "peak": round(max(p["v"] for p in points), 2),
        "note": _summary(minutes, total),
        "method": ("Heart rate above your resting level and short-term HRV below "
                   "your baseline, combined and placed within your own historical "
                   "range. Not WHOOP's Stress Score, and not a clinical measure."),
    })
    return out


def _summary(minutes: dict[str, int], total: int) -> str:
    dominant = max(minutes, key=lambda k: minutes[k])
    high = minutes["high"]
    text = f"Most of the day sat in the {dominant} band."
    if high >= 15:
        text += (f" About {high // 60}h {high % 60}m in the high band"
                 if high >= 60 else f" About {high} minutes in the high band")
        text += " — worth knowing what that was."
    return text
