"""Device status: the three states, and the staleness rule that separates them."""
from datetime import datetime, timedelta, timezone

from server.app.db import Database
from server.app.device import STALE_AFTER_SECONDS, describe


def fresh(**kw):
    return {"received_at": datetime.now(timezone.utc).isoformat(), **kw}


def aged(seconds, **kw):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return {"received_at": stamp.isoformat(), **kw}


def test_no_heartbeat_is_its_own_state():
    d = describe(None)
    assert d["state"] == "unknown" and d["tone"] == "muted"
    assert d["connected"] is False and d["battery_pct"] is None


def test_connected():
    d = describe(fresh(connected=True, battery_pct=87.3, charging=False))
    assert d["state"] == "connected" and d["tone"] == "good"
    assert d["battery_pct"] == 87.3 and d["charging"] is False


def test_bridge_running_but_strap_absent():
    d = describe(fresh(connected=False, battery_pct=64.0))
    assert d["state"] == "searching" and d["tone"] == "warn"
    # It must still show the last known charge rather than blanking it.
    assert d["battery_pct"] == 64.0


def test_stale_heartbeat_overrides_a_stale_connected_claim():
    # The key rule: an old heartbeat saying "connected" means the laptop died
    # mid-session, not that the strap is still live.
    d = describe(aged(STALE_AFTER_SECONDS + 60, connected=True, battery_pct=64.0))
    assert d["state"] == "offline" and d["tone"] == "bad"
    assert d["connected"] is True     # raw claim preserved...
    assert d["state"] != "connected"  # ...but the state does not believe it


def test_just_inside_the_stale_window_is_still_live():
    d = describe(aged(STALE_AFTER_SECONDS - 30, connected=True))
    assert d["state"] == "connected"


def test_queued_backlog_is_surfaced():
    d = describe(fresh(connected=True, queued=1500))
    assert "1,500" in d["detail"]


def test_unparseable_timestamp_is_treated_as_offline():
    assert describe({"received_at": "not-a-date", "connected": True})["state"] == "offline"


def test_offline_detail_explains_the_strap_keeps_recording():
    d = describe(aged(9 * 3600, connected=True))
    assert "keeps recording" in d["detail"]
    assert "hours" in d["detail"]


def test_status_survives_a_restart(tmp_path):
    path = tmp_path / "s.db"
    db = Database(path)
    db.put_device_status({"connected": True, "battery_pct": 55.0})
    db.close()
    db2 = Database(path)
    got = db2.get_device_status()
    assert got["battery_pct"] == 55.0 and "received_at" in got
    db2.close()


def test_status_keeps_only_the_latest(tmp_path):
    db = Database(tmp_path / "s.db")
    db.put_device_status({"battery_pct": 90.0})
    db.put_device_status({"battery_pct": 12.0})
    assert db.get_device_status()["battery_pct"] == 12.0
    db.close()


def test_missing_status_is_none(tmp_path):
    db = Database(tmp_path / "s.db")
    assert db.get_device_status() is None
    db.close()
