"""Journal storage and the edit / delete / manual-entry rules for activities."""
import pytest
from server.app.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


# --- journal ----------------------------------------------------------------
def test_journal_upsert_replaces(db):
    db.put_journal("2026-01-01", ["alcohol", "late_meal"], {"units": 2}, "note")
    db.put_journal("2026-01-01", ["alcohol"], {"units": 3}, "changed")
    e = db.get_journal("2026-01-01")
    assert e["tags"] == ["alcohol"] and e["amounts"]["units"] == 3
    assert e["notes"] == "changed"


def test_journal_tags_are_deduplicated_and_sorted(db):
    db.put_journal("2026-01-02", ["b", "a", "b"], {}, "")
    assert db.get_journal("2026-01-02")["tags"] == ["a", "b"]


def test_journal_range_and_known_tags(db):
    for i, tags in enumerate([["a"], ["b"], ["a", "c"]], start=1):
        db.put_journal(f"2026-02-{i:02d}", tags, {}, "")
    assert len(db.journal_range("2026-02-01", "2026-02-03")) == 3
    assert len(db.journal_range("2026-02-02", "2026-02-02")) == 1
    assert db.all_tags() == ["a", "b", "c"]


def test_missing_journal_day_is_none(db):
    assert db.get_journal("2099-01-01") is None


# --- activities -------------------------------------------------------------
def test_redetection_updates_an_untouched_bout(db):
    aid = db.upsert_activity(1000, 2000, "walk", 0.4, {"hr_mean": 100})
    db.upsert_activity(1000, 2000, "run", 0.9, {"hr_mean": 150})
    row = db.activities_range(0, 9999)[0]
    assert row["id"] == aid and row["detected_type"] == "run"


def test_user_correction_survives_redetection(db):
    aid = db.upsert_activity(1000, 2000, "walk", 0.4, {})
    db.update_activity(aid, confirmed_type="run", note="hills")
    db.upsert_activity(1000, 2000, "sedentary", 0.9, {"hr_mean": 60})
    row = db.activities_range(0, 9999)[0]
    assert row["confirmed_type"] == "run"        # the correction stands
    assert row["note"] == "hills"
    assert row["detected_type"] == "walk"        # and re-detection did not overwrite
    assert row["source"] == "edited"


def test_times_can_be_corrected(db):
    aid = db.upsert_activity(1000, 2000, "walk", 0.4, {})
    assert db.update_activity(aid, start_unix=1200, end_unix=2400)
    row = db.activities_range(0, 9999)[0]
    assert (row["start_unix"], row["end_unix"]) == (1200, 2400)


def test_update_with_nothing_to_change_is_rejected(db):
    aid = db.upsert_activity(1000, 2000, "walk", 0.4, {})
    assert db.update_activity(aid) is False


def test_soft_delete_hides_and_survives_redetection(db):
    aid = db.upsert_activity(1000, 2000, "walk", 0.4, {})
    assert db.delete_activity(aid)
    assert db.activities_range(0, 9999) == []
    db.upsert_activity(1000, 2000, "walk", 0.4, {})     # detection runs again
    assert db.activities_range(0, 9999) == []           # stays deleted
    assert db.restore_activity(aid)
    assert len(db.activities_range(0, 9999)) == 1


def test_deleted_activity_cannot_be_edited(db):
    aid = db.upsert_activity(1000, 2000, "walk", 0.4, {})
    db.delete_activity(aid)
    assert db.update_activity(aid, confirmed_type="run") is False


def test_manual_activity_is_kept_out_of_training_data(db):
    auto = db.upsert_activity(1000, 2000, "walk", 0.4, {"hr_mean": 100})
    db.update_activity(auto, confirmed_type="walk")
    db.add_manual_activity(5000, 6000, "yoga", "off-wrist")
    # Manual entries have no sensor features, so training on them would be
    # training on a vector of zeros.
    assert [a["confirmed_type"] for a in db.labelled_activities()] == ["walk"]
    assert len(db.activities_range(0, 9999)) == 2


def test_manual_activity_is_idempotent_on_the_same_slot(db):
    a = db.add_manual_activity(5000, 6000, "yoga", "first")
    b = db.add_manual_activity(5000, 6000, "pilates", "second")
    assert a == b
    row = [r for r in db.activities_range(0, 9999) if r["id"] == a][0]
    assert row["confirmed_type"] == "pilates" and row["note"] == "second"


def test_activities_are_scoped_to_their_window(db):
    db.upsert_activity(1000, 2000, "walk", 0.4, {})
    db.upsert_activity(90000, 91000, "run", 0.8, {})
    assert len(db.activities_range(0, 50000)) == 1


def test_unlabelled_activities_are_not_training_data(db):
    db.upsert_activity(1000, 2000, "walk", 0.4, {})
    assert db.labelled_activities() == []


def test_model_state_roundtrip(db):
    db.save_model("m", {"w": [1, 2, 3]}, 12, 0.83)
    got = db.load_model("m")
    assert got["payload"]["w"] == [1, 2, 3] and got["accuracy"] == 0.83
    db.save_model("m", {"w": [9]}, 20, 0.91)
    assert db.load_model("m")["payload"]["w"] == [9]
    assert db.load_model("absent") is None
