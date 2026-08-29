"""The three detail views: fitness age, stress and the health monitor.

Each module is checked against a property that holds independently of the
implementation -- a known VO2 max maps to a known age band, a calm day scores
below a busy one, a value inside a wide baseline is in range -- rather than
against numbers the code happened to produce.
"""
import statistics

import pytest

from server.app.analytics import Epoch
from server.app.monitor import MIN_BASELINE_DAYS, channel, summarise
from server.app.norms import (
    HR_RATIO_FACTOR, MAX_AGE, MIN_AGE, VO2MAX_MEDIAN, estimate_vo2max,
    fitness_age, trend, vo2max_trend,
)
from server.app.stress import (
    MIN_BASELINE_EPOCHS, SCALE_MAX, raw_series, stress_day,
)


# --- VO2 max estimate -------------------------------------------------------
def test_vo2max_follows_the_published_ratio():
    out = estimate_vo2max(max_hr=190, resting_hr=50)
    assert out["usable"]
    assert out["vo2max"] == pytest.approx(HR_RATIO_FACTOR * 190 / 50, abs=0.05)
    assert out["ratio"] == pytest.approx(3.8, abs=0.01)


def test_fitter_person_scores_higher():
    fit = estimate_vo2max(190, 45)["vo2max"]
    unfit = estimate_vo2max(190, 75)["vo2max"]
    assert fit > unfit


@pytest.mark.parametrize("max_hr,resting", [
    (0, 50),          # no maximum
    (190, 0),         # no resting
    (190, 200),       # resting above maximum
    (190, 100),       # ratio 1.9, below the method's range
    (190, 30),        # ratio 6.3, almost certainly a bad resting estimate
])
def test_implausible_inputs_refuse_rather_than_guess(max_hr, resting):
    out = estimate_vo2max(max_hr, resting)
    assert not out["usable"] and out["reason"]


# --- fitness age ------------------------------------------------------------
def test_median_vo2max_returns_that_age():
    """A man at the 45-year-old median should read as 45."""
    median45 = VO2MAX_MEDIAN["male"][45]
    assert fitness_age(median45, "male")["fitness_age"] == pytest.approx(45, abs=0.1)


def test_between_bands_interpolates():
    mid = (VO2MAX_MEDIAN["male"][35] + VO2MAX_MEDIAN["male"][45]) / 2
    age = fitness_age(mid, "male")["fitness_age"]
    assert 39.5 <= age <= 40.5


def test_clamped_at_both_ends():
    top = fitness_age(90.0, "male")
    bottom = fitness_age(5.0, "male")
    assert top["fitness_age"] == MIN_AGE and "youngest" in top["edge"]
    assert bottom["fitness_age"] == MAX_AGE and "oldest" in bottom["edge"]


def test_fitness_age_is_monotonic_in_vo2max():
    ages = [fitness_age(v, "male")["fitness_age"] for v in range(25, 50)]
    assert ages == sorted(ages, reverse=True)


def test_female_reference_reads_younger_for_the_same_vo2max():
    """The female medians are lower, so the same VO2 max maps to a lower age."""
    v = 34.0
    assert (fitness_age(v, "female")["fitness_age"]
            < fitness_age(v, "male")["fitness_age"])


def test_verdict_needs_more_than_rounding_noise():
    median45 = VO2MAX_MEDIAN["male"][45]
    assert fitness_age(median45, "male", 45.0)["verdict"] == "in line"
    assert fitness_age(median45, "male", 60.0)["verdict"] == "younger"
    assert fitness_age(median45, "male", 30.0)["verdict"] == "older"


# --- within-person trend ----------------------------------------------------
def _history(start, per_day, n=30):
    return [{"days_ago": d, "fitness_age": start + per_day * (n - d)}
            for d in range(n, 0, -1)]


def test_trend_needs_history():
    assert not trend(_history(40, 0.0, n=5))["usable"]


def test_falling_fitness_age_reads_as_improving():
    out = trend(_history(45, -0.02))
    assert out["usable"] and out["direction"] == "improving"
    assert out["years_per_month"] == pytest.approx(-0.6, abs=0.05)
    assert "biological" in out["note"]


def test_rising_reads_as_declining_and_flat_reads_as_steady():
    assert trend(_history(40, 0.02))["direction"] == "declining"
    assert trend(_history(40, 0.0))["direction"] == "steady"


