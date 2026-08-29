"""Advanced analytics. The maths is validated against signals whose answers
are known independently, not against the implementation's own output."""
import numpy as np
import pytest

from server.app.analytics import build_epochs
from server.app.circadian import hr_trough, recovery_velocity
from server.app.efficiency import efficiency_index
from server.app.hrv_advanced import (
    MIN_BEATS, _dfa_slope, clean_rr, dfa_alpha1, sample_entropy,
)
from server.app.substances import (
    alcohol_remaining, caffeine_remaining, onboard_at, overlay,
)
from server.app.workload import (
    MIN_DAYS_FOR_ACWR, acwr, payback_plan, sleep_debt, strain_target,
)


# --- DFA: validated against known scaling exponents -------------------------
def _signals(n=8000, seed=4):
    rng = np.random.default_rng(seed)
    white = rng.normal(0, 1, n)
    brown = np.cumsum(rng.normal(0, 1, n))
    f = np.fft.rfftfreq(n)[1:]
    spec = np.concatenate([[0], (1 / np.sqrt(f)) * np.exp(1j * rng.uniform(0, 2 * np.pi, f.size))])
    return white, np.fft.irfft(spec, n=n), brown


@pytest.mark.parametrize("index,expected", [(0, 0.5), (1, 1.0), (2, 1.5)])
def test_dfa_recovers_known_exponents(index, expected):
    # White noise ~0.5, 1/f ~1.0, Brownian ~1.5. If these drift, DFA is broken.
    alpha = _dfa_slope(_signals()[index], range(4, 65))
    assert abs(alpha - expected) < 0.12


def test_alpha1_separates_correlated_from_random():
    rng = np.random.default_rng(1)
    correlated = np.clip(1000 + np.cumsum(rng.normal(0, 4, 1500)), 700, 1300)
    random_rr = 420 + rng.normal(0, 6, 1500)
    assert dfa_alpha1(correlated)["alpha1"] > dfa_alpha1(random_rr)["alpha1"]


def test_alpha1_refuses_dirty_data():
    rng = np.random.default_rng(2)
    rr = np.clip(1000 + np.cumsum(rng.normal(0, 4, 1500)), 700, 1300)
    rr[rng.choice(len(rr), 300, replace=False)] *= 1.9
    out = dfa_alpha1(rr)
    assert out["usable"] is False and out["alpha1"] is None
    assert "artifact" in out["reason"]


def test_alpha1_refuses_short_windows():
    out = dfa_alpha1([900] * (MIN_BEATS - 10))
    assert out["usable"] is False and str(MIN_BEATS) in out["reason"]


def test_artifact_rate_is_always_reported():
    for rr in ([900] * 300, [900, 5000] * 200, []):
        assert "artifact_rate" in dfa_alpha1(rr)


def test_clean_rr_drops_out_of_range_and_ectopics():
    clean, rate = clean_rr([900, 905, 5000, 100, 1800, 895])
    assert 5000 not in clean and 100 not in clean
    assert rate > 0


def test_sample_entropy_regular_below_random():
    rng = np.random.default_rng(5)
    regular = 900 + 5 * np.sin(np.arange(600) / 3)
    noisy = 900 + rng.normal(0, 30, 600)
    assert sample_entropy(regular)["sample_entropy"] < sample_entropy(noisy)["sample_entropy"]


# --- circadian --------------------------------------------------------------
def night(dip_fraction, minutes=480, seed=8):
    rng = np.random.default_rng(seed)
    base = 1_700_000_000
    return build_epochs([{
        "device_unix": base + m * 60,
        "heart_rate": 46 + 14 * abs(m / (minutes - 1) - dip_fraction) + rng.normal(0, .6),
        "gravity_x": 0.005, "gravity_y": 0.0, "gravity_z": 0.98,
        "skin_contact": 1, "rr_intervals_ms": []} for m in range(minutes)])


def test_trough_in_first_half_reads_as_aligned():
    out = hr_trough(night(0.25), 1_700_000_000, 1_700_000_000 + 480 * 60)
    assert out["usable"] and out["half"] == "first" and out["aligned"] is True


def test_trough_in_second_half_reads_as_misaligned():
    out = hr_trough(night(0.78), 1_700_000_000, 1_700_000_000 + 480 * 60)
    assert out["half"] == "second" and out["aligned"] is False
    assert "timing, not which" in out["note"]


