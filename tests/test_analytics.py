"""Analytics tests. These metrics are approximations, but they must be
mathematically correct approximations with sane edge-case behaviour."""
import math
import pytest
from server.app.analytics import (
    build_epochs, detect_sleep, malik_filter, pnn50, recovery_score, rmssd,
    sdnn, strain_score, summarise_day, trimp, _ratio_score,
)


# --- HRV --------------------------------------------------------------------
def test_rmssd_matches_hand_calculation():
    rr = [800, 810, 790, 805]
    diffs = [10, -20, 15]
    expected = math.sqrt(sum(d * d for d in diffs) / 3)
    assert rmssd(rr) == pytest.approx(round(expected, 1))


def test_malik_filter_drops_outliers_and_implausible_beats():
    # 2000+ is out of range; 1500 differs >20% from 800 so Malik rejects it.
    assert malik_filter([800, 810, 5000, 1500, 805]) == [800, 810, 805]


def test_hrv_returns_none_on_insufficient_data():
    assert rmssd([]) is None
    assert rmssd([800]) is None
    assert sdnn([800, 810]) is None
    assert pnn50([]) is None


def test_pnn50_counts_successive_diffs_over_50ms():
    # diffs: 60, 60 -> both >50 -> 100%. Both pass the 20% Malik rule.
    assert pnn50([800, 860, 920]) == 100.0
    assert pnn50([800, 805, 810]) == 0.0


def test_higher_variability_gives_higher_rmssd():
    steady = rmssd([800, 802, 798, 801, 799, 800])
    variable = rmssd([800, 850, 810, 860, 820, 870])
    assert variable > steady


# --- strain -----------------------------------------------------------------
def test_strain_is_zero_without_load_and_capped_at_21():
    assert strain_score(0) == 0.0
    assert strain_score(-5) == 0.0
    assert strain_score(10_000) <= 21.0


def test_strain_is_monotonic_with_diminishing_returns():
    scores = [strain_score(t) for t in (10, 50, 150, 300, 600, 900, 1200)]
    assert scores == sorted(scores)
    # Equal load increments buy less strain the higher you already are.
    assert (scores[4] - scores[3]) > (scores[6] - scores[5])


def test_strain_calibration_is_sane():
    # A curve where an ordinary day already scores 20/21 is useless. These
    # bounds pin the shape that makes the number meaningful day to day.
    assert 6 <= strain_score(100) <= 9      # sedentary day
    assert 12 <= strain_score(270) <= 16    # day with one hard hour
    assert strain_score(600) >= 18          # genuinely brutal day


def test_trimp_zero_when_at_rest_and_grows_with_effort():
    rest = [type("E", (), {"hr": 50})() for _ in range(60)]
    hard = [type("E", (), {"hr": 170})() for _ in range(60)]
    assert trimp(rest, 50, 190) == 0.0
    assert trimp(hard, 50, 190) > 0
    # Degenerate config must not divide by zero.
    assert trimp(hard, 190, 190) == 0.0


# --- recovery ---------------------------------------------------------------
def test_ratio_score_direction():
    # HRV above baseline is good; resting HR above baseline is bad.
    assert _ratio_score(120, 100, higher_is_better=True) > 50
    assert _ratio_score(80, 100, higher_is_better=True) < 50
    assert _ratio_score(60, 50, higher_is_better=False) < 50


def test_ratio_score_clamps_to_range():
    assert _ratio_score(1000, 50) == 100.0
    assert _ratio_score(1, 50) == 0.0


def test_recovery_reweights_when_components_missing():
    full = recovery_score(100, 100, 50, 50, 100)
    assert full["score"] is not None and 0 <= full["score"] <= 100
    partial = recovery_score(100, 100, None, None, None)
    assert partial["score"] is not None
    assert partial["weights_used"] == {"hrv": 1.0}   # reweighted, not zero-filled


def test_recovery_none_when_no_data():
    r = recovery_score(None, None, None, None, None)
    assert r["score"] is None and "note" in r


def test_recovery_bands():
    assert recovery_score(200, 100, 40, 50, 100)["band"] == "green"
    assert recovery_score(10, 100, 90, 50, 0)["band"] == "red"


# --- epochs and sleep -------------------------------------------------------
def _rec(unix, hr, g=(0.0, 0.0, 1.0), contact=1, rr=None):
    return {"device_unix": unix, "heart_rate": hr, "gravity_x": g[0],
            "gravity_y": g[1], "gravity_z": g[2], "skin_contact": contact,
            "rr_intervals_ms": rr or []}


def test_build_epochs_buckets_by_minute():
    # 960 and 990 share the minute starting at 960; 1020 starts the next.
    recs = [_rec(960, 60), _rec(990, 70), _rec(1020, 80)]
    epochs = build_epochs(recs)
    assert len(epochs) == 2
    assert [e.unix for e in epochs] == [960, 1020]
    assert epochs[0].hr == 65.0        # 60 and 70 averaged in one minute
    assert epochs[1].hr == 80.0


def test_detect_sleep_finds_a_still_low_hr_block():
    base = 1_700_000_000
    recs = []
    for m in range(120):                       # 2 hours, still, low HR
        recs.append(_rec(base + m * 60, 48))
    blocks = detect_sleep(build_epochs(recs), resting_hr=45)
    assert len(blocks) == 1
    assert blocks[0]["minutes"] >= 100


def test_detect_sleep_ignores_active_periods():
    base = 1_700_000_000
    recs = []
    for m in range(120):                       # high HR, lots of movement
        g = (0.5 if m % 2 else -0.5, 0.0, 0.8)
        recs.extend([_rec(base + m * 60, 120, g), _rec(base + m * 60 + 30, 125, (0.0, 0.9, 0.1))])
    assert detect_sleep(build_epochs(recs), resting_hr=50) == []


def test_detect_sleep_ignores_off_wrist():
    base = 1_700_000_000
    recs = [_rec(base + m * 60, 48, contact=0) for m in range(120)]
    assert detect_sleep(build_epochs(recs), resting_hr=45) == []


def test_short_blocks_are_discarded():
    base = 1_700_000_000
    recs = [_rec(base + m * 60, 48) for m in range(5)]   # only 5 minutes
    assert detect_sleep(build_epochs(recs), resting_hr=45) == []


# --- day rollup -------------------------------------------------------------
def test_summarise_day_empty():
    assert summarise_day([])["has_data"] is False


def test_summarise_day_end_to_end():
    base = 1_700_000_000
    recs = []
    for m in range(480):                       # 8h sleep
        recs.append(_rec(base + m * 60, 50, rr=[900, 910, 895]))
    for m in range(480, 600):                  # 2h activity
        g = (0.6 if m % 2 else -0.6, 0.2, 0.7)
        recs.append(_rec(base + m * 60, 140, g, rr=[420, 425]))
    s = summarise_day(recs, hrv_baseline=40, rhr_baseline=52)
    assert s["has_data"]
    assert s["heart_rate"]["resting"] is not None
    assert s["hrv"]["rmssd_ms"] is not None
    assert s["hrv"]["source"] == "sleep"        # HRV taken from the sleep window
    assert s["sleep"]["total_minutes"] >= 400
    assert s["sleep"]["performance_pct"] is not None
    assert 0 <= s["strain"]["score"] <= 21
    assert s["recovery"]["score"] is not None
    assert 0 <= s["wear"]["on_wrist_pct"] <= 100
