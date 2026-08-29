"""How far along the learning is, and roughly how long until it switches on.

Both learned features stay dormant until they have enough data to be worth
trusting. That is the right behaviour, but silent dormancy is confusing, so
this module reports what is still missing and estimates when it will be met
based on the rate data has actually been arriving.

Estimates are extrapolations from your own recent behaviour, not promises. If
you label more activities or journal more days, they arrive sooner.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .insights import MIN_GROUP, MIN_PAIRS
from .ml import MIN_CLASSES, MIN_PER_CLASS

MODEL_MIN_ACCURACY = 0.60


def _eta(remaining: float, per_day: float) -> dict[str, Any]:
    """Turn a shortfall and a rate into a human estimate."""
    if remaining <= 0:
        return {"days": 0, "text": "ready now"}
    if per_day <= 0:
        return {"days": None, "text": "cannot estimate yet - no activity recorded so far"}
    days = int(remaining / per_day + 0.999)
    when = date.today() + timedelta(days=days)
    if days <= 1:
        text = "about a day"
    elif days < 14:
        text = f"about {days} days"
    elif days < 60:
        text = f"about {round(days / 7)} weeks"
    else:
        text = f"about {round(days / 30)} months"
    return {"days": days, "text": text, "estimated_date": when.isoformat()}


def activity_learning_status(labelled: list[dict[str, Any]],
                             detected_recent: int, window_days: int,
                             model_accuracy: float | None) -> dict[str, Any]:
    """Progress toward the activity classifier taking over from the rules."""
    counts: dict[str, int] = {}
    for row in labelled:
        label = row.get("confirmed_type")
        if label:
            counts[label] = counts.get(label, 0) + 1

    ready_classes = [c for c, n in counts.items() if n >= MIN_PER_CLASS]
    short = {c: MIN_PER_CLASS - n for c, n in counts.items() if n < MIN_PER_CLASS}

    # Labels still needed: fill partly-complete classes, then add new ones.
    needed = sum(short.values())
    if len(ready_classes) + len(short) < MIN_CLASSES:
        needed += (MIN_CLASSES - len(ready_classes) - len(short)) * MIN_PER_CLASS

    have_enough = len(ready_classes) >= MIN_CLASSES
    accurate_enough = (model_accuracy or 0) >= MODEL_MIN_ACCURACY
    active = have_enough and accurate_enough

    # You can only label what gets detected, so detection rate bounds the pace.
    per_day = detected_recent / window_days if window_days else 0.0

    if active:
        blocker = None
    elif not have_enough:
        blocker = (f"needs {MIN_CLASSES} activity types with {MIN_PER_CLASS} "
                   f"confirmed examples each")
    else:
        blocker = (f"has enough examples, but cross-validated accuracy is "
                   f"{model_accuracy:.0%}; needs {MODEL_MIN_ACCURACY:.0%}. "
                   f"Confirming more examples usually fixes this")

    return {
        "active": active,
        "using": "model" if active else "rules",
        "labelled_total": sum(counts.values()),
        "counts": counts,
        "ready_classes": sorted(ready_classes),
        "labels_needed": max(0, needed),
        "model_accuracy": model_accuracy,
        "min_accuracy": MODEL_MIN_ACCURACY,
        "blocker": blocker,
        "eta": _eta(max(0, needed), per_day) if not active else {"days": 0, "text": "active"},
        "detected_per_day": round(per_day, 2),
        "note": ("Until this is active, activities are labelled by fixed rules and "
                 "shown with low confidence. Confirming or correcting them is what "
                 "trains the model."),
    }


def insight_learning_status(pairs: int, tag_day_counts: dict[str, int],
                            journalled_days: int, span_days: int) -> dict[str, Any]:
    """Progress toward the lifestyle-driver analysis producing findings."""
    per_day = journalled_days / span_days if span_days else 0.0
    remaining_pairs = max(0, MIN_PAIRS - pairs)

    # A tag also needs enough days on each side of the comparison.
    testable, nearly = [], {}
    for tag, n_with in tag_day_counts.items():
        n_without = journalled_days - n_with
        if n_with >= MIN_GROUP and n_without >= MIN_GROUP:
            testable.append(tag)
        else:
            nearly[tag] = {
                "days_with": n_with, "days_without": n_without,
                "needs": (f"{MIN_GROUP - n_with} more days with it"
                          if n_with < MIN_GROUP
                          else f"{MIN_GROUP - n_without} more days without it"),
            }

    active = pairs >= MIN_PAIRS and bool(testable)
    return {
        "active": active,
        "journalled_days": journalled_days,
        "usable_pairs": pairs,
        "pairs_needed": remaining_pairs,
        "testable_factors": sorted(testable),
        "factors_not_yet_testable": nearly,
        "blocker": None if active else (
            f"needs {MIN_PAIRS} journalled days (have {pairs})" if remaining_pairs
            else f"needs one factor logged on at least {MIN_GROUP} days and "
                 f"absent on {MIN_GROUP} others"),
        "eta": _eta(remaining_pairs, per_day) if not active
               else {"days": 0, "text": "active"},
        "note": ("Journal most days, including ordinary ones. A factor can only be "
                 "tested if there are days both with and without it, so logging only "
                 "the notable days makes the comparison impossible."),
    }