def test_a_clamped_fitness_age_refuses_to_report_a_slope():
    """Pinned at the edge of the table it cannot move, so "steady" would lie."""
    hist = [{"days_ago": d, "fitness_age": 20.0,
             "edge": "at or above the median for the youngest band"}
            for d in range(30, 0, -1)]
    out = trend(hist)
    assert not out["usable"] and out["clamped"]
    assert "edge of the reference table" in out["reason"]
    assert out["reason"][0].isupper() and out["reason"].endswith(".")


def test_a_few_clamped_days_do_not_block_the_trend():
    hist = _history(45, -0.02)
    for h in hist[:3]:
        h["edge"] = "at or above the median for the youngest band"
    assert trend(hist)["usable"]


def test_vo2max_trend_reads_the_other_way_round():
    """Rising VO2 max is improving; rising fitness age is declining."""
    hist = [{"days_ago": d, "vo2max": 40.0 + 0.02 * (30 - d)} for d in range(30, 0, -1)]
    up = vo2max_trend(hist)
    assert up["usable"] and up["direction"] == "improving"
    assert up["per_month"] == pytest.approx(0.6, abs=0.05)
    assert "VO" in up["note"]

    down = vo2max_trend([{"days_ago": d, "vo2max": 40.0 - 0.02 * (30 - d)}
                         for d in range(30, 0, -1)])
    assert down["direction"] == "declining"
    flat = vo2max_trend([{"days_ago": d, "vo2max": 40.0} for d in range(30, 0, -1)])
    assert flat["direction"] == "steady"


def test_vo2max_trend_still_works_where_fitness_age_is_clamped():
    """The whole point: a very fit person still gets a usable signal."""
    hist = [{"days_ago": d, "fitness_age": 20.0, "vo2max": 55.0 + 0.03 * (30 - d),
             "edge": "at or above the median for the youngest band"}
            for d in range(30, 0, -1)]
    assert not trend(hist)["usable"]
    assert vo2max_trend(hist)["direction"] == "improving"


def test_vo2max_trend_needs_history():
    assert not vo2max_trend([{"days_ago": d, "vo2max": 40.0}
                             for d in range(5, 0, -1)])["usable"]


def test_trend_ignores_unusable_points():
    hist = _history(45, -0.02)
    hist[0]["fitness_age"] = None
    hist[1].pop("fitness_age")
    assert trend(hist)["days"] == len(hist) - 2


# --- stress -----------------------------------------------------------------
def _epochs(hr, minutes=180, rmssd_ms=45.0, start=1_700_000_000):
    """A flat day at one heart rate, with RR intervals of a chosen spread."""
    out = []
    for i in range(minutes):
        mean = 60000.0 / hr
        # Alternating intervals give an exactly predictable RMSSD.
        rr = [mean + (rmssd_ms / 2 if j % 2 else -rmssd_ms / 2) for j in range(8)]
        out.append(Epoch(unix=start + i * 60, hr=float(hr), motion=0.0,
                         on_wrist=True, rr=rr))
    return out


def test_raw_series_rises_with_heart_rate():
    calm = raw_series(_epochs(55), resting_hr=50, max_hr=190, rmssd_baseline=None)
    busy = raw_series(_epochs(120), resting_hr=50, max_hr=190, rmssd_baseline=None)
    assert statistics.mean(v for _, v in busy) > statistics.mean(v for _, v in calm)


def test_raw_series_is_bounded_even_past_max_heart_rate():
    over = raw_series(_epochs(250), resting_hr=50, max_hr=190, rmssd_baseline=None)
    under = raw_series(_epochs(40), resting_hr=50, max_hr=190, rmssd_baseline=None)
    assert all(v == pytest.approx(1.0) for _, v in over)
    assert all(v == pytest.approx(0.0) for _, v in under)


def test_suppressed_hrv_raises_stress_at_the_same_heart_rate():
    """The point of the HRV term: same HR, less variability, more arousal."""
    kw = dict(resting_hr=50, max_hr=190, rmssd_baseline=50.0)
    normal = raw_series(_epochs(90, rmssd_ms=50.0), **kw)
    flattened = raw_series(_epochs(90, rmssd_ms=10.0), **kw)
    assert (statistics.mean(v for _, v in flattened)
            > statistics.mean(v for _, v in normal))


