"""Health-monitor metrics with a personal normal range.

Every channel is shown against a range built from your own recent history
rather than a population table, which is the only comparison that is valid for
all of them: three of the five are raw sensor counts with no real-world unit,
so an absolute reference does not exist.

The range is the 10th to 90th percentile of your own baseline window. A value
inside it is unremarkable for you; outside it is worth noticing. That is a
statement about your own variation, not a clinical reference interval.
"""

from __future__ import annotations

import statistics
from typing import Any, Sequence

MIN_BASELINE_DAYS = 7
LOW_PCT, HIGH_PCT = 0.10, 0.90


def _percentile(values: Sequence[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


def channel(label: str, unit: str, value: float | None,
            history: Sequence[float], *, raw: bool = False,
            decimals: int = 0) -> dict[str, Any]:
    """One metric with its personal range and whether today sits inside it."""
    usable = [float(v) for v in history if isinstance(v, (int, float))]
    out: dict[str, Any] = {
        "label": label, "unit": unit, "raw": raw,
        "value": round(value, decimals) if isinstance(value, (int, float)) else None,
        "in_range": None, "low": None, "high": None,
        "baseline_days": len(usable),
    }
    if out["value"] is None:
        out["state"] = "no reading"
        return out
    if len(usable) < MIN_BASELINE_DAYS:
        out["state"] = f"building baseline ({len(usable)}/{MIN_BASELINE_DAYS} days)"
        return out

    low, high = _percentile(usable, LOW_PCT), _percentile(usable, HIGH_PCT)
    out["low"], out["high"] = round(low, decimals), round(high, decimals)
    inside = low <= out["value"] <= high
    out["in_range"] = inside
    out["state"] = "within your range" if inside else (
        "above your usual range" if out["value"] > high else "below your usual range")

    median = statistics.median(usable)
    if median:
        out["delta_pct"] = round((out["value"] - median) / median * 100, 1)
    out["baseline_median"] = round(median, decimals)
    return out


def summarise(channels: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Headline for the card on the main feed."""
    scored = [c for c in channels if c.get("in_range") is not None]
    if not scored:
        return {"ready": False, "headline": "Building baseline",
                "detail": "A week of history is needed before ranges mean anything."}
    inside = sum(1 for c in scored if c["in_range"])
    out_of = [c["label"] for c in scored if not c["in_range"]]
    return {
        "ready": True,
        "in_range": inside, "total": len(scored),
        "headline": ("All within range" if not out_of
                     else f"{len(out_of)} outside range"),
        "detail": ", ".join(out_of) if out_of else "Nothing unusual for you today.",
        "ok": not out_of,
    }
