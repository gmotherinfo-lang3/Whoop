"""CSV export: what is on the screen, in a shape a spreadsheet understands.

The layout is fixed on purpose: the first column is always the time the row
describes, and every other column is one metric. That is the orientation
spreadsheets, pandas and every charting tool assume -- one observation per
row, one variable per column -- so an export drops straight into a pivot table
without being transposed first.

Nothing here invents a value. A metric that was not measured is left blank
rather than filled with a zero, because a zero recovery score and a day the
strap was on the charger are not the same thing and averaging them together
is exactly the mistake a blank prevents.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator

# Rows are capped so a mistyped date range cannot ask the server to hold a
# decade of minute readings in memory at once.
MAX_DAYS = 366
MAX_ROWS = 400_000


def _get(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _num(value: Any, digits: int = 1) -> str:
    if value is None or isinstance(value, bool):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if f != f or f in (float("inf"), float("-inf")):
        return ""
    return str(int(round(f))) if digits == 0 else f"{f:.{digits}f}"


# (header, how to read it from a day summary). Headers are the words the app
# itself uses -- someone reading the CSV a year later should not have to map
# "rmssd_ms" back onto the HRV number they remember seeing.
DAILY: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
    ("Recovery %",          lambda s: _num(_get(s, "recovery", "score"), 0)),
    ("Recovery band",       lambda s: str(_get(s, "recovery", "band") or "")),
    ("Strain (0-21)",       lambda s: _num(_get(s, "strain", "score"))),
    ("Sleep (hours)",       lambda s: _num((_get(s, "sleep", "total_minutes") or 0) / 60.0, 2)
                                      if _get(s, "sleep", "total_minutes") else ""),
    ("Sleep performance %", lambda s: _num(_get(s, "sleep", "performance_pct"), 0)),
    ("Resting HR (bpm)",    lambda s: _num(_get(s, "heart_rate", "resting"), 0)),
    ("Average HR (bpm)",    lambda s: _num(_get(s, "heart_rate", "avg"), 0)),
    ("Max HR (bpm)",        lambda s: _num(_get(s, "heart_rate", "max"), 0)),
    ("HRV RMSSD (ms)",      lambda s: _num(_get(s, "hrv", "rmssd_ms"))),
    ("HRV SDNN (ms)",       lambda s: _num(_get(s, "hrv", "sdnn_ms"))),
    # The four raw channels keep "(raw)" in the header for the same reason the
    # app refuses to print them with units: they are ADC counts, meaningful
    # against your own baseline and meaningless as absolute numbers.
    ("Respiration (raw)",   lambda s: _num(_get(s, "sensors", "resp_rate_raw"))),
    ("SpO2 red (raw)",      lambda s: _num(_get(s, "sensors", "spo2_red"))),
    ("SpO2 infrared (raw)", lambda s: _num(_get(s, "sensors", "spo2_ir"))),
    ("Skin temp (raw)",     lambda s: _num(_get(s, "sensors", "skin_temp_raw"))),
    ("Wrist coverage %",    lambda s: _num(_get(s, "wear", "on_wrist_pct"), 0)),
]

MINUTE: list[tuple[str, str, int]] = [
    ("Heart rate (bpm)",    "heart_rate",     0),
    ("SpO2 red (raw)",      "spo2_red",       0),
    ("SpO2 infrared (raw)", "spo2_ir",        0),
    ("Skin temp (raw)",     "skin_temp_raw",  0),
    ("Respiration (raw)",   "resp_rate_raw",  0),
    ("Ambient light (raw)", "ambient_light",  0),
    ("Signal quality",      "signal_quality", 0),
    ("Skin contact",        "skin_contact",   0),
]


def _writer() -> tuple[io.StringIO, Any]:
    buf = io.StringIO()
    return buf, csv.writer(buf, lineterminator="\n")


def _flush(buf: io.StringIO) -> str:
    text = buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    return text


def daily_rows(store: Any, start: datetime, end: datetime) -> Iterator[str]:
    """One row per local day, oldest first, plus whatever you journalled."""
    buf, out = _writer()
    journal = {j["date"]: j for j in store.db.journal_range(
        store.clock.as_date(start).isoformat(), store.clock.as_date(end).isoformat())}
    out.writerow(["Date"] + [h for h, _ in DAILY] +
                 ["Activities", "Journal tags", "Journal amounts", "Journal notes"])
    yield _flush(buf)

    day = start
    while day <= end:
        key = store.clock.as_date(day).isoformat()
        summary = store.summarise(day)
        lo, hi = store.bounds(day)
        acts = store.db.activities_range(lo, hi)   # already excludes deleted
        names = ", ".join(sorted({str(a.get("confirmed_type") or a.get("detected_type") or "")
                                  for a in acts} - {""}))
        j = journal.get(key, {})
        amounts = j.get("amounts") or {}
        out.writerow(
            [key] +
            [fn(summary) if summary.get("has_data") else "" for _, fn in DAILY] +
            [names,
             ", ".join(j.get("tags") or []),
             "; ".join(f"{k}={v}" for k, v in sorted(amounts.items())),
             (j.get("notes") or "").replace("\r\n", " ").replace("\n", " ")])
        yield _flush(buf)
        day += timedelta(days=1)


def minute_rows(store: Any, start: datetime, end: datetime) -> Iterator[str]:
    """Every stored reading in the range, oldest first.

    Written a day at a time. Pulling a whole range in one query would hold the
    entire export in memory before a single byte reaches the browser, which is
    the difference between a large export and a dead server.
    """
    buf, out = _writer()
    out.writerow(["Time"] + [h for h, _, _ in MINUTE])
    yield _flush(buf)

    written = 0
    day = start
    while day <= end and written < MAX_ROWS:
        lo, hi = store.bounds(day)
        for r in sorted(store.db.range(lo, hi), key=lambda x: x.get("device_unix") or 0):
            at = store.clock.local(r.get("device_unix") or 0)
            if at is None:
                continue
            out.writerow([at.strftime("%Y-%m-%d %H:%M:%S")] +
                         [_num(r.get(field), digits) for _, field, digits in MINUTE])
            written += 1
            if written >= MAX_ROWS:
                break
        yield _flush(buf)
        day += timedelta(days=1)


def journal_rows(store: Any, start: datetime, end: datetime) -> Iterator[str]:
    """Only what you typed: the part no sensor produced."""
    buf, out = _writer()
    entries = store.db.journal_range(store.clock.as_date(start).isoformat(),
                                     store.clock.as_date(end).isoformat())
    keys = sorted({k for e in entries for k in (e.get("amounts") or {})})
    out.writerow(["Date", "Tags"] + [k.replace("_", " ").capitalize() for k in keys] + ["Notes"])
    yield _flush(buf)
    for e in entries:
        amounts = e.get("amounts") or {}
        out.writerow([e.get("date", ""), ", ".join(e.get("tags") or [])] +
                     [_num(amounts.get(k), 2) for k in keys] +
                     [(e.get("notes") or "").replace("\r\n", " ").replace("\n", " ")])
        yield _flush(buf)


DATASETS = {"daily": daily_rows, "minutes": minute_rows, "journal": journal_rows}
