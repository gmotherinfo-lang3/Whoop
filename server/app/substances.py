"""Caffeine and alcohol against the night that followed.

One correction to the usual framing: caffeine and alcohol do not clear the
same way, so modelling both as a "half-life" is wrong for one of them.

* **Caffeine** is first-order. A roughly 5-hour half-life means the amount
  remaining halves every 5 hours, so an evening dose still has a quarter of it
  on board at bedtime plus five hours.
* **Alcohol** at ordinary doses is *zero-order*: the liver clears it at a
  near-constant rate, about one standard drink an hour, regardless of how much
  is on board. Applying an exponential half-life to it would badly
  underestimate how long a heavy night lingers.

What this can and cannot correlate against: it uses overnight HRV, resting
heart rate and sleep duration, all of which this system measures. It does
**not** use slow-wave sleep percentage. Sleep staging needs signals this strap
does not expose to us, so any "deep sleep %" here would be invented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

CAFFEINE_HALF_LIFE_H = 5.0
# Standard drinks per hour, the usual zero-order clearance figure.
ALCOHOL_CLEARANCE_PER_H = 1.0

# Rough guides so a log entry can be a familiar thing rather than a number.
CAFFEINE_MG = {
    "espresso": 63, "coffee": 95, "filter_coffee": 95, "instant_coffee": 62,
    "black_tea": 47, "green_tea": 28, "energy_drink": 80, "cola": 34,
    "pre_workout": 200, "matcha": 70,
}
ALCOHOL_UNITS = {
    "beer": 1.0, "pint": 1.7, "wine": 1.5, "large_wine": 2.3,
    "spirit": 1.0, "double_spirit": 2.0, "cocktail": 1.7,
}


def caffeine_remaining(dose_mg: float, hours_since: float) -> float:
    """First-order decay."""
    if hours_since < 0:
        return 0.0
    return dose_mg * (0.5 ** (hours_since / CAFFEINE_HALF_LIFE_H))


def alcohol_remaining(units: float, hours_since: float) -> float:
    """Zero-order clearance: a flat amount per hour, floored at zero."""
    if hours_since < 0:
        return 0.0
    return max(0.0, units - ALCOHOL_CLEARANCE_PER_H * hours_since)


def _hours(a: str, b: str) -> float | None:
    try:
        t1 = datetime.fromisoformat(a)
        t2 = datetime.fromisoformat(b)
    except ValueError:
        return None
    for t in (t1, t2):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=timezone.utc)
    if t2.tzinfo is None:
        t2 = t2.replace(tzinfo=timezone.utc)
    return (t2 - t1).total_seconds() / 3600.0


def onboard_at(intakes: Sequence[dict[str, Any]], when_iso: str) -> dict[str, Any]:
    """How much of each substance is still circulating at a given moment."""
    caffeine = 0.0
    alcohol = 0.0
    contributions = []
    for entry in intakes:
        gap = _hours(entry.get("at", ""), when_iso)
        if gap is None or gap < 0:
            continue
        kind = entry.get("substance")
        amount = float(entry.get("amount") or 0)
        if kind == "caffeine":
            left = caffeine_remaining(amount, gap)
            caffeine += left
        elif kind == "alcohol":
            left = alcohol_remaining(amount, gap)
            alcohol += left
        else:
            continue
        if left > 0.01:
            contributions.append({
                "at": entry.get("at"), "substance": kind, "amount": amount,
                "hours_before": round(gap, 2), "remaining": round(left, 2),
                "label": entry.get("label", ""),
            })
    return {
        "caffeine_mg": round(caffeine, 1),
        "alcohol_units": round(alcohol, 2),
        "contributions": contributions,
    }


def curve(intakes: Sequence[dict[str, Any]], start_iso: str, hours: int = 24,
          step_minutes: int = 30) -> list[dict[str, Any]]:
    """The on-board amount over time, for plotting against the night."""
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        return []
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    points = []
    steps = int(hours * 60 / step_minutes) + 1
    for i in range(steps):
        moment = start.timestamp() + i * step_minutes * 60
        iso = datetime.fromtimestamp(moment, timezone.utc).isoformat()
        state = onboard_at(intakes, iso)
        points.append({"at": iso,
                       "caffeine_mg": state["caffeine_mg"],
                       "alcohol_units": state["alcohol_units"]})
    return points


def overlay(intakes: Sequence[dict[str, Any]], sleep_start_iso: str,
            night: dict[str, Any]) -> dict[str, Any]:
    """What was on board at sleep onset, beside how that night went."""
    at_onset = onboard_at(intakes, sleep_start_iso)
    hrv = (night or {}).get("hrv", {}).get("rmssd_ms")
    rhr = (night or {}).get("heart_rate", {}).get("resting")
    minutes = (night or {}).get("sleep", {}).get("total_minutes")

    flags = []
    if at_onset["caffeine_mg"] >= 50:
        flags.append(f"{at_onset['caffeine_mg']:.0f} mg of caffeine still on board "
                     "at lights out")
    if at_onset["alcohol_units"] >= 0.5:
        flags.append(f"{at_onset['alcohol_units']:.1f} units of alcohol still "
                     "clearing at lights out")

    return {
        "at_sleep_onset": at_onset,
        "night": {"hrv_rmssd_ms": hrv, "resting_hr": rhr, "sleep_minutes": minutes},
        "flags": flags,
        "measured": ["overnight HRV", "resting heart rate", "sleep duration"],
        "not_measured": ["slow-wave sleep percentage — this strap does not give "
                         "us sleep staging, so it is not estimated here"],
        "model": {"caffeine": f"first-order, {CAFFEINE_HALF_LIFE_H}h half-life",
                  "alcohol": f"zero-order, ~{ALCOHOL_CLEARANCE_PER_H} unit/hour"},
    }
