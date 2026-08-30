"""One person's data: their database, their caches, their clock, their limits.

Each account gets its own SQLite file. Everything that used to be a module
global here belongs to a person instead, which is what makes the isolation
structural rather than a WHERE clause everyone has to remember — and what lets
two people on one server keep different timezones and different maximum heart
rates without either of them affecting the other's numbers.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .analytics import summarise_day
from .clock import Clock, load_zone
from .db import Database
from .stress import raw_series
from .analytics import build_epochs

# Day summaries are pure functions of the stored records, so they are cached.
#
# Invalidation is scoped to the dates the incoming records actually touch. It
# used to clear everything on any ingest, which is fine for a nightly backfill
# and catastrophic for a live strap: with records arriving several times a
# second the cache never survived, every read recomputed fourteen days and
# their thirty-day baselines, and /api/summary went from 10 ms to 27 s.
#
# A short coalescing window on top means a date is recomputed at most once
# every few seconds however fast records arrive. Three seconds of staleness is
# invisible on a dashboard; the recompute storm was not.
CACHE_LIMIT = 2048
MIN_RECOMPUTE_SECONDS = 3.0

SENSOR_FIELDS = ("skin_temp_raw", "resp_rate_raw", "spo2_red", "spo2_ir")

DEFAULT_MAX_HR = 190.0
DEFAULT_SLEEP_NEED_H = 8.0
DEFAULT_BASELINE_DAYS = 30
STRESS_BASELINE_DAYS = 14
WARM_DAYS = 60

_cache_lock = threading.Lock()


class DayCache:
    """Per-date memo with lazy invalidation.

    Ingest marks dates stale rather than clearing them, and a stale entry is
    still served until MIN_RECOMPUTE_SECONDS have passed, so a burst of live
    records cannot make every concurrent reader recompute the same day.

    Each cache owns its own stale set. Sharing one would let whichever cache
    recomputed first clear the flag for all the others, leaving them serving
    data they had already been told was out of date.
    """

    def __init__(self) -> None:
        self.data: dict[str, tuple[float, dict[str, Any]]] = {}
        self.dirty: set[str] = set()

    def get(self, key: str) -> dict[str, Any] | None:
        with _cache_lock:
            entry = self.data.get(key)
            if entry is None:
                return None
            computed_at, value = entry
            if key in self.dirty and (time.monotonic() - computed_at) >= MIN_RECOMPUTE_SECONDS:
                return None
            return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        with _cache_lock:
            self.data[key] = (time.monotonic(), value)
            self.dirty.discard(key)
            while len(self.data) > CACHE_LIMIT:
                self.data.pop(next(iter(self.data)))

    def mark(self, dates: set[str], onwards: bool) -> None:
        with _cache_lock:
            self.dirty.update(dates)
            if onwards:
                # A day's final summary depends on later days' baselines, so
                # everything cached from the earliest touched date onward is
                # stale too.
                earliest = min(dates)
                self.dirty.update(k for k in self.data if k >= earliest)

    def clear(self) -> None:
        with _cache_lock:
            self.data.clear()
            self.dirty.clear()


def _median(values: list[float]) -> float | None:
    return round(sorted(values)[len(values) // 2], 1) if values else None


class UserStore:
    """Everything scoped to one account."""

    def __init__(self, user: dict[str, Any], path: Path, *,
                 fallback_zone: str = "", fallback_max_hr: float | None = None,
                 fallback_sleep_need: float | None = None):
        self.user_id = int(user["id"])
        self.db = Database(path)
        self.raw_cache = DayCache()
        self.final_cache = DayCache()
        # One past day's unscaled stress series. Rebuilding all fourteen per
        # request cost 1.7s under live ingest, and a finished day's series
        # never changes, so it is memoised under the same rules.
        self.stress_cache = DayCache()
        self.caches = (self.raw_cache, self.final_cache, self.stress_cache)
        self._warmed = False
        self.apply(user, fallback_zone=fallback_zone, fallback_max_hr=fallback_max_hr,
                   fallback_sleep_need=fallback_sleep_need)

    # --- profile -----------------------------------------------------------
    def apply(self, user: dict[str, Any], *, fallback_zone: str = "",
              fallback_max_hr: float | None = None,
              fallback_sleep_need: float | None = None) -> None:
        """Adopt the account's settings. Called again whenever they change.

        A changed timezone or maximum heart rate moves every derived number,
        so the caches go with them rather than serving figures computed under
        the old settings.
        """
        zone_name = (user.get("timezone") or fallback_zone or "").strip()
        max_hr = user.get("max_hr") or fallback_max_hr or DEFAULT_MAX_HR
        sleep_need = user.get("sleep_need_h") or fallback_sleep_need or DEFAULT_SLEEP_NEED_H
        changed = (getattr(self, "zone_name", None) != zone_name
                   or getattr(self, "max_hr", None) != float(max_hr)
                   or getattr(self, "sleep_need_h", None) != float(sleep_need))
        self.zone_name = zone_name
        self.clock = Clock(load_zone(zone_name, "") if zone_name else load_zone())
        self.max_hr = float(max_hr)
        self.sleep_need_h = float(sleep_need)
        self.baseline_days = DEFAULT_BASELINE_DAYS
        self.age = user.get("age")
        self.sex = (user.get("sex") or "male").strip().lower() or "male"
        self.display_name = user.get("display_name") or ""
        if changed:
            self.invalidate_all()

    # --- days --------------------------------------------------------------
    def today(self) -> datetime:
        return self.clock.now()

    def bounds(self, day: datetime) -> tuple[int, int]:
        return self.clock.bounds(day)

    def touched_dates(self, records: list[dict[str, Any]]) -> set[str]:
        out: set[str] = set()
        for r in records:
            unix = r.get("unix")
            if isinstance(unix, (int, float)):
                # None for a timestamp that is not a real instant; those
                # records are stored but belong to no day, so nothing expires.
                day = self.clock.day_of(unix)
                if day:
                    out.add(day)
        return out

    def invalidate(self, dates: set[str]) -> None:
        if not dates:
            return
        for cache in self.caches:
            cache.mark(dates, onwards=cache is not self.raw_cache)

    def invalidate_all(self) -> None:
        for cache in self.caches:
            cache.clear()

    # --- summaries ---------------------------------------------------------
    def raw_summary(self, day: datetime) -> dict[str, Any]:
        """Day summary without baselines. Used to build the baseline series."""
        key = self.clock.as_date(day).isoformat()
        cached = self.raw_cache.get(key)
        if cached is None:
            lo, hi = self.bounds(day)
            cached = summarise_day(self.db.range(lo, hi), max_hr=self.max_hr,
                                   sleep_need_h=self.sleep_need_h)
            self.raw_cache.put(key, cached)
        return cached

    def baselines(self, before: datetime, days: int) -> tuple[float | None, float | None]:
        """Rolling personal baselines: median HRV and resting HR over `days`."""
        hrvs, rhrs = [], []
        for i in range(1, days + 1):
            s = self.raw_summary(before - timedelta(days=i))
            if not s.get("has_data"):
                continue
            if s["hrv"]["rmssd_ms"]:
                hrvs.append(s["hrv"]["rmssd_ms"])
            if s["heart_rate"]["resting"]:
                rhrs.append(s["heart_rate"]["resting"])
        return _median(hrvs), _median(rhrs)

    def sensor_baselines(self, before: datetime, days: int) -> dict[str, float | None]:
        """Median of each raw sensor channel. These have no real-world unit, so
        a comparison against your own history is the only reading that means
        anything."""
        collected: dict[str, list[float]] = {f: [] for f in SENSOR_FIELDS}
        for i in range(1, days + 1):
            s = self.raw_summary(before - timedelta(days=i))
            if not s.get("has_data"):
                continue
            for field in SENSOR_FIELDS:
                value = (s.get("sensors") or {}).get(field)
                if isinstance(value, (int, float)):
                    collected[field].append(float(value))
        return {f: _median(v) for f, v in collected.items()}

    def summarise(self, day: datetime) -> dict[str, Any]:
        key = self.clock.as_date(day).isoformat()
        cached = self.final_cache.get(key)
        if cached is not None:
            return cached
        lo, hi = self.bounds(day)
        hrv_base, rhr_base = self.baselines(day, self.baseline_days)
        s = summarise_day(self.db.range(lo, hi), max_hr=self.max_hr,
                          sleep_need_h=self.sleep_need_h,
                          hrv_baseline=hrv_base, rhr_baseline=rhr_base)
        s["date"] = key
        s["baselines"] = {"hrv_rmssd_ms": hrv_base, "resting_hr": rhr_base,
                          "window_days": self.baseline_days,
                          "sensors": self.sensor_baselines(day, self.baseline_days)}
        self.final_cache.put(key, s)
        return s

    def series(self, day: datetime, days: int, *path: str) -> list[float]:
        """One metric across the baseline window, most recent last."""
        out: list[float] = []
        for i in range(days, 0, -1):
            s = self.raw_summary(day - timedelta(days=i))
            if not s.get("has_data"):
                continue
            node: Any = s
            for key in path:
                node = (node or {}).get(key) if isinstance(node, dict) else None
            if isinstance(node, (int, float)):
                out.append(float(node))
        return out

    def stress_reference_day(self, past: datetime, hrv_base: float | None) -> list[float]:
        """One past day's unscaled stress values, memoised.

        The key is the date alone, because that is what ingest marks stale.
        The HRV baseline is stored alongside and checked on the way out: it
        shifts as new days land, and reusing a series built against an old one
        would silently mix two scales.
        """
        key = self.clock.as_date(past).isoformat()
        base = round(hrv_base, 1) if hrv_base else None
        cached = self.stress_cache.get(key)
        if cached is not None and cached["hrv_base"] == base:
            return cached["values"]

        summary = self.raw_summary(past)
        values: list[float] = []
        rest = (summary.get("heart_rate") or {}).get("resting")
        if summary.get("has_data") and rest is not None:
            lo, hi = self.bounds(past)
            values = [v for _, v in raw_series(build_epochs(self.db.range(lo, hi)),
                                               rest, self.max_hr, hrv_base)]
        self.stress_cache.put(key, {"hrv_base": base, "values": values})
        return values

    # --- startup -----------------------------------------------------------
    def warm(self) -> None:
        """Rebuild the recent-day memo. Best effort; never fatal.

        Cold, one request has to summarise up to sixty days before it can
        answer, and several arriving together each redo that work.
        """
        if self._warmed:
            return
        self._warmed = True
        try:
            today = self.today()
            for i in range(WARM_DAYS + 1):
                self.raw_summary(today - timedelta(days=i))
            summary = self.summarise(today)
            hrv_base = (summary.get("baselines") or {}).get("hrv_rmssd_ms")
            for i in range(1, STRESS_BASELINE_DAYS + 1):
                self.stress_reference_day(today - timedelta(days=i), hrv_base)
        except Exception:                      # noqa: BLE001 - warm-up only
            pass

    def close(self) -> None:
        self.db.close()


class StoreRegistry:
    """One store per account, made on first use."""

    def __init__(self, data_dir: Path, **fallbacks: Any):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fallbacks = fallbacks
        self._stores: dict[int, UserStore] = {}
        self._lock = threading.Lock()

    def path_for(self, user_id: int) -> Path:
        return self.data_dir / f"data-{int(user_id)}.db"

    def for_user(self, user: dict[str, Any]) -> UserStore:
        user_id = int(user["id"])
        with self._lock:
            store = self._stores.get(user_id)
            if store is None:
                store = UserStore(user, self.path_for(user_id), **self.fallbacks)
                self._stores[user_id] = store
            else:
                store.apply(user, **self.fallbacks)
        return store

    def existing(self) -> list[UserStore]:
        with self._lock:
            return list(self._stores.values())

    def forget(self, user_id: int) -> None:
        with self._lock:
            store = self._stores.pop(int(user_id), None)
        if store:
            store.close()
