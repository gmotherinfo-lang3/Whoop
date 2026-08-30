"""Local days.

The bug these guard against was visible in the app: at 7pm Central the server
had already rolled over to the next UTC day, so an evening's records landed on
tomorrow and the date above the rings disagreed with the list below it.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from server.app.clock import Clock, load_zone

CHICAGO = ZoneInfo("America/Chicago")


@pytest.fixture
def clock():
    return Clock(CHICAGO)


def test_a_local_day_starts_at_local_midnight(clock):
    lo, hi = clock.bounds("2026-08-30")
    assert datetime.fromtimestamp(lo, timezone.utc).isoformat() == "2026-08-30T05:00:00+00:00"
    assert datetime.fromtimestamp(hi, timezone.utc).isoformat() == "2026-08-31T05:00:00+00:00"


def test_an_evening_belongs_to_that_evening_not_to_tomorrow(clock):
    """21:00 on the 30th in Chicago is 02:00 on the 31st in UTC."""
    evening = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc).timestamp()
    assert clock.day_of(evening) == "2026-08-30"


def test_the_small_hours_belong_to_that_night(clock):
    small_hours = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc).timestamp()
    assert clock.day_of(small_hours) == "2026-08-30"     # 03:00 local


def test_a_day_covers_exactly_its_own_records(clock):
    lo, hi = clock.bounds("2026-08-30")
    for hour in range(24):
        local = datetime(2026, 8, 30, hour, 30, tzinfo=CHICAGO).timestamp()
        assert lo <= local < hi, f"{hour}:30 fell outside its own day"


@pytest.mark.parametrize("day,hours", [
    ("2026-03-08", 23),      # clocks forward
    ("2026-11-01", 25),      # clocks back
    ("2026-06-01", 24),
])
def test_the_days_the_clocks_change_are_the_right_length(clock, day, hours):
    """A fixed offset cannot do this, which is why it is not used."""
    lo, hi = clock.bounds(day)
    assert (hi - lo) / 3600 == hours


def test_consecutive_days_meet_exactly(clock):
    """No gap and no overlap, including across a clock change."""
    for a, b in [("2026-03-07", "2026-03-08"), ("2026-03-08", "2026-03-09"),
                 ("2026-10-31", "2026-11-01"), ("2026-11-01", "2026-11-02")]:
        assert clock.bounds(a)[1] == clock.bounds(b)[0]


def test_utc_is_the_default_when_nothing_is_configured():
    assert load_zone("", "") is timezone.utc


def test_an_unknown_zone_name_falls_back_rather_than_crashing():
    assert load_zone("Middle/Earth", "") is timezone.utc


def test_the_old_fixed_offset_setting_still_works():
    zone = load_zone("", "-5")
    assert Clock(zone).bounds("2026-08-30")[0] == \
        int(datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc).timestamp())


def test_a_zone_name_wins_over_an_offset():
    assert Clock(load_zone("America/Chicago", "3")).name == "America/Chicago"


def test_dates_survive_a_round_trip(clock):
    for value in ["2026-01-01", "2026-08-30", "2026-12-31"]:
        lo, _ = clock.bounds(value)
        assert clock.day_of(lo) == value


def test_as_date_accepts_what_the_endpoints_pass(clock):
    assert clock.as_date("2026-08-30") == date(2026, 8, 30)
    assert clock.as_date(date(2026, 8, 30)) == date(2026, 8, 30)
    assert clock.as_date(datetime(2026, 8, 30, 12, tzinfo=CHICAGO)) == date(2026, 8, 30)
    # An aware UTC instant is read in the local zone, not as its UTC date.
    assert clock.as_date(datetime(2026, 8, 31, 2, tzinfo=timezone.utc)) == date(2026, 8, 30)


# --- timestamps that are not instants ---------------------------------------
# Found by the end-to-end run: a record carrying 2**100 as its unix time made
# datetime.fromtimestamp raise OverflowError inside /ingest, which is a 500 --
# and a 5xx is exactly what stalls the bridge's queue permanently.
@pytest.mark.parametrize("unix", [2 ** 100, -(2 ** 100), 1e30, -1e30, float("inf")])
def test_an_impossible_timestamp_is_not_a_day(clock, unix):
    assert clock.day_of(unix) is None
    assert clock.local(unix) is None


@pytest.mark.parametrize("unix", [0, 1, 1_788_066_000, 2 ** 31])
def test_real_timestamps_still_resolve(clock, unix):
    assert clock.day_of(unix) is not None


def test_ingest_does_not_expire_anything_for_an_impossible_timestamp(monkeypatch):
    import server.app.main as main
    monkeypatch.setattr(main, "CLOCK", Clock(CHICAGO))
    touched = main._touched_dates([{"unix": 2 ** 100}, {"unix": 1_788_066_000}])
    assert touched == {"2026-08-30"}
