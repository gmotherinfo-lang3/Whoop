"""Estimated cardiorespiratory fitness, and how it compares to age norms.

What this is: an estimate of VO2 max from the ratio of maximum to resting
heart rate, and the age at which the population median VO2 max equals yours.
That is the same construction Garmin and others call "fitness age".

What this is NOT, and will not pretend to be:

* **Biological or epigenetic age.** That needs methylation data. Nothing
  derived from heart rate can speak to it.
* **A "pace of aging" multiplier.** The published pace-of-aging measures come
  from longitudinal biomarker panels, not wearables. What can honestly be
  shown instead is whether *your own* fitness age is trending up or down,
  which this does.

Two limits on the estimate itself, both worth knowing before reading the
number:

1. The heart-rate ratio method (Uth et al. 2004, Eur J Appl Physiol) was
   validated in *well-trained men*, r = 0.92 against measured VO2 max. It is
   a reasonable estimate, not a lab test, and it is weakest for people
   unlike that validation group.
2. It depends on true maximum heart rate. Unless you have measured yours, the
   configured value is the 220-age rule, which carries roughly +/- 10-12 bpm
   of individual scatter. That error passes straight through.

HRV is deliberately NOT scored against population norms here. The published
norms are five-minute resting supine ECG; this measures overnight wrist
optical, which reads systematically differently. Comparing them would look
rigorous and be wrong. HRV is shown as your own trend instead.
"""

from __future__ import annotations

from typing import Any

# Population median VO2 max, ml/kg/min, by age band. Approximate ACSM / Cooper
# Institute reference values -- population medians, not targets.
VO2MAX_MEDIAN = {
    "male":   {25: 42.5, 35: 41.0, 45: 38.1, 55: 35.2, 65: 31.4, 75: 27.2},
    "female": {25: 36.7, 35: 34.6, 45: 32.3, 55: 29.4, 65: 27.2, 75: 24.4},
}

# Uth et al. proportionality factor, ml/kg/min.
HR_RATIO_FACTOR = 15.3

MIN_AGE, MAX_AGE = 20, 80

MIN_TREND_DAYS = 8
# Slopes smaller than these are noise, not a direction.
TREND_DEADBAND = 0.05   # years of fitness age per month
VO2_DEADBAND = 0.1      # ml/kg/min per month


def estimate_vo2max(max_hr: float, resting_hr: float) -> dict[str, Any]:
    """Heart-rate ratio method. Returns the estimate and its caveats."""
    out: dict[str, Any] = {"usable": False}
    if not max_hr or not resting_hr or resting_hr <= 0 or max_hr <= resting_hr:
        out["reason"] = "needs a resting and a maximum heart rate"
        return out
    ratio = max_hr / resting_hr
    # Ratios outside this are almost always a bad resting-HR estimate rather
    # than an extraordinary athlete.
    if not 2.0 <= ratio <= 5.5:
        out["reason"] = (f"heart-rate ratio of {ratio:.1f} is outside the range this "
                         "method covers — check your resting and maximum heart rate")
        return out
    out.update({
        "usable": True,
        "vo2max": round(HR_RATIO_FACTOR * ratio, 1),
        "ratio": round(ratio, 2),
        "max_hr": round(max_hr), "resting_hr": round(resting_hr, 1),
        "method": "heart-rate ratio (Uth et al. 2004)",
    })
    return out


def _median_curve(sex: str) -> list[tuple[int, float]]:
    table = VO2MAX_MEDIAN.get(sex, VO2MAX_MEDIAN["male"])
    return sorted(table.items())


