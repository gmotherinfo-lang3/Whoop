"""Plain-language suggestions, produced only once the baseline can support them.

Every suggestion carries the numbers that produced it, so you can disagree with
it. Nothing fires until there is enough baseline history to know what "normal"
looks like for you, because a deviation is meaningless without a distribution
to deviate from.

The illness signal deserves a specific warning. It is the same physiological
pattern consumer wearables use -- resting heart rate up, HRV down, skin
temperature and respiration up, all relative to your own baseline. It detects
*physiological strain*, which illness causes, but so do alcohol, poor sleep, a
hard workout, heat, dehydration and stress. It is not a diagnosis, it cannot
identify what is wrong, and its absence is not evidence that you are well.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict
from typing import Any, Sequence

MIN_BASELINE_DAYS = 14      # before any deviation-based suggestion fires
MIN_READINESS_DAYS = 7      # before recovery-based coaching fires
Z_FLAG = 1.5                # standard deviations that counts as a real deviation
MIN_MARKERS = 2             # concordant markers needed for an illness signal


@dataclass
class Suggestion:
    id: str
    severity: str            # "info", "notice", "warning"
    headline: str
    detail: str
    evidence: dict[str, Any]
    confidence: str          # "low", "moderate", "high"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _zscore(value: float | None, history: Sequence[float]) -> float | None:
    """Deviation from personal baseline, in standard deviations."""
    values = [v for v in history if v is not None]
    if value is None or len(values) < MIN_BASELINE_DAYS:
        return None
    sd = statistics.pstdev(values)
    if sd < 1e-9:
        return None
    return round((value - statistics.mean(values)) / sd, 2)


def _series(days: Sequence[dict[str, Any]], *path: str) -> list[float]:
    out = []
    for d in days:
        node: Any = d
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if isinstance(node, (int, float)):
            out.append(float(node))
    return out


def _get(day: dict[str, Any], *path: str) -> float | None:
    node: Any = day
    for key in path:
        node = (node or {}).get(key) if isinstance(node, dict) else None
    return float(node) if isinstance(node, (int, float)) else None


def suggest(today: dict[str, Any], history: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Build today's suggestions from today's numbers and prior days.

    `history` must exclude today, and be ordered most-recent-first.
    """
    past = [d for d in history if d.get("has_data")]
    if not today.get("has_data"):
        return {"ready": False, "reason": "no data for today", "suggestions": []}
    if len(past) < MIN_READINESS_DAYS:
        return {
            "ready": False,
            "reason": (f"needs {MIN_READINESS_DAYS} prior days of data to know your "
                       f"normal; have {len(past)}"),
            "baseline_days": len(past),
            "suggestions": [],
        }

    out: list[Suggestion] = []
    out.extend(_illness(today, past))
    out.extend(_readiness(today, past))
    out.extend(_sleep(today, past))

    order = {"warning": 0, "notice": 1, "info": 2}
    out.sort(key=lambda s: order.get(s.severity, 3))
    return {
        "ready": True,
        "baseline_days": len(past),
        "suggestions": [s.as_dict() for s in out],
        "disclaimer": ("Not medical advice and not a diagnosis. These are patterns "
                       "in your own data, computed from unofficial approximations. "
                       "If you feel unwell, that matters more than any number here."),
    }


def _illness(today: dict[str, Any], past: Sequence[dict[str, Any]]) -> list[Suggestion]:
    """Concordant deviation across independent markers of physiological strain."""
    markers: dict[str, dict[str, Any]] = {}

    checks = (
        ("resting_hr", ("heart_rate", "resting"), 1),    # +1 = higher is a flag
        ("hrv", ("hrv", "rmssd_ms"), -1),                # -1 = lower is a flag
        ("skin_temp", ("sensors", "skin_temp_raw"), 1),
        ("respiration", ("sensors", "resp_rate_raw"), 1),
    )
    for name, path, direction in checks:
        z = _zscore(_get(today, *path), _series(past, *path))
        if z is None:
            continue
        flagged = (z >= Z_FLAG) if direction > 0 else (z <= -Z_FLAG)
        markers[name] = {"z": z, "flagged": flagged, "value": _get(today, *path)}

    flagged = [n for n, m in markers.items() if m["flagged"]]
    if len(flagged) < MIN_MARKERS:
        return []

    strong = len(flagged) >= 3
    return [Suggestion(
        id="possible_illness",
        severity="warning" if strong else "notice",
        headline=("Several markers are off at once — this is what the run-up to "
                  "illness often looks like"
                  if strong else
                  "A couple of markers are off — worth keeping an eye on"),
        detail=(f"{len(flagged)} of {len(markers)} markers deviate from your baseline "
                f"by more than {Z_FLAG} standard deviations: {', '.join(flagged)}. "
                "Illness produces this pattern, but so do alcohol, a hard session, "
                "heat, dehydration, poor sleep and stress. This cannot tell them "
                "apart. Treat it as a prompt to check in with how you feel, "
                "not as a diagnosis."),
        evidence={"markers": markers, "flagged": flagged,
                  "threshold_sd": Z_FLAG, "baseline_days": len(past)},
        confidence="moderate" if strong else "low",
    )]


