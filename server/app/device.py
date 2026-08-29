"""Turn the bridge's last heartbeat into a state the dashboard can show.

Three states, not two, because "the strap is not connected" and "the laptop
is not running" are different problems with different fixes, and both look
identical if you only track whether data is arriving.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# A heartbeat every 30s by default, so allow a couple of misses before calling
# the bridge itself offline.
STALE_AFTER_SECONDS = 150


def _age(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def describe(status: dict[str, Any] | None) -> dict[str, Any]:
    """Fold the raw heartbeat into a state, a colour and a sentence."""
    if not status:
        return {
            "state": "unknown", "tone": "muted",
            "label": "No bridge yet",
            "detail": ("The laptop bridge has never reported in. Once it is running "
                       "and pointed at this server, its status appears here."),
            "battery_pct": None, "charging": None, "connected": False,
            "heartbeat_age_s": None,
        }

    age = _age(status.get("received_at"))
    battery = status.get("battery_pct")
    charging = status.get("charging")
    queued = status.get("queued")

    if age is None or age > STALE_AFTER_SECONDS:
        state, tone = "offline", "bad"
        label = "Bridge offline"
        detail = ("The laptop has not checked in"
                  + (f" for {_human(age)}" if age else "")
                  + ". It is probably asleep or shut down. The strap keeps recording "
                    "on its own and the backlog syncs when the laptop is back.")
    elif status.get("connected"):
        state, tone = "connected", "good"
        label = "Connected"
        detail = "The laptop is connected to your strap and receiving data."
        if queued:
            detail += f" {queued:,} record(s) still waiting to upload."
    else:
        state, tone = "searching", "warn"
        label = "Strap not connected"
        detail = ("The laptop bridge is running but cannot see the strap — out of "
                  "range, on the charger, or claimed by the phone app.")

    return {
        "state": state, "tone": tone, "label": label, "detail": detail,
        "battery_pct": battery,
        "charging": bool(charging) if charging is not None else None,
        "connected": bool(status.get("connected")),
        "on_wrist": status.get("on_wrist"),
        "queued": queued,
        "heartbeat_age_s": round(age) if age is not None else None,
        "last_packet_at": status.get("last_packet_at"),
        "battery_mv": status.get("battery_mv"),
    }


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours"
    return f"{round(seconds / 86400)} days"