def fitness_age(vo2max: float, sex: str = "male",
                chronological_age: float | None = None) -> dict[str, Any]:
    """The age whose population median VO2 max matches this one."""
    curve = _median_curve(sex)
    ages = [a for a, _ in curve]
    values = [v for _, v in curve]

    if vo2max >= values[0]:
        age = float(MIN_AGE)
        edge = "at or above the median for the youngest band"
    elif vo2max <= values[-1]:
        age = float(MAX_AGE)
        edge = "at or below the median for the oldest band"
    else:
        age, edge = float(MAX_AGE), None
        for i in range(len(curve) - 1):
            hi_v, lo_v = values[i], values[i + 1]
            if lo_v <= vo2max <= hi_v:
                span = hi_v - lo_v
                frac = 0.0 if span == 0 else (hi_v - vo2max) / span
                age = ages[i] + frac * (ages[i + 1] - ages[i])
                break

    out: dict[str, Any] = {
        "usable": True,
        "fitness_age": round(age, 1),
        "vo2max": round(vo2max, 1),
        "sex_reference": sex,
        "edge": edge,
    }
    if chronological_age:
        delta = chronological_age - age
        out["chronological_age"] = chronological_age
        out["delta_years"] = round(delta, 1)
        out["verdict"] = ("younger" if delta > 0.5 else
                          "older" if delta < -0.5 else "in line")
    return out


def _slope_per_month(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope in units per 30 days, or None if time has no spread."""
    xs = [-p[0] for p in points]          # days_ago -> increasing time
    ys = [p[1] for p in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom * 30


def _points(history: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [h for h in history if isinstance(h.get(key), (int, float))]


def trend(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether your own fitness age is moving, from your own history.

    This replaces the "pace of aging" framing: a within-person trend is
    something this data can actually support.
    """
    usable = _points(history, "fitness_age")
    if len(usable) < MIN_TREND_DAYS:
        return {"usable": False,
                "reason": f"needs {MIN_TREND_DAYS} days of history, have {len(usable)}"}

    # A fitness age pinned to the top or bottom of the reference table cannot
    # move, so a slope through those points would read "steady" forever no
    # matter what the underlying VO2 max did. Say so instead.
    if sum(1 for h in usable if h.get("edge")) > len(usable) / 2:
        return {"usable": False, "clamped": True,
                "reason": ("Your estimate sits at the edge of the reference table, "
                           "where fitness age cannot move. Your VO\u2082 max is "
                           "what to watch instead.")}

    per_month = _slope_per_month([(h["days_ago"], h["fitness_age"]) for h in usable])
    if per_month is None:
        return {"usable": False, "reason": "not enough spread in time"}

    # Falling fitness age is the good direction, so the sign is flipped here
    # relative to the VO2 max trend below.
    direction = ("improving" if per_month < -TREND_DEADBAND
                 else "declining" if per_month > TREND_DEADBAND else "steady")
    moving = ("falling" if direction == "improving" else
              "rising" if direction == "declining" else "holding steady")
    return {
        "usable": True,
        "days": len(usable),
        "years_per_month": round(per_month, 3),
        "direction": direction,
        "note": (f"Your estimated fitness age is {moving} over the period measured. "
                 "This is your own trend, not a biological rate of ageing."),
    }


def vo2max_trend(history: list[dict[str, Any]]) -> dict[str, Any]:
    """The same within-person slope on VO2 max itself.

    VO2 max keeps moving where fitness age is clamped at the end of the
    reference table, so this is what the very fit and the very unfit should be
    watching. Note the sign convention is the opposite of `trend`: here, up is
    the good direction.
    """
    usable = _points(history, "vo2max")
    if len(usable) < MIN_TREND_DAYS:
        return {"usable": False,
                "reason": f"needs {MIN_TREND_DAYS} days of history, have {len(usable)}"}

    per_month = _slope_per_month([(h["days_ago"], h["vo2max"]) for h in usable])
    if per_month is None:
        return {"usable": False, "reason": "not enough spread in time"}

    direction = ("improving" if per_month > VO2_DEADBAND
                 else "declining" if per_month < -VO2_DEADBAND else "steady")
    return {
        "usable": True,
        "days": len(usable),
        "per_month": round(per_month, 2),
        "unit": "ml/kg/min per month",
        "direction": direction,
        "note": ("Your estimated VO\u2082 max is "
                 + ("rising" if direction == "improving" else
                    "falling" if direction == "declining" else "holding steady")
                 + " over the period measured."),
    }
