"""Which lifestyle factors track with better or worse days.

This is the part of a wellness app that is easiest to get wrong and most
tempting to overstate, so the statistics are deliberately conservative:

  * **Temporal ordering.** A factor on day D is tested against the outcome on
    day D+1 by default. "Drinking is associated with worse recovery tomorrow"
    is a defensible claim in a way that a same-day correlation is not.
  * **Permutation tests**, not t-tests. With 30-90 days and skewed
    distributions, the normality assumption behind a t-test does not hold.
    Shuffling the labels makes no distributional assumption at all.
  * **Bootstrap confidence intervals**, so the size of an effect is reported
    with its uncertainty rather than as a bare point estimate.
  * **Multiple-comparison control.** Testing 15 factors at p<0.05 yields about
    one false positive every run by construction. Benjamini-Hochberg FDR is
    applied across every factor tested in a request.
  * **Minimum group sizes**, and an explicit "not enough data" result instead
    of a number computed from four days.

Everything here is association, never causation. A confound you did not record
(you drink on Fridays, and you also sleep badly on Fridays for other reasons)
is indistinguishable from a real effect in observational data like this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Sequence

import numpy as np

log = logging.getLogger("whoop.insights")

MIN_GROUP = 5           # days needed on each side of a comparison
MIN_PAIRS = 12          # total paired days before anything is reported
N_PERMUTATIONS = 10_000
N_BOOTSTRAP = 2_000
FDR_Q = 0.10            # false discovery rate we are willing to tolerate

OUTCOMES = {
    "recovery": ("Recovery", "points", True),
    "hrv": ("HRV (RMSSD)", "ms", True),
    "resting_hr": ("Resting HR", "bpm", False),   # lower is better
    "sleep_minutes": ("Sleep", "min", True),
}


@dataclass
class Finding:
    factor: str
    outcome: str
    outcome_label: str
    unit: str
    n_with: int
    n_without: int
    mean_with: float
    mean_without: float
    difference: float
    ci_low: float
    ci_high: float
    p_value: float
    q_value: float
    significant: bool
    direction: str          # "better", "worse", or "unclear"
    lag_days: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def permutation_p(a: np.ndarray, b: np.ndarray, rng: np.random.Generator,
                  n: int = N_PERMUTATIONS) -> tuple[float, float]:
    """Two-sided permutation test on the difference in means.

    Returns (observed_difference, p_value). The +1 in the p-value is the
    standard correction that keeps p from ever being exactly zero -- with a
    finite number of shuffles we cannot distinguish p=0 from p<1/n.
    """
    observed = float(a.mean() - b.mean())
    pool = np.concatenate([a, b])
    n_a = len(a)
    # Shuffle group membership n times in one vectorised pass.
    idx = np.argsort(rng.random((n, len(pool))), axis=1)
    shuffled = pool[idx]
    diffs = shuffled[:, :n_a].mean(axis=1) - shuffled[:, n_a:].mean(axis=1)
    extreme = int(np.sum(np.abs(diffs) >= abs(observed) - 1e-12))
    return observed, (extreme + 1) / (n + 1)


def bootstrap_ci(a: np.ndarray, b: np.ndarray, rng: np.random.Generator,
                 n: int = N_BOOTSTRAP) -> tuple[float, float]:
    """Percentile bootstrap CI for the difference in means."""
    ia = rng.integers(0, len(a), size=(n, len(a)))
    ib = rng.integers(0, len(b), size=(n, len(b)))
    diffs = a[ia].mean(axis=1) - b[ib].mean(axis=1)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(lo), float(hi)


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Convert p-values to FDR-adjusted q-values."""
    m = len(p_values)
    if m == 0:
        return []
    order = np.argsort(p_values)
    ranked = np.asarray(p_values, dtype=float)[order]
    q = ranked * m / np.arange(1, m + 1)
    # Enforce monotonicity walking back from the largest.
    q = np.minimum.accumulate(q[::-1])[::-1]
    q = np.minimum(q, 1.0)
    out = np.empty(m)
    out[order] = q
    return [float(v) for v in out]


