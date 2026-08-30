"""The day-summary memo, which is what keeps the dashboard responsive while
records are streaming in. The invariants here are the ones a live strap breaks.
"""
import pytest

from server.app.clock import Clock
from server.app.store import CACHE_LIMIT, MIN_RECOMPUTE_SECONDS, DayCache


class _Stub:
    """Just the cache trio and the day mapping, without touching a database."""

    def __init__(self, zone="UTC"):
        from zoneinfo import ZoneInfo
        self.clock = Clock(ZoneInfo(zone))
        self.raw_cache, self.final_cache = DayCache(), DayCache()
        self.stress_cache = DayCache()
        self.caches = (self.raw_cache, self.final_cache, self.stress_cache)

    invalidate = property(lambda self: __import__(
        "server.app.store", fromlist=["UserStore"]).UserStore.invalidate.__get__(self))
    touched_dates = property(lambda self: __import__(
        "server.app.store", fromlist=["UserStore"]).UserStore.touched_dates.__get__(self))


@pytest.fixture
def store():
    return _Stub()


def test_a_value_survives_a_round_trip():
    c = DayCache()
    c.put("2026-01-02", {"v": 1})
    assert c.get("2026-01-02") == {"v": 1}
    assert c.get("2026-01-03") is None


def test_a_freshly_marked_entry_is_still_served():
    """Coalescing: a burst of live records must not make every reader recompute."""
    c = DayCache()
    c.put("2026-01-02", {"v": 1})
    c.mark({"2026-01-02"}, onwards=False)
    assert c.get("2026-01-02") == {"v": 1}


def test_a_stale_entry_is_dropped_once_it_is_worth_redoing():
    c = DayCache()
    c.put("2026-01-02", {"v": 1})
    # Backdate the entry past the coalescing window.
    stamp, value = c.data["2026-01-02"]
    c.data["2026-01-02"] = (stamp - MIN_RECOMPUTE_SECONDS - 1, value)
    c.mark({"2026-01-02"}, onwards=False)
    assert c.get("2026-01-02") is None


def test_putting_a_value_back_clears_its_stale_mark():
    c = DayCache()
    c.put("2026-01-02", {"v": 1})
    c.mark({"2026-01-02"}, onwards=False)
    c.put("2026-01-02", {"v": 2})
    assert "2026-01-02" not in c.dirty


def test_onwards_marks_later_days_too():
    """A day's final summary depends on later days' baselines."""
    c = DayCache()
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        c.put(d, {"v": d})
    c.mark({"2026-01-02"}, onwards=True)
    assert c.dirty == {"2026-01-02", "2026-01-03"}


def test_onwards_off_marks_only_what_was_touched():
    c = DayCache()
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        c.put(d, {"v": d})
    c.mark({"2026-01-02"}, onwards=False)
    assert c.dirty == {"2026-01-02"}


def test_each_cache_keeps_its_own_stale_set(store):
    """The bug this guards against: one cache recomputing a date used to clear
    the stale mark for every other cache, which then went on serving data it
    had already been told was out of date."""
    for cache, value in zip(store.caches, ["raw", "final", "stress"]):
        cache.put("2026-01-02", {"v": value})

    store.invalidate({"2026-01-02"})
    assert all("2026-01-02" in c.dirty for c in store.caches)

    # One cache recomputes. The others must still know they are stale.
    store.raw_cache.put("2026-01-02", {"v": "raw again"})
    assert "2026-01-02" not in store.raw_cache.dirty
    assert "2026-01-02" in store.final_cache.dirty
    assert "2026-01-02" in store.stress_cache.dirty


def test_invalidating_nothing_marks_nothing(store):
    store.final_cache.put("2026-01-02", {"v": 1})
    store.invalidate(set())
    assert not store.final_cache.dirty


def test_the_cache_is_bounded():
    c = DayCache()
    _CACHE_LIMIT = CACHE_LIMIT
    for i in range(_CACHE_LIMIT + 50):
        c.put(f"key-{i:05d}", {"v": i})
    assert len(c.data) == _CACHE_LIMIT
    # The oldest entries are the ones evicted.
    assert "key-00000" not in c.data
    assert f"key-{_CACHE_LIMIT + 49:05d}" in c.data


def test_touched_dates_uses_each_accounts_own_day_boundary():
    """Two people on one server can be in different zones."""
    at = 1_767_236_400   # 2026-01-01T03:00:00Z -- still New Year's Eve in Chicago
    assert _Stub("UTC").touched_dates([{"unix": at}]) == {"2026-01-01"}
    assert _Stub("America/Chicago").touched_dates([{"unix": at}]) == {"2025-12-31"}


def test_touched_dates_ignores_records_without_a_timestamp(store):
    assert store.touched_dates([{"heart_rate": 70}, {"unix": None}]) == set()
