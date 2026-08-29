"""The day-summary memo, which is what keeps the dashboard responsive while
records are streaming in. The invariants here are the ones a live strap breaks.
"""
import os
import tempfile

os.environ.setdefault("WHOOP_DB", os.path.join(tempfile.mkdtemp(), "cache-test.db"))

import pytest

from server.app.main import (  # noqa: E402
    MIN_RECOMPUTE_SECONDS, _CACHES, _DayCache, _final_cache, _invalidate,
    _invalidate_cache, _raw_cache, _stress_cache, _touched_dates,
)


@pytest.fixture(autouse=True)
def clean():
    _invalidate_cache()
    yield
    _invalidate_cache()


def test_a_value_survives_a_round_trip():
    c = _DayCache()
    c.put("2026-01-02", {"v": 1})
    assert c.get("2026-01-02") == {"v": 1}
    assert c.get("2026-01-03") is None


def test_a_freshly_marked_entry_is_still_served():
    """Coalescing: a burst of live records must not make every reader recompute."""
    c = _DayCache()
    c.put("2026-01-02", {"v": 1})
    c.mark({"2026-01-02"}, onwards=False)
    assert c.get("2026-01-02") == {"v": 1}


def test_a_stale_entry_is_dropped_once_it_is_worth_redoing():
    c = _DayCache()
    c.put("2026-01-02", {"v": 1})
    # Backdate the entry past the coalescing window.
    stamp, value = c.data["2026-01-02"]
    c.data["2026-01-02"] = (stamp - MIN_RECOMPUTE_SECONDS - 1, value)
    c.mark({"2026-01-02"}, onwards=False)
    assert c.get("2026-01-02") is None


def test_putting_a_value_back_clears_its_stale_mark():
    c = _DayCache()
    c.put("2026-01-02", {"v": 1})
    c.mark({"2026-01-02"}, onwards=False)
    c.put("2026-01-02", {"v": 2})
    assert "2026-01-02" not in c.dirty


def test_onwards_marks_later_days_too():
    """A day's final summary depends on later days' baselines."""
    c = _DayCache()
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        c.put(d, {"v": d})
    c.mark({"2026-01-02"}, onwards=True)
    assert c.dirty == {"2026-01-02", "2026-01-03"}


def test_onwards_off_marks_only_what_was_touched():
    c = _DayCache()
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        c.put(d, {"v": d})
    c.mark({"2026-01-02"}, onwards=False)
    assert c.dirty == {"2026-01-02"}


def test_each_cache_keeps_its_own_stale_set():
    """The bug this guards against: one cache recomputing a date used to clear
    the stale mark for every other cache, which then went on serving data it
    had already been told was out of date."""
    _raw_cache.put("2026-01-02", {"v": "raw"})
    _final_cache.put("2026-01-02", {"v": "final"})
    _stress_cache.put("2026-01-02", {"v": "stress"})

    _invalidate({"2026-01-02"})
    assert all("2026-01-02" in c.dirty for c in _CACHES)

    # One cache recomputes. The others must still know they are stale.
    _raw_cache.put("2026-01-02", {"v": "raw again"})
    assert "2026-01-02" not in _raw_cache.dirty
    assert "2026-01-02" in _final_cache.dirty
    assert "2026-01-02" in _stress_cache.dirty


def test_invalidating_nothing_marks_nothing():
    _final_cache.put("2026-01-02", {"v": 1})
    _invalidate(set())
    assert not _final_cache.dirty


def test_the_cache_is_bounded():
    c = _DayCache()
    from server.app.main import _CACHE_LIMIT
    for i in range(_CACHE_LIMIT + 50):
        c.put(f"key-{i:05d}", {"v": i})
    assert len(c.data) == _CACHE_LIMIT
    # The oldest entries are the ones evicted.
    assert "key-00000" not in c.data
    assert f"key-{_CACHE_LIMIT + 49:05d}" in c.data


def test_touched_dates_uses_the_configured_day_boundary(monkeypatch):
    import server.app.main as main
    # 03:00 UTC is the previous local day at UTC-5.
    at = 1_767_236_400   # 2026-01-01T03:00:00Z
    monkeypatch.setattr(main, "TZ_OFFSET_H", 0.0)
    assert main._touched_dates([{"unix": at}]) == {"2026-01-01"}
    monkeypatch.setattr(main, "TZ_OFFSET_H", -5.0)
    assert main._touched_dates([{"unix": at}]) == {"2025-12-31"}


def test_touched_dates_ignores_records_without_a_timestamp():
    assert _touched_dates([{"heart_rate": 70}, {"unix": None}]) == set()