def test_short_night_is_refused():
    out = hr_trough(night(0.3, minutes=90), 1_700_000_000, 1_700_000_000 + 90 * 60)
    assert out["usable"] is False


# --- recovery velocity ------------------------------------------------------
def bout_then_rest(decay, seed=8):
    base = 1_700_000_000
    recs = [{"device_unix": base + m * 60, "heart_rate": 160, "gravity_x": .5,
             "gravity_y": 0, "gravity_z": .9, "skin_contact": 1,
             "rr_intervals_ms": []} for m in range(30)]
    recs += [{"device_unix": base + m * 60,
              "heart_rate": 55 + 105 * np.exp(-(m - 30) / decay), "gravity_x": .02,
              "gravity_y": 0, "gravity_z": .98, "skin_contact": 1,
              "rr_intervals_ms": []} for m in range(30, 60)]
    return build_epochs(recs)


def test_faster_decay_gives_bigger_recovery():
    fit = recovery_velocity(bout_then_rest(2.5), 1_700_000_000 + 1800, 55)
    unfit = recovery_velocity(bout_then_rest(16), 1_700_000_000 + 1800, 55)
    assert fit["hrr_1min"] > unfit["hrr_1min"]
    assert fit["fraction_recovered_5min"] > unfit["fraction_recovered_5min"]


def test_recovery_velocity_works_off_the_epoch_grid():
    # Regression: exact-key lookups silently found nothing when the bout
    # boundary did not land on an epoch boundary.
    out = recovery_velocity(bout_then_rest(3), 1_700_000_000 + 1800, 55)
    assert out["usable"] and "hrr_1min" in out


def test_no_data_after_the_bout_is_refused():
    assert recovery_velocity(bout_then_rest(3), 1_700_000_000 + 99999, 55)["usable"] is False


# --- ACWR -------------------------------------------------------------------
def test_acwr_bands():
    assert acwr([10.0] * 40)["band"] == "optimal"
    assert acwr([8.0] * 28 + [20.0] * 7)["ratio"] > 1.3
    assert acwr([15.0] * 28 + [3.0] * 7)["band"] == "detraining"


def test_acwr_needs_enough_history():
    out = acwr([10] * (MIN_DAYS_FOR_ACWR - 1))
    assert out["usable"] is False


def test_acwr_does_not_claim_injury_risk():
    # The injury-risk framing is contested; the wording must not assert it.
    text = (acwr([10.0] * 40)["caveat"] + acwr([8.0] * 28 + [20.0] * 7)["note"]).lower()
    assert "challenged" in text
    assert "injury risk" not in acwr([8.0] * 28 + [20.0] * 7)["note"].lower()


# --- sleep debt -------------------------------------------------------------
def test_debt_compounds_rather_than_summing():
    naive = 7 * 100
    assert sleep_debt([380] * 7, 480) < naive       # decay makes it less than the sum
    assert sleep_debt([380] * 7, 480) > 400          # but it is still substantial


def test_extra_sleep_pays_debt_down_partially():
    after_short = sleep_debt([380] * 5, 480)
    after_catchup = sleep_debt([380] * 5 + [600] * 2, 480)
    assert after_catchup < after_short
    # Two 2-hour lie-ins must NOT clear a 500-minute debt one-for-one.
    assert after_catchup > 0


def test_payback_plan_is_consistent_with_the_debt_model():
    debt = sleep_debt([380] * 7, 480)
    plan = payback_plan(debt, 480)
    for option in plan["options"]:
        remaining = debt
        for _ in range(option["nights"]):
            remaining *= (1 - 0.06)
            remaining = max(0.0, remaining - 0.55 * option["extra_minutes_per_night"])
        assert remaining <= 1.0


def test_no_debt_no_plan():
    assert payback_plan(5, 480)["options"] == []


# --- strain target ----------------------------------------------------------
def test_target_falls_back_without_history():
    out = strain_target(70, [])
    assert out["usable"] and out["source"] == "curve"


