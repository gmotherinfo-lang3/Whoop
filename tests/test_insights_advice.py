"""Statistics and suggestions. The bar here is that the analysis must find real
effects AND stay quiet about noise -- the second is the harder half."""
import numpy as np
from server.app.advice import MIN_READINESS_DAYS, suggest
from server.app.insights import (
    MIN_PAIRS, analyse, benjamini_hochberg, bootstrap_ci, permutation_p,
)
from server.app.readiness import activity_learning_status, insight_learning_status


# --- multiple-comparison correction -----------------------------------------
def test_bh_matches_the_published_worked_example():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205]
    q = benjamini_hochberg(p)
    assert [round(v, 4) for v in q] == [0.008, 0.032, 0.0672, 0.0672, 0.0672,
                                        0.08, 0.0846, 0.205]


def test_bh_is_monotonic_and_bounded():
    q = benjamini_hochberg([0.9, 0.001, 0.5, 0.02])
    assert all(0 <= v <= 1 for v in q)
    pairs = sorted(zip([0.9, 0.001, 0.5, 0.02], q))
    assert [v for _, v in pairs] == sorted(v for _, v in pairs)


def test_bh_handles_empty():
    assert benjamini_hochberg([]) == []


# --- permutation test -------------------------------------------------------
def test_permutation_detects_a_real_difference():
    rng = np.random.default_rng(0)
    a, b = rng.normal(45, 8, 30), rng.normal(65, 8, 30)
    diff, p = permutation_p(a, b, rng)
    assert diff < 0 and p < 0.01


def test_permutation_is_quiet_on_noise():
    rng = np.random.default_rng(1)
    a, b = rng.normal(60, 8, 30), rng.normal(60, 8, 30)
    _, p = permutation_p(a, b, rng)
    assert p > 0.05


def test_permutation_p_is_never_zero():
    rng = np.random.default_rng(2)
    _, p = permutation_p(np.zeros(20), np.ones(20) * 100, rng, n=200)
    assert p > 0


def test_bootstrap_ci_brackets_the_true_difference():
    rng = np.random.default_rng(3)
    a, b = rng.normal(50, 5, 60), rng.normal(60, 5, 60)
    lo, hi = bootstrap_ci(a, b, rng)
    assert lo < -10 < hi          # true difference is -10
    assert hi < 0                 # and the interval excludes zero


# --- end-to-end analysis ----------------------------------------------------
def make_days(n, effect_on=None, effect=-20.0, seed=0):
    """n days; when `effect_on` fires on day d, day d+1's recovery shifts."""
    rng = np.random.default_rng(seed)
    days, journal = [], []
    flags = [(i % 3 == 0) for i in range(n)]
    for i in range(n):
        date = f"2026-01-{i + 1:02d}"
        shifted = effect_on is not None and i > 0 and flags[i - 1]
        days.append({
            "date": date, "has_data": True,
            "recovery": {"score": float(rng.normal(65 + (effect if shifted else 0), 4))},
            "hrv": {"rmssd_ms": float(rng.normal(60, 5))},
            "heart_rate": {"resting": float(rng.normal(50, 2))},
            "sleep": {"total_minutes": float(rng.normal(440, 20))},
        })
        journal.append({"date": date,
                        "tags": (["alcohol"] if flags[i] else []) + ["coffee"]})
    return days, journal


def test_finds_a_planted_effect():
    days, journal = make_days(45, effect_on="alcohol", effect=-20.0)
    out = analyse(days, journal, lag_days=1, seed=1)
    assert out["ready"]
    hits = [f for f in out["signals"]
            if f["factor"] == "alcohol" and f["outcome"] == "recovery"]
    assert hits, "should detect a 20-point planted effect"
    assert hits[0]["direction"] == "worse"
    assert hits[0]["ci_high"] < 0


def test_reports_nothing_when_there_is_nothing():
    days, journal = make_days(45, effect_on=None)
    out = analyse(days, journal, lag_days=1, seed=2)
    assert out["ready"]
    # With no planted effect, FDR control should leave the signal list empty.
    assert out["signals"] == []


def test_refuses_to_analyse_too_little_data():
    days, journal = make_days(6, effect_on="alcohol")
    out = analyse(days, journal)
    assert out["ready"] is False
    assert str(MIN_PAIRS) in out["reason"]


def test_a_factor_present_every_day_is_skipped_not_tested():
    # "coffee" is on every day, so there is no comparison group.
    days, journal = make_days(45, effect_on="alcohol")
    out = analyse(days, journal, seed=3)
    assert "coffee" not in {f["factor"] for f in out["findings"]}
    assert any(s["factor"] == "coffee" for s in out["skipped"])


