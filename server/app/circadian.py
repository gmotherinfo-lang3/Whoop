"""Circadian phase from the shape of the night, and post-effort HR recovery.

Two things that fall out of per-minute heart rate once sleep is segmented:

* **Where the nightly HR trough sits.** In a well-aligned night the lowest
  heart rate arrives in the first half of sleep. A trough pushed into the
  second half is the signature of the body still working when it should be
  settling -- late eating, alcohol, a warm room, a late-shifted body clock.
  This measures the timing, not the cause; it cannot tell those apart.

* **How fast heart rate falls after effort.** Parasympathetic reactivation
  shows up as a steep drop in the first minutes after a bout ends, and it is
  one of the few fitness markers that improves visibly within weeks.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Sequence

from .analytics import EPOCH_SECONDS, Epoch

SMOOTH_MINUTES = 15          # rolling window; a single dip is not a trough
MIN_SLEEP_MINUTES = 180      # below this the shape of the night means little
HRR_WINDOWS = (1, 5, 15)     # minutes after a bout ends


def _smooth(values: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        lo, hi = max(0, i - window // 2), min(len(values), i + window // 2 + 1)
        chunk = [v for v in values[lo:hi] if v is not None]
        out.append(statistics.mean(chunk) if chunk else None)
    return out


def hr_trough(epochs: Sequence[Epoch], sleep_start: int, sleep_end: int,
              tz_offset_h: float = 0.0) -> dict[str, Any]:
    """Where in the night heart rate bottoms out."""
    window = [e for e in epochs if sleep_start <= e.unix < sleep_end]
    minutes = len(window)
    out: dict[str, Any] = {"usable": False, "sleep_minutes": minutes}
    if minutes < MIN_SLEEP_MINUTES:
        out["reason"] = f"needs {MIN_SLEEP_MINUTES} min of sleep, have {minutes}"
        return out

    smoothed = _smooth([e.hr for e in window], SMOOTH_MINUTES)
    pairs = [(v, i) for i, v in enumerate(smoothed) if v is not None]
    if len(pairs) < MIN_SLEEP_MINUTES // 2:
        out["reason"] = "not enough heart-rate coverage across the night"
        return out

    low, index = min(pairs)
    position = index / max(1, len(window) - 1)
    trough_unix = window[index].unix
    local = datetime.fromtimestamp(trough_unix + tz_offset_h * 3600, timezone.utc)

    out.update({
        "usable": True,
        "trough_hr": round(low, 1),
        "trough_at": datetime.fromtimestamp(trough_unix, timezone.utc).isoformat(),
        "trough_local_hour": round(local.hour + local.minute / 60, 2),
        # 0 = at sleep onset, 1 = at wake.
        "position_in_night": round(position, 3),
        "half": "first" if position < 0.5 else "second",
        "aligned": position < 0.5,
        "note": ("Lowest heart rate came in the first half of the night, which is "
                 "the settled pattern."
                 if position < 0.5 else
                 "Lowest heart rate came in the second half of the night. That "
                 "often follows a late meal, alcohol, a warm room or a shifted "
                 "body clock — this shows the timing, not which of those it was."),
    })
    return out


def recovery_velocity(epochs: Sequence[Epoch], bout_end_unix: int,
                      resting_hr: float | None) -> dict[str, Any]:
    """How far heart rate falls in the minutes after a bout ends.

    Selection is by time range rather than exact epoch keys: a bout boundary
    does not necessarily land on the epoch grid, and an exact-key lookup would
    silently find nothing when it did not.
    """
    series = sorted(((e.unix, e.hr) for e in epochs if e.hr is not None))

    def between(lo: int, hi: int) -> list[float]:
        return [hr for unix, hr in series if lo <= unix < hi]

    tail = between(bout_end_unix - 5 * EPOCH_SECONDS, bout_end_unix + EPOCH_SECONDS)
    out: dict[str, Any] = {"usable": False}
    if not tail:
        out["reason"] = "no heart rate at the end of the bout"
        return out

    peak = max(tail)
    drops: dict[str, float] = {}
    for minutes in HRR_WINDOWS:
        target = bout_end_unix + minutes * EPOCH_SECONDS
        # A minute either side, so a slightly off-grid boundary still matches.
        got = between(target - EPOCH_SECONDS, target + 2 * EPOCH_SECONDS)
        if got:
            drops[f"hrr_{minutes}min"] = round(peak - min(got), 1)

    if not drops:
        out["reason"] = "no heart rate recorded after the bout"
        return out

    out.update({"usable": True, "peak_hr": round(peak, 1), **drops})
    # Fraction of the way back to rest after 5 minutes: scale-free, so it is
    # comparable across sessions of different intensity.
    if resting_hr and "hrr_5min" in drops and peak > resting_hr:
        out["fraction_recovered_5min"] = round(
            min(1.0, drops["hrr_5min"] / (peak - resting_hr)), 3)
    # HRR60 is the conventionally reported figure.
    if "hrr_1min" in drops:
        out["hrr_60s_band"] = ("strong" if drops["hrr_1min"] >= 25
                               else "typical" if drops["hrr_1min"] >= 12
                               else "sluggish")
    return out
