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


# --- one night is one bout --------------------------------------------------
# Seen in the app: a single night showed up as seven SLEEP entries, all
# starting 12:00 AM and ending 06:24, 06:26, 06:27, 06:29, 06:33... Detection
# re-runs while the strap is still streaming and each run sees a slightly
# longer night; keying on (start_unix, end_unix) made every one a new row.
NIGHT = 1_788_066_000        # local midnight


def _sleep(db, start, end, **kw):
    return db.upsert_activity(start, end, "sleep", 0.8, {"duration_min": (end - start) / 60},
                              **kw)


def test_a_night_that_grows_stays_one_activity(tmp_path):
    from server.app.db import Database
    db = Database(tmp_path / "a.db")
    ids = [_sleep(db, NIGHT, NIGHT + m * 60) for m in (384, 386, 387, 389, 393)]
    rows = [a for a in db.activities_range(NIGHT - 3600, NIGHT + 86400) if not a["deleted"]]
    assert len(rows) == 1, f"{len(rows)} rows for one night"
    assert len(set(ids)) == 1, "the same row should be updated each time"
    assert rows[0]["end_unix"] == NIGHT + 393 * 60, "the bout should extend"


def test_a_night_detected_in_fragments_becomes_one(tmp_path):
    from server.app.db import Database
    db = Database(tmp_path / "a.db")
    _sleep(db, NIGHT, NIGHT + 82 * 60)               # 12:00 - 01:22
    _sleep(db, NIGHT + 98 * 60, NIGHT + 394 * 60)    # 01:38 - 06:34
    _sleep(db, NIGHT, NIGHT + 384 * 60)              # the whole night
    rows = [a for a in db.activities_range(NIGHT - 3600, NIGHT + 86400) if not a["deleted"]]
    assert len(rows) == 1
    assert rows[0]["start_unix"] == NIGHT
    assert rows[0]["end_unix"] == NIGHT + 394 * 60


def test_two_genuinely_separate_bouts_stay_separate(tmp_path):
    from server.app.db import Database
    db = Database(tmp_path / "a.db")
    _sleep(db, NIGHT, NIGHT + 380 * 60)                          # the night
    db.upsert_activity(NIGHT + 800 * 60, NIGHT + 855 * 60,       # a run that evening
                       "run", 0.7, {"duration_min": 55})
    rows = [a for a in db.activities_range(NIGHT - 3600, NIGHT + 86400) if not a["deleted"]]
    assert len(rows) == 2


def test_a_barely_touching_bout_is_not_absorbed(tmp_path):
    """A workout starting right after a nap ends is its own thing."""
    from server.app.db import Database
    db = Database(tmp_path / "a.db")
    db.upsert_activity(NIGHT, NIGHT + 60 * 60, "sleep", 0.8, {})
    db.upsert_activity(NIGHT + 59 * 60, NIGHT + 120 * 60, "run", 0.7, {})
    rows = [a for a in db.activities_range(NIGHT - 3600, NIGHT + 86400) if not a["deleted"]]
    assert len(rows) == 2


def test_re_detection_never_reopens_something_you_deleted(tmp_path):
    from server.app.db import Database
    db = Database(tmp_path / "a.db")
    first = _sleep(db, NIGHT, NIGHT + 384 * 60)
    db.delete_activity(first)
    _sleep(db, NIGHT, NIGHT + 390 * 60)
    rows = [a for a in db.activities_range(NIGHT - 3600, NIGHT + 86400) if not a["deleted"]]
    assert len(rows) == 1 and rows[0]["id"] != first


def test_re_detection_does_not_swallow_something_you_entered(tmp_path):
    from server.app.db import Database
    db = Database(tmp_path / "a.db")
    mine = db.add_manual_activity(NIGHT, NIGHT + 380 * 60, "sleep", "went to bed early")
    _sleep(db, NIGHT, NIGHT + 384 * 60)
    rows = [a for a in db.activities_range(NIGHT - 3600, NIGHT + 86400) if not a["deleted"]]
    assert mine in [r["id"] for r in rows]
    assert [r for r in rows if r["id"] == mine][0]["note"] == "went to bed early"


def test_databases_full_of_the_old_duplicates_are_cleaned_up(tmp_path):
    """The migration has to fix the nights already recorded that way."""
    import sqlite3
    from server.app.db import Database

    path = tmp_path / "a.db"
    db = Database(path)
    with db._lock:                                   # write them the old way
        for m in (384, 386, 387, 389, 393):
            db._conn.execute(
                "INSERT INTO activities (start_unix, end_unix, detected_type, "
                "confidence, features, source, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (NIGHT, NIGHT + m * 60, "sleep", 0.8, "{}", "auto", None))
        db._conn.commit()
    assert len(db.activities_range(NIGHT - 3600, NIGHT + 86400)) == 5
    db.close()

    reopened = Database(path)                        # migration runs on open
    rows = [a for a in reopened.activities_range(NIGHT - 3600, NIGHT + 86400)
            if not a["deleted"]]
    assert len(rows) == 1
    assert rows[0]["end_unix"] == NIGHT + 393 * 60