def build_pairs(days: list[dict[str, Any]], journal: dict[str, dict[str, Any]],
                lag_days: int = 1) -> list[dict[str, Any]]:
    """Pair each journalled day with the outcome `lag_days` later."""
    by_date = {d["date"]: d for d in days if d.get("has_data")}
    dates = sorted(by_date)
    pairs = []
    for i, date in enumerate(dates):
        target_index = i + lag_days
        if target_index >= len(dates):
            continue
        outcome_day = by_date[dates[target_index]]
        entry = journal.get(date)
        if entry is None:
            continue
        pairs.append({
            "date": date,
            "outcome_date": dates[target_index],
            "tags": set(entry.get("tags", [])),
            "outcomes": {
                "recovery": _num(outcome_day.get("recovery", {}).get("score")),
                "hrv": _num(outcome_day.get("hrv", {}).get("rmssd_ms")),
                "resting_hr": _num(outcome_day.get("heart_rate", {}).get("resting")),
                "sleep_minutes": _num(outcome_day.get("sleep", {}).get("total_minutes")),
            },
        })
    return pairs


def analyse(days: list[dict[str, Any]], journal_entries: list[dict[str, Any]],
            lag_days: int = 1, seed: int = 0) -> dict[str, Any]:
    """Test every journalled tag against every outcome, with FDR control."""
    journal = {e["date"]: e for e in journal_entries}
    pairs = build_pairs(days, journal, lag_days)

    tags = sorted({t for p in pairs for t in p["tags"]})
    if len(pairs) < MIN_PAIRS or not tags:
        return {
            "ready": False,
            "reason": (f"needs at least {MIN_PAIRS} journalled days with a following "
                       f"day of data; have {len(pairs)}"),
            "pairs": len(pairs), "tags_seen": tags, "findings": [],
        }

    rng = np.random.default_rng(seed)
    raw: list[Finding] = []
    skipped: list[dict[str, Any]] = []

    for tag in tags:
        for key, (label, unit, higher_better) in OUTCOMES.items():
            with_vals = np.array([p["outcomes"][key] for p in pairs
                                  if tag in p["tags"] and p["outcomes"][key] is not None])
            without_vals = np.array([p["outcomes"][key] for p in pairs
                                     if tag not in p["tags"] and p["outcomes"][key] is not None])
            if len(with_vals) < MIN_GROUP or len(without_vals) < MIN_GROUP:
                skipped.append({"factor": tag, "outcome": key,
                                "n_with": len(with_vals), "n_without": len(without_vals),
                                "reason": f"needs {MIN_GROUP} days on each side"})
                continue

            diff, p = permutation_p(with_vals, without_vals, rng)
            lo, hi = bootstrap_ci(with_vals, without_vals, rng)
            improved = diff > 0 if higher_better else diff < 0
            raw.append(Finding(
                factor=tag, outcome=key, outcome_label=label, unit=unit,
                n_with=len(with_vals), n_without=len(without_vals),
                mean_with=round(float(with_vals.mean()), 1),
                mean_without=round(float(without_vals.mean()), 1),
                difference=round(diff, 1), ci_low=round(lo, 1), ci_high=round(hi, 1),
                p_value=round(p, 4), q_value=1.0, significant=False,
                direction="better" if improved else "worse", lag_days=lag_days,
            ))

    for finding, q in zip(raw, benjamini_hochberg([f.p_value for f in raw])):
        finding.q_value = round(q, 4)
        finding.significant = q <= FDR_Q

    findings = sorted(raw, key=lambda f: f.q_value)
    return {
        "ready": True,
        "pairs": len(pairs),
        "tags_tested": tags,
        "tests_run": len(raw),
        "fdr_q": FDR_Q,
        "lag_days": lag_days,
        "findings": [f.as_dict() for f in findings],
        "signals": [f.as_dict() for f in findings if f.significant],
        "skipped": skipped,
        "caveat": ("Associations, not causes. Days are not randomised, so an "
                   "unrecorded confound can produce the same pattern. "
                   f"q-values are Benjamini-Hochberg adjusted across all "
                   f"{len(raw)} tests in this run."),
    }


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
