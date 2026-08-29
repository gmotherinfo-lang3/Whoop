"""Training load, sleep-debt payback, and a daily strain target.

Three models, each with a different confidence level, stated openly:

* **ACWR** is arithmetic on your own load. The arithmetic is solid; the *injury
  risk* framing around it is not. The original acute:chronic work has been
  substantially criticised for mathematical coupling and spurious correlation
  (Impellizzeri and others), so this reports the ratio and what it means about
  your load trend, and does not tell you that you are about to get hurt.

* **Sleep-debt payback** is a model, not physiology. Sleep debt demonstrably
  does not clear one-for-one, so a compounding model is closer than a simple
  subtraction, but the constants are a convention. They are named and tunable.

* **The strain target** is learned from your own next-day recovery when there
  is enough history, and falls back to a plain recovery-to-strain curve when
  there is not. It never invents a number from nothing.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

# EWMA smoothing factors, the standard 2/(N+1) form.
ACUTE_DAYS, CHRONIC_DAYS = 7, 28
MIN_DAYS_FOR_ACWR = 14

# Sleep-debt model constants. Extra sleep pays debt down at less than face
# value, and old debt partially fades as the body adapts.
PAYBACK_EFFICIENCY = 0.55
DEBT_DECAY_PER_NIGHT = 0.06
MAX_USEFUL_EXTRA_MIN = 120     # beyond this, extra time in bed stops helping

MIN_DAYS_FOR_TARGET = 21


def _ewma(values: Sequence[float], days: int) -> float | None:
    """Exponentially weighted mean, most recent value last."""
    if not values:
        return None
    alpha = 2.0 / (days + 1.0)
    acc = float(values[0])
    for v in values[1:]:
        acc = alpha * float(v) + (1 - alpha) * acc
    return acc


def acwr(daily_strain: Sequence[float]) -> dict[str, Any]:
    """Acute vs chronic load. `daily_strain` is oldest first."""
    series = [float(v) for v in daily_strain if v is not None]
    out: dict[str, Any] = {"usable": False, "days": len(series)}
    if len(series) < MIN_DAYS_FOR_ACWR:
        out["reason"] = f"needs {MIN_DAYS_FOR_ACWR} days of load, have {len(series)}"
        return out

    acute = _ewma(series[-ACUTE_DAYS * 3:], ACUTE_DAYS)
    chronic = _ewma(series, CHRONIC_DAYS)
    if not chronic:
        out["reason"] = "no chronic load to compare against"
        return out

    ratio = acute / chronic
    if ratio < 0.8:
        band, note = "detraining", "Recent load is below what you are adapted to."
    elif ratio <= 1.3:
        band, note = "optimal", "Recent load is in step with what you are adapted to."
    elif ratio <= 1.5:
        band, note = "building", "Load is climbing faster than your base. Watch it."
    else:
        band, note = "spike", "Load has jumped well above your base."

    out.update({
        "usable": True,
        "acute": round(acute, 2), "chronic": round(chronic, 2),
        "ratio": round(ratio, 2), "band": band, "note": note,
        "acute_days": ACUTE_DAYS, "chronic_days": CHRONIC_DAYS,
        "caveat": ("Exponentially weighted, so recent days count for more than a "
                   "flat average gives them. The ratio describes your load trend. "
                   "The popular claim that above 1.5 predicts injury comes from "
                   "work that has been widely challenged, so it is not stated here "
                   "as a risk figure."),
    })
    return out


def sleep_debt(nightly_minutes: Sequence[float], need_minutes: float,
               decay: float = DEBT_DECAY_PER_NIGHT) -> float:
    """Accumulated debt, compounding, most recent night last."""
    debt = 0.0
    for slept in nightly_minutes:
        if slept is None:
            continue
        debt *= (1 - decay)
        shortfall = need_minutes - float(slept)
        if shortfall > 0:
            debt += shortfall
        else:
            debt = max(0.0, debt + PAYBACK_EFFICIENCY * shortfall)
    return round(debt, 1)


def payback_plan(debt_minutes: float, need_minutes: float,
                 horizons: Sequence[int] = (3, 5, 7, 10)) -> dict[str, Any]:
    """What extra per night clears the debt, over each horizon.

    Solved by simulating the same model forward, so the plan and the debt
    figure cannot drift apart.
    """
    out: dict[str, Any] = {"debt_minutes": round(debt_minutes, 1), "options": []}
    if debt_minutes <= 15:
        out["note"] = "No meaningful debt to pay back."
        return out

    for nights in horizons:
        # Bisect on the extra minutes per night that lands debt at ~zero.
        lo, hi = 0.0, MAX_USEFUL_EXTRA_MIN
        best = None
        for _ in range(40):
            mid = (lo + hi) / 2
            debt = debt_minutes
            for _ in range(nights):
                debt *= (1 - DEBT_DECAY_PER_NIGHT)
                debt = max(0.0, debt - PAYBACK_EFFICIENCY * mid)
            if debt <= 0.5:
                best, hi = mid, mid
            else:
                lo = mid
        if best is not None and best <= MAX_USEFUL_EXTRA_MIN - 1:
            # Round UP: rounding to nearest can report a number that does not
            # quite clear the debt it claims to.
            extra = int(math.ceil(best))
            out["options"].append({
                "nights": nights,
                "extra_minutes_per_night": extra,
                "target_minutes_per_night": int(round(need_minutes)) + extra,
            })

    if not out["options"]:
        out["note"] = (f"This much debt cannot be cleared inside {max(horizons)} "
                       "nights at a sensible extra amount. It will take longer.")
    else:
        first = out["options"][0]
        out["note"] = (f"About {first['extra_minutes_per_night']} extra minutes a "
                       f"night for {first['nights']} nights clears it.")
    out["model"] = {"payback_efficiency": PAYBACK_EFFICIENCY,
                    "nightly_decay": DEBT_DECAY_PER_NIGHT,
                    "note": "A model, not a measurement. Constants are conventions."}
    return out


def _fallback_target(recovery: float) -> float:
    """Plain recovery-to-strain curve, used until there is history to learn from."""
    r = max(0.0, min(100.0, recovery)) / 100.0
    return round(4.0 + 14.0 * (r ** 1.2), 1)


def strain_target(recovery_today: float | None,
                  history: Sequence[dict[str, Any]],
                  recovery_floor: float = 55.0) -> dict[str, Any]:
    """Today's strain target: how hard you can go without tanking tomorrow.

    `history` is a list of {recovery, strain, next_recovery}, oldest first.
    Fits next-day recovery on today's recovery and today's strain, then solves
    for the strain that keeps tomorrow at or above `recovery_floor`.
    """
    out: dict[str, Any] = {"usable": False, "source": "none"}
    if recovery_today is None:
        out["reason"] = "no recovery score for today"
        return out

    rows = [h for h in history
            if all(isinstance(h.get(k), (int, float))
                   for k in ("recovery", "strain", "next_recovery"))]

    fallback = _fallback_target(recovery_today)
    if len(rows) < MIN_DAYS_FOR_TARGET:
        out.update({
            "usable": True, "source": "curve", "target": fallback,
            "range": [round(max(0.0, fallback - 2), 1), round(fallback + 2, 1)],
            "days_used": len(rows),
            "note": (f"From a standard recovery-to-strain curve. After "
                     f"{MIN_DAYS_FOR_TARGET} days this is learned from how your own "
                     f"next-day recovery actually responds; have {len(rows)}."),
        })
        return out

    x = np.array([[r["recovery"], r["strain"]] for r in rows], dtype=float)
    y = np.array([r["next_recovery"] for r in rows], dtype=float)
    # Ridge on standardised inputs: n is small and the two predictors correlate.
    mean, std = x.mean(axis=0), np.where(x.std(axis=0) < 1e-9, 1.0, x.std(axis=0))
    xs = np.hstack([(x - mean) / std, np.ones((len(x), 1))])
    ridge = np.eye(3) * 1.0
    ridge[-1, -1] = 0.0
    beta = np.linalg.solve(xs.T @ xs + ridge, xs.T @ y)

    pred = xs @ beta
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    strain_coeff = beta[1] / std[1]
    # A model that says strain does not cost you anything is not one to plan on.
    if r2 < 0.15 or strain_coeff >= -1e-6:
        out.update({
            "usable": True, "source": "curve", "target": fallback,
            "range": [round(max(0.0, fallback - 2), 1), round(fallback + 2, 1)],
            "days_used": len(rows), "r2": round(r2, 3),
            "note": ("Your own data does not yet show strain reliably affecting "
                     "next-day recovery, so this is the standard curve rather than "
                     "a fitted number."),
        })
        return out

    base = beta[2] + beta[0] * (recovery_today - mean[0]) / std[0]
    target = (recovery_floor - base) * std[1] / beta[1] + mean[1]
    target = float(max(0.0, min(21.0, target)))

    out.update({
        "usable": True, "source": "learned", "target": round(target, 1),
        "range": [round(max(0.0, target - 1.5), 1), round(min(21.0, target + 1.5), 1)],
        "days_used": len(rows), "r2": round(r2, 3),
        "recovery_floor": recovery_floor,
        "note": (f"Fitted on {len(rows)} days of your own next-day recovery "
                 f"(R²={r2:.2f}). The target is the strain that, on your history, "
                 f"still leaves tomorrow at {recovery_floor:.0f}% or better."),
    })
    return out