def test_stress_day_refuses_without_a_resting_heart_rate():
    out = stress_day(_epochs(70), None, 190, None, [0.2] * MIN_BASELINE_EPOCHS)
    assert not out["usable"] and "resting" in out["reason"]


def test_stress_day_refuses_until_it_knows_your_range():
    out = stress_day(_epochs(70), 50, 190, None, [0.2] * (MIN_BASELINE_EPOCHS - 1))
    assert not out["usable"] and "hours of history" in out["reason"]


def test_stress_is_a_position_within_your_own_history():
    baseline = [i / MIN_BASELINE_EPOCHS for i in range(MIN_BASELINE_EPOCHS)]
    calm = stress_day(_epochs(52), 50, 190, None, baseline)
    busy = stress_day(_epochs(150), 50, 190, None, baseline)
    assert calm["usable"] and busy["usable"]
    assert 0.0 <= calm["current"] < busy["current"] <= SCALE_MAX
    assert calm["band"] == "low" and busy["band"] == "high"


def test_stress_day_accounts_for_every_minute():
    baseline = [i / MIN_BASELINE_EPOCHS for i in range(MIN_BASELINE_EPOCHS)]
    day = stress_day(_epochs(90, minutes=240), 50, 190, None, baseline)
    assert sum(day["minutes"].values()) == 240
    assert sum(day["share"].values()) == pytest.approx(100, abs=1)
    assert day["peak"] >= day["average"]
    assert len(day["points"]) == 240


def test_stress_day_handles_a_day_with_no_heart_rate():
    blank = [Epoch(unix=1_700_000_000 + i * 60, hr=None, motion=0.0,
                   on_wrist=False, rr=[]) for i in range(60)]
    out = stress_day(blank, 50, 190, None, [0.2] * MIN_BASELINE_EPOCHS)
    assert not out["usable"] and "no heart rate" in out["reason"]


# --- health monitor ---------------------------------------------------------
def test_channel_waits_for_a_week_of_baseline():
    out = channel("Skin temp", "°C", 33.4, [33.3] * (MIN_BASELINE_DAYS - 1), decimals=1)
    assert out["in_range"] is None and "building baseline" in out["state"]


def test_channel_reports_no_reading_without_a_value():
    out = channel("SpO2", "%", None, [97.0] * 30)
    assert out["value"] is None and out["state"] == "no reading"


def test_value_inside_the_personal_range_is_unremarkable():
    history = [float(v) for v in range(90, 110)]
    out = channel("Resting HR", "bpm", 100, history)
    assert out["in_range"] is True and out["state"] == "within your range"
    assert out["low"] <= 100 <= out["high"]


def test_values_outside_the_range_say_which_way():
    history = [float(v) for v in range(90, 110)]
    assert channel("Resting HR", "bpm", 130, history)["state"] == "above your usual range"
    assert channel("Resting HR", "bpm", 60, history)["state"] == "below your usual range"


def test_range_is_the_10th_to_90th_percentile_not_the_extremes():
    history = [float(v) for v in range(0, 101)]
    out = channel("Anything", "", 50, history)
    assert out["low"] == pytest.approx(10, abs=1)
    assert out["high"] == pytest.approx(90, abs=1)


def test_delta_is_measured_against_the_median():
    out = channel("Resting HR", "bpm", 55, [50.0] * 30)
    assert out["baseline_median"] == 50
    assert out["delta_pct"] == pytest.approx(10.0, abs=0.1)


def test_zero_median_does_not_divide_by_zero():
    out = channel("Motion", "", 0, [0.0] * 30)
    assert "delta_pct" not in out and out["baseline_median"] == 0


def test_summary_names_the_channels_that_are_out():
    chans = [channel("A", "", 100, [float(v) for v in range(90, 110)]),
             channel("B", "", 500, [float(v) for v in range(90, 110)])]
    out = summarise(chans)
    assert out["ready"] and out["ok"] is False
    assert out["headline"] == "1 outside range" and out["detail"] == "B"


def test_summary_is_not_ready_while_every_channel_is_still_building():
    chans = [channel("A", "", 100, [100.0, 101.0])]
    assert summarise(chans)["ready"] is False


def test_summary_is_clean_when_everything_sits_inside():
    chans = [channel("A", "", 100, [float(v) for v in range(90, 110)])]
    out = summarise(chans)
    assert out["ok"] and out["headline"] == "All within range"
