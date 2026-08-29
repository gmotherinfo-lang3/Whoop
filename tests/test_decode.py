"""Decoder tests built on synthetic frames with known field values."""
import struct
import pytest
from whoop_bridge.protocol import SOF, crc8, crc32, parse_frame
from whoop_bridge.decode import decode


def make_frame(packet_type: int, seq: int, fields, size: int = 96) -> bytes:
    """Build a frame with `fields` = [(frame_offset, struct_fmt, value)]."""
    inner = bytearray(bytes([packet_type, seq]) + b"\x00" * (size - 2))
    for off, fmt, val in fields:
        struct.pack_into(fmt, inner, off - 4, val)   # inner starts at frame offset 4
    inner = bytes(inner)
    length = len(inner) + 4
    head = struct.pack("<BH", SOF, length)
    head += bytes([crc8(head[1:3])])
    return head + inner + struct.pack("<I", crc32(inner))


def test_historical_v24_full_field_set():
    frame = make_frame(47, 24, [
        (11, "<I", 1735689600), (21, "B", 62), (22, "B", 2),
        (23, "<H", 940), (25, "<H", 955),
        (33, "<H", 1111), (35, "<H", 2222),
        (40, "<f", 0.01), (44, "<f", 0.02), (48, "<f", 0.99),
        (55, "B", 1), (68, "<H", 12345), (70, "<H", 23456),
        (72, "<H", 7000), (74, "<H", 300), (80, "<H", 321), (82, "<H", 88),
    ])
    rec = decode(parse_frame(frame), "data")
    assert rec["packet"] == "HISTORICAL_DATA"
    assert rec["version"] == 24 and rec["decoded"] is True
    assert rec["device_time"] == "2025-01-01T00:00:00+00:00"
    assert rec["heart_rate"] == 62
    assert rec["rr_intervals_ms"] == [940, 955]
    assert rec["ppg_green"] == 1111 and rec["ppg_red_ir"] == 2222
    assert rec["gravity_z"] == pytest.approx(0.99, abs=1e-6)
    assert rec["skin_contact"] == 1
    assert rec["spo2_red"] == 12345 and rec["spo2_ir"] == 23456
    assert rec["skin_temp_raw"] == 7000
    assert rec["ambient_light"] == 300
    assert rec["resp_rate_raw"] == 321
    assert rec["signal_quality"] == 88
    assert "raw_hex" in rec


def test_unknown_historical_version_is_not_misdecoded():
    rec = decode(parse_frame(make_frame(47, 5, [(11, "<I", 1735689600)])), "data")
    assert rec["decoded"] is False
    assert "heart_rate" not in rec       # must not guess with the wrong layout


def test_realtime_packet():
    frame = make_frame(40, 1, [
        (6, "<I", 1735689600), (10, "<H", 512), (12, "B", 71), (13, "B", 1), (14, "<H", 845),
    ])
    rec = decode(parse_frame(frame), "data")
    assert rec["packet"] == "REALTIME_DATA"
    assert rec["heart_rate"] == 71
    assert rec["rr_intervals_ms"] == [845]


def test_implausible_timestamp_is_rejected():
    rec = decode(parse_frame(make_frame(47, 24, [(11, "<I", 42)])), "data")
    assert rec["device_time"] is None    # not a bogus 1970 date


def test_rr_count_cannot_overrun_buffer():
    # A corrupt rr_count must not read past the frame.
    rec = decode(parse_frame(make_frame(47, 24, [(11, "<I", 1735689600), (22, "B", 255)])), "data")
    assert len(rec.get("rr_intervals_ms", [])) < 255


def test_event_and_metadata():
    ev = decode(parse_frame(make_frame(48, 1, [(6, "B", 10), (8, "<I", 1735689600)])), "events")
    assert ev["packet"] == "EVENT" and ev["event"] == 10      # WRIST_OFF
    md = decode(parse_frame(make_frame(49, 1, [(6, "B", 2)])), "cmd")
    assert md["packet"] == "METADATA" and md["meta_type"] == 2  # HISTORY_END


def test_record_id_is_stable_content_hash():
    # Must be identical for identical bytes -- forwarder retries and strap
    # re-offloads both depend on this for de-duplication.
    frame = make_frame(47, 24, [(11, "<I", 1735689600), (21, "B", 62)])
    a = decode(parse_frame(frame), "data")
    b = decode(parse_frame(frame), "data")
    assert a["record_id"] == b["record_id"]
    assert len(a["record_id"]) == 32

    other = make_frame(47, 24, [(11, "<I", 1735689601), (21, "B", 62)])
    assert decode(parse_frame(other), "data")["record_id"] != a["record_id"]
