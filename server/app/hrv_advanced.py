"""Non-linear HRV: DFA alpha-1 and sample entropy.

RMSSD tells you how much beat-to-beat variability there is. These describe its
*structure* -- whether the variability is correlated or random -- which is what
tracks autonomic balance rather than just autonomic volume.

A warning that matters more here than anywhere else in this codebase: DFA
alpha-1 is exquisitely sensitive to artifacts. A single missed or extra beat in
a few hundred can swing it by 0.2, which is the width of the entire band people
make decisions on. So the artifact rate is computed, reported alongside every
value, and a window that is too dirty returns nothing rather than a number that
looks authoritative.

Reference bands (Gronwald & Hoos, endurance literature):
  alpha-1 > 0.75   correlated, low intensity / aerobic warm-up
  0.5 - 0.75       transitional
  alpha-1 < 0.5    uncorrelated, at or above the aerobic threshold
Those bands come from cycling and running studies on chest straps. A wrist
optical sensor is a noisier source, so treat them as indicative.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

# Beats outside this range are not beats.
RR_MIN, RR_MAX = 300.0, 2000.0
# A beat differing this much from the local median is treated as an artifact.
ARTIFACT_TOLERANCE = 0.20
# Above this share of corrected beats, alpha-1 is not reported at all.
MAX_ARTIFACT_RATE = 0.05
# Short-scale box sizes, in beats. This window IS the definition of alpha-1.
ALPHA1_MIN_BOX, ALPHA1_MAX_BOX = 4, 16
MIN_BEATS = 120


def clean_rr(rr: Sequence[float]) -> tuple[np.ndarray, float]:
    """Drop implausible and ectopic beats. Returns (clean, artifact_rate).

    Compares each beat against a local median rather than its predecessor, so a
    single bad beat does not drag a whole run out with it.
    """
    raw = np.asarray([float(v) for v in rr], dtype=float)
    if raw.size == 0:
        return raw, 0.0

    in_range = (raw >= RR_MIN) & (raw <= RR_MAX)
    kept = raw[in_range]
    if kept.size < 5:
        return kept, 1.0 - kept.size / raw.size if raw.size else 0.0

    # Rolling median over a 5-beat window, edges padded.
    padded = np.pad(kept, 2, mode="edge")
    local = np.median(np.lib.stride_tricks.sliding_window_view(padded, 5), axis=1)
    ok = np.abs(kept - local) <= ARTIFACT_TOLERANCE * local

    clean = kept[ok]
    removed = raw.size - clean.size
    return clean, (removed / raw.size if raw.size else 0.0)


def _dfa_slope(series: np.ndarray, boxes: Sequence[int]) -> float | None:
    """Detrended fluctuation analysis: slope of log F(n) against log n."""
    n = series.size
    if n < max(boxes) * 2:
        return None

    # Integrate the mean-centred series into a random-walk profile.
    profile = np.cumsum(series - series.mean())

    sizes, fluct = [], []
    for box in boxes:
        count = n // box
        if count < 2:
            continue
        trimmed = profile[: count * box].reshape(count, box)
        x = np.arange(box, dtype=float)
        # Least-squares line per box, vectorised.
        x_mean = x.mean()
        y_mean = trimmed.mean(axis=1, keepdims=True)
        denom = ((x - x_mean) ** 2).sum()
        if denom == 0:
            continue
        slope = ((trimmed - y_mean) * (x - x_mean)).sum(axis=1) / denom
        trend = y_mean + slope[:, None] * (x - x_mean)
        rms = np.sqrt(((trimmed - trend) ** 2).mean())
        if rms > 0:
            sizes.append(box)
            fluct.append(rms)

    if len(sizes) < 3:
        return None
    coeffs = np.polyfit(np.log(sizes), np.log(fluct), 1)
    return float(coeffs[0])


def dfa_alpha1(rr: Sequence[float]) -> dict[str, Any]:
    """Short-scale scaling exponent of an RR series."""
    clean, artifact_rate = clean_rr(rr)
    out: dict[str, Any] = {
        "beats": int(clean.size),
        "artifact_rate": round(artifact_rate, 4),
        "alpha1": None,
        "band": None,
        "usable": False,
    }
    if clean.size < MIN_BEATS:
        out["reason"] = f"needs {MIN_BEATS} clean beats, have {clean.size}"
        return out
    if artifact_rate > MAX_ARTIFACT_RATE:
        out["reason"] = (f"{artifact_rate:.1%} of beats were artifacts; above "
                         f"{MAX_ARTIFACT_RATE:.0%} alpha-1 is not trustworthy")
        return out

    alpha = _dfa_slope(clean, range(ALPHA1_MIN_BOX, ALPHA1_MAX_BOX + 1))
    if alpha is None:
        out["reason"] = "not enough data for the short-scale fit"
        return out

    out["alpha1"] = round(alpha, 3)
    out["usable"] = True
    out["band"] = ("aerobic" if alpha > 0.75
                   else "threshold" if alpha < 0.5
                   else "transitional")
    return out


def sample_entropy(rr: Sequence[float], m: int = 2, r: float = 0.2,
                   max_beats: int = 3000) -> dict[str, Any]:
    """SampEn(m, r*SD): how unpredictable the series is. Lower = more regular."""
    clean, artifact_rate = clean_rr(rr)
    out: dict[str, Any] = {"beats": int(clean.size),
                           "artifact_rate": round(artifact_rate, 4),
                           "sample_entropy": None, "usable": False}
    if clean.size < MIN_BEATS:
        out["reason"] = f"needs {MIN_BEATS} clean beats, have {clean.size}"
        return out

    # SampEn is O(n^2); cap the window so a long night cannot stall a request.
    series = clean[-max_beats:]
    sd = series.std()
    if sd == 0:
        out["reason"] = "no variability in the window"
        return out
    tol = r * sd

    def _count(length: int) -> int:
        templates = np.lib.stride_tricks.sliding_window_view(series, length)
        total = 0
        for i in range(templates.shape[0] - 1):
            dist = np.max(np.abs(templates[i + 1:] - templates[i]), axis=1)
            total += int(np.count_nonzero(dist <= tol))
        return total

    a, b = _count(m + 1), _count(m)
    if a == 0 or b == 0:
        out["reason"] = "no matching templates at this tolerance"
        return out
    out["sample_entropy"] = round(-math.log(a / b), 3)
    out["usable"] = True
    return out