def _readiness(today: dict[str, Any], past: Sequence[dict[str, Any]]) -> list[Suggestion]:
    """Train-hard / take-it-easy guidance from recovery and recent load."""
    score = _get(today, "recovery", "score")
    if score is None:
        return []

    recent_strain = _series(past[:7], "strain", "score")
    avg_strain = round(statistics.mean(recent_strain), 1) if recent_strain else None
    evidence = {"recovery": score, "avg_strain_7d": avg_strain,
                "hrv": _get(today, "hrv", "rmssd_ms"),
                "resting_hr": _get(today, "heart_rate", "resting"),
                "hrv_baseline": round(statistics.mean(_series(past, "hrv", "rmssd_ms")), 1)
                if _series(past, "hrv", "rmssd_ms") else None}

    if score >= 75:
        return [Suggestion(
            id="ready_to_push", severity="info",
            headline="Well recovered — a good day to push",
            detail=(f"Recovery is {score:.0f}%. HRV and resting heart rate are both "
                    "sitting favourably against your baseline, which is the pattern "
                    "that usually precedes a session going well."),
            evidence=evidence, confidence="moderate")]

    if score < 34:
        return [Suggestion(
            id="take_it_easy", severity="notice",
            headline="Barely recovered — keep today light",
            detail=(f"Recovery is {score:.0f}%. Your body is still carrying load from "
                    "something. A light day now generally costs less than pushing "
                    "through and needing three."
                    + (f" Your 7-day average strain is {avg_strain}."
                       if avg_strain else "")),
            evidence=evidence, confidence="moderate")]

    if avg_strain is not None and avg_strain >= 15 and score < 55:
        return [Suggestion(
            id="possible_overreaching", severity="notice",
            headline="Sustained hard training with recovery not keeping up",
            detail=(f"Your 7-day average strain is {avg_strain} while recovery sits at "
                    f"{score:.0f}%. That gap, held for a while, is the usual shape of "
                    "digging a hole. Consider a lighter block."),
            evidence=evidence, confidence="low")]

    return [Suggestion(
        id="moderate_day", severity="info",
        headline="Middling recovery — train, but leave something in reserve",
        detail=f"Recovery is {score:.0f}%. Nothing alarming, nothing to push into either.",
        evidence=evidence, confidence="low")]


def _sleep(today: dict[str, Any], past: Sequence[dict[str, Any]]) -> list[Suggestion]:
    """Accumulating sleep shortfall against your own recent norm."""
    recent = _series(past[:7], "sleep", "total_minutes")
    tonight = _get(today, "sleep", "total_minutes")
    if tonight is None or len(recent) < 5:
        return []

    need = (_get(today, "sleep", "need_hours") or 8.0) * 60
    debt = sum(max(0.0, need - m) for m in ([tonight] + recent[:6]))
    if debt < 180:                       # under three hours across a week: normal noise
        return []
    return [Suggestion(
        id="sleep_debt", severity="notice",
        headline=f"About {debt / 60:.1f} hours of sleep debt this week",
        detail=("Measured against your configured need of "
                f"{need / 60:.0f}h a night. Sleep debt is the single factor most "
                "reliably associated with lower recovery in most people's data — "
                "check your own Insights tab to see whether that holds for you."),
        evidence={"debt_minutes": round(debt), "last_night_minutes": tonight,
                  "need_minutes": need, "recent_nights": recent[:6]},
        confidence="moderate")]
