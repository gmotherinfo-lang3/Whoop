"""Heart rate against physical output, with the honest caveat about "output".

The idea is sound: at the same external workload, a falling heart rate means
cardiovascular adaptation and a rising one means you are under-recovered or
getting ill. It needs a measure of external workload.

**This strap does not provide one.** No GPS, no power meter, no cadence — the
only movement signal is wrist acceleration. So "output" here is wrist movement
intensity, which is a genuine proxy for *locomotion* and close to useless
elsewhere:

  running, walking, hiking   proxy tracks effort reasonably
  rowing, elliptical         partial - the arms move with the effort
  cycling                    poor - the wrist is nearly still regardless
  lifting, yoga, swimming    not meaningful

So the index is computed per activity type and only reported for types where
wrist movement plausibly tracks effort. Pair the strap with a phone GPS or a
power meter and this becomes a real efficiency measure; until then it is a
proxy and is labelled as one.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

# Types where wrist movement plausibly scales with external effort.
LOCOMOTION_TYPES = {"run", "walk", "hike", "cardio", "hiit", "sport"}
# Labels the rule classifier produces before you confirm what a bout was. They
# are not excluded because the proxy fails, but because we do not yet know
# which activity it was -- a distinction worth telling the user about.
UNCONFIRMED_LABELS = {"other", "workout", "hard_workout", "sedentary", "activity"}
MIN_BOUTS = 6
MIN_MOTION = 0.12          # below this there is not enough movement to compare
MIN_DURATION_MIN = 8


def _usable(bout: dict[str, Any]) -> bool:
    f = bout.get("features") or {}
    return (f.get("motion_mean", 0) >= MIN_MOTION
            and f.get("duration_min", 0) >= MIN_DURATION_MIN
            and (f.get("hr_mean") or 0) > 0)


def efficiency_index(bouts: Sequence[dict[str, Any]],
                     resting_hr: float | None = None) -> dict[str, Any]:
    """Trend in heart rate at comparable wrist-movement intensity, per type.

    `bouts` need `start_unix`, `features`, and a type (confirmed or detected),
    oldest first.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    skipped: dict[str, str] = {}
    for b in bouts:
        kind = b.get("confirmed_type") or b.get("detected_type") or "other"
        if kind not in LOCOMOTION_TYPES:
            skipped.setdefault(kind, (
                "confirm what this activity was to include it"
                if kind in UNCONFIRMED_LABELS else
                "wrist movement does not track effort for this activity"))
            continue
        if not _usable(b):
            continue
        groups.setdefault(kind, []).append(b)

    results = []
    for kind, rows in sorted(groups.items()):
        if len(rows) < MIN_BOUTS:
            skipped[kind] = f"needs {MIN_BOUTS} comparable bouts, have {len(rows)}"
            continue
        rows = sorted(rows, key=lambda r: r["start_unix"])
        t = np.array([r["start_unix"] for r in rows], dtype=float)
        hr = np.array([r["features"]["hr_mean"] for r in rows], dtype=float)
        motion = np.array([r["features"]["motion_mean"] for r in rows], dtype=float)

        # Regress HR on time AND movement, so the trend is not just "recent
        # sessions happened to be harder".
        days = (t - t.min()) / 86400.0
        design = np.column_stack([days, motion, np.ones(len(rows))])
        try:
            beta, *_ = np.linalg.lstsq(design, hr, rcond=None)
        except np.linalg.LinAlgError:
            skipped[kind] = "could not fit"
            continue

        per_day = float(beta[0])
        span = float(days.max() - days.min()) or 1.0
        change = per_day * span
        reference_motion = float(np.median(motion))
        index = float(beta[1] * reference_motion + beta[2] + per_day * days.max())

        results.append({
            "activity": kind,
            "bouts": len(rows),
            "span_days": round(span, 1),
            "hr_at_reference_motion": round(index, 1),
            "reference_motion": round(reference_motion, 4),
            "hr_change_per_week": round(per_day * 7, 2),
            "hr_change_over_span": round(change, 1),
            "direction": ("improving" if change <= -1.5
                          else "declining" if change >= 1.5
                          else "steady"),
            "note": ("Heart rate at the same wrist movement is "
                     + ("falling — the usual sign of aerobic adaptation."
                        if change <= -1.5 else
                        "rising — often under-recovery, heat, or something coming on."
                        if change >= 1.5 else
                        "holding steady.")),
        })

    return {
        "usable": bool(results),
        "activities": results,
        "skipped": skipped,
        "proxy": True,
        "caveat": ("'Output' here is wrist movement intensity, not pace or power - "
                   "this strap has no GPS or power meter. It is a fair proxy for "
                   "running and walking, weak for rowing, and not meaningful for "
                   "cycling, lifting or swimming, so only locomotion types are "
                   "reported."),
    }