def test_target_learns_a_real_relationship():
    rng = np.random.default_rng(3)
    hist = []
    for _ in range(60):
        rec, st = float(rng.uniform(30, 95)), float(rng.uniform(4, 20))
        hist.append({"recovery": rec, "strain": st,
                     "next_recovery": float(np.clip(.55 * rec - 2.1 * st + 55
                                                    + rng.normal(0, 4), 0, 100))})
    out = strain_target(70, hist)
    assert out["source"] == "learned" and out["r2"] > 0.5
    # A better-recovered day must permit more strain.
    assert strain_target(90, hist)["target"] > strain_target(35, hist)["target"]


def test_target_refuses_to_learn_from_noise():
    rng = np.random.default_rng(9)
    noise = [{"recovery": float(rng.uniform(30, 95)), "strain": float(rng.uniform(4, 20)),
              "next_recovery": float(rng.uniform(30, 95))} for _ in range(60)]
    assert strain_target(70, noise)["source"] == "curve"


def test_target_needs_a_recovery_score():
    assert strain_target(None, [])["usable"] is False


# --- substances -------------------------------------------------------------
def test_caffeine_halves_every_half_life():
    assert caffeine_remaining(200, 5) == pytest.approx(100, abs=0.1)
    assert caffeine_remaining(200, 10) == pytest.approx(50, abs=0.1)


def test_alcohol_is_zero_order_not_exponential():
    # The distinction that matters: a half-life model would leave ~1 unit at 8h.
    assert alcohol_remaining(4, 2) == pytest.approx(2.0, abs=0.01)
    assert alcohol_remaining(4, 4) == 0.0
    assert alcohol_remaining(4, 8) == 0.0


def test_onboard_sums_multiple_doses():
    intakes = [{"at": "2026-08-28T18:00:00+00:00", "substance": "caffeine", "amount": 100},
               {"at": "2026-08-28T20:00:00+00:00", "substance": "caffeine", "amount": 100}]
    state = onboard_at(intakes, "2026-08-28T23:00:00+00:00")
    # 100 mg at 5 h ago -> 50; 100 mg at 3 h ago -> 100*0.5^0.6 = 66. Total 116.
    assert state["caffeine_mg"] == pytest.approx(116.0, abs=0.5)
    assert len(state["contributions"]) == 2


def test_future_intake_is_ignored():
    intakes = [{"at": "2026-08-29T09:00:00+00:00", "substance": "caffeine", "amount": 200}]
    assert onboard_at(intakes, "2026-08-28T23:00:00+00:00")["caffeine_mg"] == 0


def test_overlay_states_what_it_cannot_measure():
    out = overlay([], "2026-08-28T23:00:00+00:00", {})
    assert any("slow-wave" in s for s in out["not_measured"])
    assert "zero-order" in out["model"]["alcohol"]


# --- efficiency -------------------------------------------------------------
def eff_bouts(kind, n, hr0, drift, seed=12):
    rng = np.random.default_rng(seed)
    return [{"start_unix": 1_700_000_000 + i * 3 * 86400, "confirmed_type": kind,
             "features": {"hr_mean": hr0 + drift * i + rng.normal(0, 1.5),
                          "motion_mean": 0.34 + rng.normal(0, 0.02),
                          "duration_min": 40}} for i in range(n)]


def test_efficiency_detects_direction():
    assert efficiency_index(eff_bouts("run", 12, 158, -0.9))["activities"][0]["direction"] == "improving"
    assert efficiency_index(eff_bouts("run", 12, 150, 0.8))["activities"][0]["direction"] == "declining"
    assert efficiency_index(eff_bouts("run", 12, 150, 0.0))["activities"][0]["direction"] == "steady"


def test_efficiency_refuses_activities_where_the_proxy_is_invalid():
    out = efficiency_index(eff_bouts("cycle", 12, 140, -1) + eff_bouts("lift", 12, 120, -1))
    assert out["usable"] is False
    assert "cycle" in out["skipped"] and "lift" in out["skipped"]


def test_efficiency_declares_itself_a_proxy():
    out = efficiency_index(eff_bouts("run", 12, 150, -1))
    assert out["proxy"] is True
    assert "not pace or power" in out["caveat"]


def test_efficiency_needs_enough_bouts():
    out = efficiency_index(eff_bouts("run", 3, 150, -1))
    assert out["usable"] is False and "6" in out["skipped"]["run"]
