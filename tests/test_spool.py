"""Spool tests: the durability guarantee the cloud push depends on."""
from whoop_bridge.spool import Spool


def test_put_peek_ack_cycle(tmp_path):
    s = Spool(tmp_path / "s.db")
    for i in range(5):
        s.put({"received_at": "t", "heart_rate": 60 + i})
    assert s.depth() == 5
    batch = s.peek(3)
    assert [r["heart_rate"] for _, r in batch] == [60, 61, 62]   # FIFO
    s.ack([i for i, _ in batch])
    assert s.depth() == 2
    s.close()


def test_unacked_records_survive_restart(tmp_path):
    path = tmp_path / "s.db"
    s = Spool(path)
    s.put({"received_at": "t", "heart_rate": 99})
    s.close()
    # A crash or reboot must not lose queued data -- this is the whole point.
    s2 = Spool(path)
    assert s2.depth() == 1
    assert s2.peek(1)[0][1]["heart_rate"] == 99
    s2.close()


def test_trim_drops_oldest_first(tmp_path):
    s = Spool(tmp_path / "s.db")
    for i in range(10):
        s.put({"received_at": "t", "n": i})
    assert s.trim(4) == 6
    assert [r["n"] for _, r in s.peek(10)] == [6, 7, 8, 9]
    s.close()


def test_ack_of_empty_list_is_safe(tmp_path):
    s = Spool(tmp_path / "s.db")
    s.put({"received_at": "t"})
    s.ack([])
    assert s.depth() == 1
    s.close()
