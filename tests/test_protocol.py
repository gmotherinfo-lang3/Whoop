"""Protocol tests. The CRC-8 cases use headers captured from real traffic."""
import struct
import pytest
from whoop_bridge.protocol import (
    SOF, Cmd, PacketType, build_command, crc8, crc32, parse_frame, set_clock_payload,
)


@pytest.mark.parametrize("length_bytes,expected", [
    (bytes([0x10, 0x00]), 0x57),   # from captured header aa100057
    (bytes([0x08, 0x00]), 0xA8),   # from captured header aa0800a8
])
def test_crc8_against_captured_headers(length_bytes, expected):
    assert crc8(length_bytes) == expected


def test_crc32_is_standard_zlib():
    # Guards against reintroducing the 0xF43F44AC xor-out some write-ups claim.
    assert crc32(b"123456789") == 0xCBF43926


def test_frame_layout():
    frame = build_command(Cmd.GET_HELLO, b"\x00", seq=7)
    assert frame[0] == SOF
    length = struct.unpack_from("<H", frame, 1)[0]
    assert crc8(frame[1:3]) == frame[3]
    assert len(frame) == length + 4          # total = length + CRC-32
    assert frame[4] == PacketType.COMMAND
    assert frame[5] == 7
    assert frame[6] == Cmd.GET_HELLO
    assert crc32(frame[4:length]) == struct.unpack_from("<I", frame, length)[0]


def test_roundtrip():
    frame = build_command(Cmd.TOGGLE_REALTIME_HR, b"\x01", seq=3)
    parsed = parse_frame(frame)
    assert parsed is not None
    assert parsed.packet_type == PacketType.COMMAND
    assert parsed.seq == 3
    assert parsed.payload == bytes([Cmd.TOGGLE_REALTIME_HR, 0x01])
    assert parsed.crc_ok


def test_set_clock_payload_is_eight_bytes():
    # A wrong-length SET_CLOCK is acked but not latched, and the strap then
    # refuses to serve historical data. The length is load-bearing.
    payload = set_clock_payload(1735689600)
    assert len(payload) == 8
    assert struct.unpack("<II", payload) == (1735689600, 0)


def test_parse_rejects_garbage():
    assert parse_frame(b"") is None
    assert parse_frame(b"\x01\x02\x03\x04\x05\x06\x07\x08") is None   # bad SOF
    bad = bytearray(build_command(Cmd.GET_HELLO))
    bad[3] ^= 0xFF                                                    # corrupt CRC-8
    assert parse_frame(bytes(bad)) is None


def test_corrupt_crc32_is_flagged_not_dropped():
    frame = bytearray(build_command(Cmd.GET_HELLO))
    frame[-1] ^= 0xFF
    parsed = parse_frame(bytes(frame))
    assert parsed is not None and not parsed.crc_ok
