"""Behaviour under live data: partial days, out-of-order arrivals, duplicates,
clock skew, and concurrent read/write."""
import random
import threading
import time

import pytest

from server.app.analytics import MIN_EPOCHS_FOR_RECOVERY, build_epochs, summarise_day
from server.app.db import Database

BASE = 1_700_000_000


def rec(unix, hr=72, sleepy=False, rid=None):
    return {"record_id": rid or f"r{unix}", "packet": "HISTORICAL_DATA", "version": 24,
            "unix": unix, "heart_rate": hr,
            "rr_intervals_ms": [int(60000 / hr)] * 3,
            "gravity_x": 0.005 if sleepy else 0.1, "gravity_y": 0.0,
            "gravity_z": 0.98, "skin_contact": 1}


def day(n, sleepy=False):
    # RR varies a little, as a real heart does; a flat series is not a measurement.
    rng = random.Random(7)
    base_rr = 1200 if sleepy else 830
    return [{"device_unix": BASE + m * 60, "heart_rate": 50 if sleepy else 72,
             "gravity_x": 0.005 if sleepy else 0.1, "gravity_y": 0.0,
             "gravity_z": 0.98, "skin_contact": 1,
             "rr_intervals_ms": [int(rng.gauss(base_rr, 25)) for _ in range(3)]}
            for m in range(n)]


# --- partial days -----------------------------------------------------------
def test_a_barely_started_day_reports_no_recovery():
    # Regression: five minutes of live data produced a confident "0% recovery",
    # which reads as alarming rather than as an incomplete day.
    s = summarise_day(day(5), rhr_baseline=52, hrv_baseline=60)
    assert s["partial"] is True
    assert s["recovery"]["score"] is None
    assert "filling up" in s["recovery"]["note"]


def test_recovery_appears_once_there_is_enough_of_the_day():
    s = summarise_day(day(MIN_EPOCHS_FOR_RECOVERY + 20), rhr_baseline=52, hrv_baseline=60)
    assert s["partial"] is False and s["recovery"]["score"] is not None


def test_a_detected_night_is_enough_on_its_own():
    s = summarise_day(day(300, sleepy=True), rhr_baseline=52, hrv_baseline=60)
    assert s["partial"] is False and s["recovery"]["score"] is not None


def test_an_empty_day_does_not_crash():
    assert summarise_day([])["has_data"] is False


@pytest.mark.parametrize("n", [1, 2, 3, 7, 19, 60])
def test_every_partial_length_produces_a_valid_summary(n):
    # Live data walks through every one of these lengths on the way to a full day.
    s = summarise_day(day(n), rhr_baseline=52, hrv_baseline=60)
    assert s["has_data"] is True
    assert isinstance(s["strain"]["score"], (int, float))
    assert s["hrv"]["rmssd_ms"] is None or s["hrv"]["rmssd_ms"] > 0


# --- arrival order and duplicates -------------------------------------------
def test_out_of_order_arrival_gives_the_same_result(tmp_path):
    forward, backward = day(120), list(reversed(day(120)))
    assert build_epochs(forward) == build_epochs(backward)
    a = summarise_day(forward, rhr_baseline=52)
    b = summarise_day(backward, rhr_baseline=52)
    assert a["heart_rate"] == b["heart_rate"] and a["strain"] == b["strain"]


def test_replayed_records_do_not_double_count(tmp_path):
    db = Database(tmp_path / "t.db")
    batch = [rec(BASE + i * 60) for i in range(50)]
    db.insert_records(batch)
    db.insert_records(batch)                      # the bridge re-sends on retry
    assert db.stats()["records"] == 50
    db.close()


def test_interleaved_old_and_new_records(tmp_path):
    db = Database(tmp_path / "t.db")
    db.insert_records([rec(BASE + 600)])          # newer first
    db.insert_records([rec(BASE)])                # then a backfilled older one
    rows = db.range(BASE - 10, BASE + 10_000)
    assert [r["device_unix"] for r in rows] == [BASE, BASE + 600]
    db.close()


# --- bad clocks -------------------------------------------------------------
def test_implausible_timestamps_do_not_poison_the_summary():
    records = day(150) + [
        {"device_unix": 1, "heart_rate": 70, "gravity_x": 0, "gravity_y": 0,
         "gravity_z": 1, "skin_contact": 1, "rr_intervals_ms": []},
        {"device_unix": 4_000_000_000, "heart_rate": 70, "gravity_x": 0,
         "gravity_y": 0, "gravity_z": 1, "skin_contact": 1, "rr_intervals_ms": []},
    ]
    s = summarise_day(records, rhr_baseline=52)
    assert s["has_data"] and s["heart_rate"]["avg"] is not None


def test_records_with_no_timestamp_are_dropped():
    records = day(60) + [{"heart_rate": 70, "gravity_x": 0, "gravity_y": 0,
                          "gravity_z": 1, "skin_contact": 1, "rr_intervals_ms": []}]
    assert len(build_epochs(records)) == len(build_epochs(day(60)))


def test_a_flat_rr_series_is_not_reported_as_zero_hrv():
    # A stuck sensor gives identical intervals. "0.0 ms HRV" would read as
    # catastrophic rather than as a hardware fault, so it reports nothing.
    flat = [{"device_unix": BASE + m * 60, "heart_rate": 72, "gravity_x": 0.1,
             "gravity_y": 0.0, "gravity_z": 0.98, "skin_contact": 1,
             "rr_intervals_ms": [830, 830, 830]} for m in range(200)]
    assert summarise_day(flat, rhr_baseline=52)["hrv"]["rmssd_ms"] is None