def test_every_finding_carries_its_evidence():
    days, journal = make_days(45, effect_on="alcohol")
    for f in analyse(days, journal, seed=4)["findings"]:
        for key in ("n_with", "n_without", "ci_low", "ci_high", "p_value", "q_value"):
            assert f[key] is not None


# --- suggestions ------------------------------------------------------------
def day(rhr=50, hrv=60, temp=7000, resp=300, recovery=70, sleep=450, strain=10):
    return {"has_data": True, "heart_rate": {"resting": rhr},
            "hrv": {"rmssd_ms": hrv},
            "sensors": {"skin_temp_raw": temp, "resp_rate_raw": resp},
            "recovery": {"score": recovery},
            "sleep": {"total_minutes": sleep, "need_hours": 8},
            "strain": {"score": strain}}


def baseline(n=30):
    rng = np.random.default_rng(7)
    return [day(rhr=50 + rng.normal(0, 1.5), hrv=60 + rng.normal(0, 4),
                temp=7000 + rng.normal(0, 25), resp=300 + rng.normal(0, 7),
                recovery=70, sleep=460) for _ in range(n)]


def test_no_suggestions_without_a_baseline():
    out = suggest(day(), baseline(3))
    assert out["ready"] is False
    assert str(MIN_READINESS_DAYS) in out["reason"]


def test_illness_signal_needs_multiple_concordant_markers():
    # One marker off is not enough -- that is just a normal bad night.
    out = suggest(day(rhr=58), baseline())
    assert not [s for s in out["suggestions"] if s["id"] == "possible_illness"]


def test_illness_signal_fires_on_the_full_pattern():
    out = suggest(day(rhr=58, hrv=42, temp=7080, resp=325, recovery=25), baseline())
    hit = [s for s in out["suggestions"] if s["id"] == "possible_illness"]
    assert hit and hit[0]["severity"] == "warning"
    assert len(hit[0]["evidence"]["flagged"]) >= 3
    # It must not claim to diagnose.
    assert "not as a diagnosis" in hit[0]["detail"]


def test_high_recovery_suggests_pushing_and_low_suggests_resting():
    assert any(s["id"] == "ready_to_push" for s in suggest(day(recovery=85), baseline())["suggestions"])
    assert any(s["id"] == "take_it_easy" for s in suggest(day(recovery=20), baseline())["suggestions"])


def test_sleep_debt_only_fires_when_meaningful():
    rested = suggest(day(sleep=480), baseline())["suggestions"]
    assert not [s for s in rested if s["id"] == "sleep_debt"]
    short = [day(sleep=300) for _ in range(30)]
    assert any(s["id"] == "sleep_debt" for s in suggest(day(sleep=300), short)["suggestions"])


def test_every_suggestion_carries_evidence_and_a_disclaimer():
    out = suggest(day(recovery=85), baseline())
    assert "not medical advice" in out["disclaimer"].lower()
    for s in out["suggestions"]:
        assert s["evidence"] and s["confidence"] in {"low", "moderate", "high"}


# --- readiness reporting ----------------------------------------------------
def test_activity_status_reports_what_is_missing_and_an_eta():
    s = activity_learning_status([], detected_recent=28, window_days=14,
                                 model_accuracy=None)
    assert s["active"] is False and s["using"] == "rules"
    assert s["labels_needed"] > 0 and s["eta"]["days"] > 0


def test_activity_status_blocks_on_poor_accuracy():
    labels = [{"confirmed_type": "run"}] * 6 + [{"confirmed_type": "walk"}] * 6
    s = activity_learning_status(labels, 28, 14, model_accuracy=0.40)
    assert s["active"] is False and "accuracy" in s["blocker"]
    assert activity_learning_status(labels, 28, 14, model_accuracy=0.9)["active"]


def test_eta_is_honest_when_it_cannot_be_computed():
    s = activity_learning_status([], detected_recent=0, window_days=14,
                                 model_accuracy=None)
    assert s["eta"]["days"] is None
    assert "cannot estimate" in s["eta"]["text"]


def test_insight_status_flags_factors_that_cannot_be_tested():
    s = insight_learning_status(pairs=20, tag_day_counts={"alcohol": 8, "travel": 1},
                                journalled_days=21, span_days=21)
    assert s["active"] and s["testable_factors"] == ["alcohol"]
    assert "travel" in s["factors_not_yet_testable"]
