"""Local days.

Everything a person reads is a *day*: today's recovery, last night's sleep,
the day a journal entry belongs to. A day is a local thing, so the boundary
has to be local too — the evening of the 30th belongs to the 30th, not to the
31st because UTC has already rolled over.

A real zone is needed, not a fixed offset. Anywhere that keeps daylight saving
spends half the year at a different offset, so a summer-correct constant
silently moves every day boundary by an hour in the autumn — and it moves the
boundary underneath data already stored against the old one.
"""

from __future__ import annotations

import logging
import os
from datetime import date as _date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("whoop.clock")

UTC = timezone.utc


def load_zone(name: str | None = None, offset_hours: str | None = None) -> tzinfo:
    """The configured zone. TZ_NAME wins; TZ_OFFSET_HOURS is the old way."""
    name = (name if name is not None else os.environ.get("TZ_NAME", "")).strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("unknown TZ_NAME %r; falling back to UTC. Use an IANA "
                        "name such as America/Chicago.", name)
    raw = (offset_hours if offset_hours is not None
           else os.environ.get("TZ_OFFSET_HOURS", "")).strip()
    if raw:
        try:
            hours = float(raw)
        except ValueError:
            log.warning("bad TZ_OFFSET_HOURS %r; falling back to UTC", raw)
        else:
            if hours:
                log.warning("TZ_OFFSET_HOURS is a fixed offset and will be wrong "
                            "for half the year wherever the clocks change. Set "
                            "TZ_NAME to an IANA zone instead.")
            return timezone(timedelta(hours=hours))
    return UTC


class Clock:
    """Converts between instants and local days in one configured zone."""

    def __init__(self, zone: tzinfo | None = None):
        self.zone = zone or load_zone()

    @property
    def name(self) -> str:
        return getattr(self.zone, "key", str(self.zone))

    def now(self) -> datetime:
        return datetime.now(self.zone)

    def today(self) -> _date:
        return self.now().date()

    def local(self, unix: float) -> datetime | None:
        """That instant in this zone, or None if it is not a real instant.

        A misdecoded frame or a strap with a lost clock can carry a timestamp
        outside anything the platform can represent, and fromtimestamp raises
        OverflowError on those -- which, reached from /ingest, is a 500, and a
        5xx is what stalls the bridge's queue for good.
        """
        try:
            return datetime.fromtimestamp(unix, self.zone)
        except (OverflowError, OSError, ValueError):
            return None

    def day_of(self, unix: float) -> str | None:
        """The local calendar day an instant falls on, as YYYY-MM-DD."""
        moment = self.local(unix)
        return moment.strftime("%Y-%m-%d") if moment else None

    def bounds(self, day: datetime | _date | str) -> tuple[int, int]:
        """[start, end) unix seconds for that local day.

        The end is computed from the next calendar day rather than by adding
        86400, so the day the clocks change is 23 or 25 hours long, as it
        actually was, instead of losing or double-counting an hour of records.
        """
        d = self.as_date(day)
        start = datetime(d.year, d.month, d.day, tzinfo=self.zone)
        nxt = d + timedelta(days=1)
        end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=self.zone)
        return int(start.timestamp()), int(end.timestamp())

    def as_date(self, day: datetime | _date | str) -> _date:
        if isinstance(day, str):
            return _date.fromisoformat(day)
        if isinstance(day, datetime):
            # An aware instant is converted into this zone before its calendar
            # day is read; a naive one is already meant as a local day.
            return (day.astimezone(self.zone).date() if day.tzinfo else day.date())
        return day

    def offset_hours(self, at: datetime | None = None) -> float:
        """The zone's offset at a given moment, for anything that needs one."""
        moment = at or self.now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(self.zone).utcoffset().total_seconds() / 3600
