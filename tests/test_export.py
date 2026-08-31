"""CSV export.

The properties that matter are about shape and honesty: the first column is
always the time, every other column is one metric, a missing measurement is
blank rather than zero, and typed text survives a trip through a spreadsheet
format that treats commas and newlines as structure.
"""
import csv
import io
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from server.app import export
from server.app.store import UserStore

BASE = 1_700_000_000          # a Tuesday, 22:13 UTC


@pytest.fixture
def store(tmp_path):
    s = UserStore({"id": 1, "timezone": "America/Chicago", "max_hr": 190,
                   "sleep_need_h": 8.0}, tmp_path / "data-1.db")
    yield s
    s.close()


def seed(store, day, *, hr=72, minutes=240, hard=0):
    """A block of readings inside one local day, optionally with a hard hour.

    Resting heart rate is estimated from the data itself, so a flat series
    scores no strain at all -- the readings have to actually vary.
    """
    lo, _ = store.bounds(day)
    rows = []
    for m in range(minutes + hard):
        beat = 160 if m >= minutes else hr
        rows.append({"record_id": f"r{lo}-{m}", "packet": "HISTORICAL_DATA",
                     "version": 24, "unix": lo + 3600 + m * 60,
                     "heart_rate": beat,
                     "rr_intervals_ms": [int(60000 / beat)] * 3,
                     "gravity_x": 0.4 if beat > 100 else 0.1, "gravity_y": 0.0,
                     "gravity_z": 0.98, "skin_contact": 1,
                     "spo2_red": 3000, "skin_temp_raw": 2100,
                     "resp_rate_raw": 1500, "signal_quality": 90})
    store.db.insert_records(rows)
    store.invalidate_all()
    return rows


def read(rows):
    return list(csv.reader(io.StringIO("".join(rows))))


# --- shape ------------------------------------------------------------------
def test_the_first_column_is_the_time_and_the_rest_are_metrics(store):
    day = store.today()
    seed(store, day)
    table = read(export.daily_rows(store, day - timedelta(days=2), day))
    assert table[0][0] == "Date"
    assert "Recovery %" in table[0] and "Strain (0-21)" in table[0]
    # One row per day in the range, oldest first, none skipped.
    assert [r[0] for r in table[1:]] == [
        store.clock.as_date(day - timedelta(days=i)).isoformat() for i in (2, 1, 0)]


def test_every_row_has_exactly_as_many_cells_as_there_are_headers(store):
    """A ragged CSV silently shifts every value one column left in a spreadsheet."""
    day = store.today()
    seed(store, day)
    store.db.put_journal(store.clock.as_date(day).isoformat(),
                         ["alcohol", "late meal"], {"alcohol_units": 2}, "felt rough")
    for name in export.DATASETS:
        table = read(export.DATASETS[name](store, day - timedelta(days=3), day))
        widths = {len(r) for r in table}
        assert len(widths) == 1, f"{name} produced ragged rows: {sorted(widths)}"


def test_minute_rows_start_with_a_local_timestamp(store):
    day = store.today()
    seed(store, day)
    table = read(export.minute_rows(store, day, day))
    assert table[0][0] == "Time"
    assert len(table) > 1
    first = table[1][0]
    assert len(first) == 19 and first[4] == "-" and first[10] == " "
    # In the account's zone, not UTC: the reading was seeded an hour into the
    # local day, so that is the hour it must come back as.
    assert first.endswith("01:00:00")
    assert [r[0] for r in table[1:]] == sorted(r[0] for r in table[1:])


# --- honesty ----------------------------------------------------------------
def test_a_day_with_no_data_is_blank_not_zero(store):
    """A zero recovery and a day on the charger must not average together."""
    day = store.today()
    seed(store, day)
    table = read(export.daily_rows(store, day - timedelta(days=1), day))
    header, empty = table[0], table[1]
    assert empty[0] == store.clock.as_date(day - timedelta(days=1)).isoformat()
    assert set(empty[1:]) == {""}
    assert header.index("Recovery %") > 0


def test_a_measured_day_carries_numbers(store):
    day = store.today()
    seed(store, day, hr=60, minutes=540, hard=60)
    table = read(export.daily_rows(store, day, day))
    row = dict(zip(table[0], table[1]))
    assert float(row["Average HR (bpm)"]) == pytest.approx(70, abs=3)
    assert float(row["Max HR (bpm)"]) == 160
    assert float(row["Strain (0-21)"]) > 0
    assert row["Skin temp (raw)"] == "2100.0"


def test_raw_channels_say_so_in_the_header(store):
    """The app refuses to print these with units; the export must not either."""
    heads = [h for h, _ in export.DAILY]
    for name in ("Respiration", "SpO2 red", "SpO2 infrared", "Skin temp"):
        assert any(h.startswith(name) and h.endswith("(raw)") for h in heads)


# --- text that would otherwise break the file -------------------------------
def test_notes_with_commas_quotes_and_newlines_survive(store):
    day = store.today()
    key = store.clock.as_date(day).isoformat()
    messy = 'ate late, drank "two" beers\nslept badly'
    store.db.put_journal(key, ["beer, wine"], {"alcohol_units": 2.5}, messy)
    table = read(export.journal_rows(store, day, day))
    row = dict(zip(table[0], table[1]))
    assert row["Tags"] == "beer, wine"
    # The newline is flattened on purpose: a hard break inside a cell reads as
    # a broken file to most people opening it, and the words are all still there.
    assert row["Notes"] == 'ate late, drank "two" beers slept badly'
    assert row["Alcohol units"] == "2.50"


def test_journal_columns_cover_every_amount_anyone_recorded(store):
    day = store.today()
    for i, (k, v) in enumerate([("alcohol_units", 2), ("caffeine_mg", 200)]):
        store.db.put_journal(store.clock.as_date(day - timedelta(days=i)).isoformat(),
                             [], {k: v}, "")
    table = read(export.journal_rows(store, day - timedelta(days=1), day))
    assert "Alcohol units" in table[0] and "Caffeine mg" in table[0]
    # A day that has no value for a column gets a blank, not another day's value.
    rows = {r[0]: dict(zip(table[0], r)) for r in table[1:]}
    younger = store.clock.as_date(day).isoformat()
    assert rows[younger]["Caffeine mg"] == ""


# --- number formatting ------------------------------------------------------
@pytest.mark.parametrize("value,digits,want", [
    (None, 1, ""), (float("nan"), 1, ""), (float("inf"), 1, ""),
    ("not a number", 1, ""), (True, 0, ""), (72, 0, "72"), (71.6, 0, "72"),
    (48.25, 1, "48.2"), (0, 0, "0"),
])
def test_numbers_that_cannot_be_written_come_out_blank(value, digits, want):
    assert export._num(value, digits) == want


def test_a_zero_is_written_not_dropped():
    """Blank means 'not measured'. Zero is a measurement and must survive."""
    assert export._num(0, 0) == "0" and export._num(0.0, 1) == "0.0"