def test_missing_fields_do_not_crash():
    sparse = [{"device_unix": BASE + m * 60} for m in range(150)]
    s = summarise_day(sparse)
    assert s["has_data"] is True


# --- concurrency ------------------------------------------------------------
def test_reads_and_writes_can_run_together(tmp_path):
    db = Database(tmp_path / "t.db")
    db.insert_records([rec(BASE + i * 60) for i in range(200)])
    errors = []
    stop = threading.Event()

    def writer():
        i = 1000
        while not stop.is_set():
            try:
                db.insert_records([rec(BASE + i * 60)])
                i += 1
            except Exception as exc:            # noqa: BLE001 - recording it is the test
                errors.append(f"write: {exc}")

    def reader():
        while not stop.is_set():
            try:
                db.range(BASE, BASE + 10_000_000)
                db.stats()
            except Exception as exc:            # noqa: BLE001
                errors.append(f"read: {exc}")

    threads = [threading.Thread(target=writer), threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    db.close()


# --- records the database cannot store --------------------------------------
# Found by end-to-end testing: one record with a dict where a number belongs
# raised sqlite3.ProgrammingError out of insert_records, so /ingest answered
# 500. The bridge treats 5xx as retryable and only deletes a spooled row after
# a 2xx, so that one record would have stalled the queue permanently and the
# strap would have stopped delivering with no visible error.
def _db(tmp_path):
    from server.app.db import Database
    return Database(tmp_path / "t.db")


def test_an_unbindable_value_does_not_fail_the_batch(tmp_path):
    db = _db(tmp_path)
    batch = [rec(BASE + 60), rec(BASE + 120), rec(BASE + 180)]
    batch[1]["heart_rate"] = {"nested": "object"}      # cannot be bound
    received, inserted = db.insert_records(batch)
    assert received == 3
    assert inserted == 3, "the good rows must still land"
    stored = {r["record_id"] for r in db.range(BASE, BASE + 600)}
    assert stored == {b["record_id"] for b in batch}


def test_an_unbindable_value_is_stored_as_null_not_as_junk(tmp_path):
    db = _db(tmp_path)
    r = rec(BASE + 60)
    r["heart_rate"] = ["not", "a", "number"]
    db.insert_records([r])
    row = db.range(BASE, BASE + 600)[0]
    assert row["heart_rate"] is None


def test_every_column_survives_a_wrong_type(tmp_path):
    """Whatever shape arrives, the batch is accepted and the row is stored."""
    db = _db(tmp_path)
    junk = {"a": 1}
    for i, field in enumerate(["received_at", "unix", "packet", "version",
                               "heart_rate", "gravity_x", "skin_contact",
                               "ppg_green", "spo2_red", "skin_temp_raw",
                               "resp_rate_raw", "signal_quality", "raw_hex"]):
        r = rec(BASE + 1000 + i * 60)
        r[field] = junk
        received, inserted = db.insert_records([r])
        assert (received, inserted) == (1, 1), f"{field} broke the insert"


def test_rr_intervals_of_the_wrong_shape_do_not_break_the_row(tmp_path):
    db = _db(tmp_path)
    for i, bad in enumerate(["830", {"rr": 830}, 830, None]):
        r = rec(BASE + 2000 + i * 60)
        r["rr_intervals_ms"] = bad
        assert db.insert_records([r]) == (1, 1)
    for row in db.range(BASE + 2000, BASE + 2600):
        assert row["rr_intervals_ms"] == []


def test_an_integer_too_big_for_sqlite_does_not_fail_the_batch(tmp_path):
    """OverflowError is not an sqlite3 error, so it is easy to leave uncaught.

    A misdecoded frame can produce an integer outside 64 bits, and that would
    have escaped the sqlite3-only exception handling as another 500.
    """
    db = _db(tmp_path)
    for i, value in enumerate([2 ** 100, -(2 ** 100), 10 ** 40]):
        r = rec(BASE + 3000 + i * 60)
        r["unix"] = value
        assert db.insert_records([r]) == (1, 1)
        r2 = rec(BASE + 4000 + i * 60)
        r2["heart_rate"] = value
        assert db.insert_records([r2]) == (1, 1)


def test_the_largest_integer_sqlite_can_hold_is_still_stored(tmp_path):
    db = _db(tmp_path)
    r = rec(BASE + 60)
    r["heart_rate"] = 2 ** 63 - 1
    db.insert_records([r])
    assert db.range(BASE, BASE + 600)[0]["heart_rate"] == 2 ** 63 - 1


def test_a_record_with_a_non_string_id_is_skipped_not_fatal(tmp_path):
    db = _db(tmp_path)
    received, inserted = db.insert_records([
        {"record_id": {"a": 1}, "unix": BASE},
        {"record_id": ["x"], "unix": BASE},
        rec(BASE + 60),
    ])
    assert (received, inserted) == (3, 1)


def test_a_booleans_stored_as_integers(tmp_path):
    db = _db(tmp_path)
    r = rec(BASE + 60)
    r["skin_contact"] = True
    db.insert_records([r])
    assert db.range(BASE, BASE + 600)[0]["skin_contact"] == 1


def test_an_event_with_a_wrong_shaped_field_is_still_accepted(tmp_path):
    db = _db(tmp_path)
    received, inserted = db.insert_records([
        {"record_id": "e1", "packet": "EVENT", "event": {"n": 3},
         "event_time": ["now"], "received_at": None},
    ])
    assert (received, inserted) == (1, 1)
