"""Bout detection and the learned activity classifier."""
import random
from server.app.analytics import build_epochs
from server.app.ml import MIN_PER_CLASS, ActivityClassifier, classify
from server.app.segment import FEATURE_NAMES, find_bouts, rule_classify, to_vector


def day_records(seed=5, workout=True):
    random.seed(seed)
    base = 1_700_000_000
    recs = []
    for m in range(1440):
        h = m // 60
        if h < 7:
            hr, mot = random.gauss(49, 2), 0.005
        elif workout and 18 <= h < 19:
            hr, mot = random.gauss(155, 8), 0.55
        else:
            hr, mot = random.gauss(70, 6), 0.05
        recs.append({"device_unix": base + m * 60, "heart_rate": int(hr),
                     "gravity_x": mot * random.uniform(-1, 1),
                     "gravity_y": mot * random.uniform(-1, 1), "gravity_z": 0.98,
                     "skin_contact": 1, "rr_intervals_ms": [int(60000 / hr)] * 3})
    return recs


def test_motion_is_computed_at_one_sample_per_minute():
    # Regression: deltas were computed only within an epoch, so at one record
    # per minute motion was always 0 and every motion rule silently died.
    epochs = build_epochs(day_records())
    assert max(e.motion for e in epochs) > 0.1
    assert epochs[0].motion < 0.05          # asleep, still


def test_find_bouts_locates_a_workout():
    bouts = find_bouts(day_records(), resting_hr=47, max_hr=190)
    assert bouts
    hard = [b for b in bouts if b["features"]["hr_reserve_mean"] > 0.6]
    assert len(hard) == 1
    assert 50 <= hard[0]["features"]["duration_min"] <= 70


def test_no_workout_day_has_no_high_intensity_bout():
    bouts = find_bouts(day_records(workout=False), resting_hr=47, max_hr=190)
    assert not [b for b in bouts if b["features"]["hr_reserve_mean"] > 0.6]


def test_sleep_bout_is_hinted():
    bouts = find_bouts(day_records(), resting_hr=47, max_hr=190)
    assert any(b.get("hint") == "sleep" for b in bouts)


def test_feature_vector_is_fixed_length_and_ordered():
    bouts = find_bouts(day_records(), resting_hr=47, max_hr=190)
    v = to_vector(bouts[0]["features"])
    assert len(v) == len(FEATURE_NAMES)
    assert to_vector({}) == [0.0] * len(FEATURE_NAMES)   # missing keys -> zeros


def test_rule_classifier_separates_intensity():
    assert rule_classify({"hr_reserve_mean": 0.8})[0] == "hard_workout"
    assert rule_classify({}, hint="sleep")[0] == "sleep"


# --- classifier -------------------------------------------------------------
def sample(kind, rng):
    base = {"run": {"duration_min": 45, "hr_mean": 160, "hr_reserve_mean": 0.78,
                    "motion_mean": 0.6, "hr_recovery": 35},
            "walk": {"duration_min": 40, "hr_mean": 100, "hr_reserve_mean": 0.35,
                     "motion_mean": 0.3, "hr_recovery": 8},
            "sleep": {"duration_min": 420, "hr_mean": 50, "hr_reserve_mean": 0.03,
                      "motion_mean": 0.006, "hr_recovery": 0}}[kind]
    return {k: v * rng.uniform(0.85, 1.15) for k, v in base.items()}


def training_set(n=12):
    rng = random.Random(4)
    return [(to_vector(sample(k, rng)), k)
            for k in ("run", "walk", "sleep") for _ in range(n)]


def test_refuses_to_train_on_too_few_labels():
    report = ActivityClassifier().train(training_set(n=MIN_PER_CLASS - 1))
    assert report["trained"] is False
    assert "not enough" in report["reason"]


def test_trains_and_beats_the_majority_baseline():
    m = ActivityClassifier()
    report = m.train(training_set())
    assert report["trained"]
    assert report["cv_accuracy"] > report["baseline_accuracy"]
    assert set(report["classes"]) == {"run", "walk", "sleep"}


def test_cross_validation_is_not_self_graded():
    # Accuracy must come from held-out folds, so a pathological set that cannot
    # generalise should not score 100%.
    rng = random.Random(1)
    noise = [(to_vector({k: rng.uniform(0, 1) for k in FEATURE_NAMES}),
              rng.choice(["a", "b"])) for _ in range(40)]
    report = ActivityClassifier().train(noise)
    assert report["trained"]
    assert report["cv_accuracy"] < 0.95


def test_predicts_each_class():
    m = ActivityClassifier(); m.train(training_set())
    rng = random.Random(9)
    for kind in ("run", "walk", "sleep"):
        assert m.predict(sample(kind, rng))[0] == kind


def test_payload_roundtrip_preserves_predictions():
    m = ActivityClassifier(); m.train(training_set())
    rng = random.Random(3)
    features = sample("run", rng)
    restored = ActivityClassifier.from_payload(m.to_payload())
    assert restored.predict(features) == m.predict(features)


def test_stale_feature_set_is_rejected():
    m = ActivityClassifier(); m.train(training_set())
    payload = m.to_payload()
    payload["features"] = ["something", "else"]
    assert ActivityClassifier.from_payload(payload).weights is None


def test_classify_falls_back_to_rules_until_model_is_good():
    features = {"hr_reserve_mean": 0.8}
    assert classify(features, None, None)[2] == "rules"

    weak = ActivityClassifier(); weak.train(training_set()); weak.accuracy = 0.30
    assert classify(features, None, weak)[2] == "rules"

    good = ActivityClassifier(); good.train(training_set())
    assert classify(features, None, good)[2] == "model"


def test_explain_returns_a_weight_per_feature():
    m = ActivityClassifier(); m.train(training_set())
    weights = m.explain()
    assert set(weights) == {"run", "walk", "sleep"}
    assert set(weights["run"]) == set(FEATURE_NAMES)
