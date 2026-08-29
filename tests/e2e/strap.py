"""A synthetic WHOOP 4.0 that emits real, CRC-valid frames.

The point of building frames rather than dicts is that the whole laptop path
gets exercised for real: protocol.parse_frame, the CRC check, decode()'s field
offsets, the content-addressed record_id, and the spool. If any of those is
wrong the test fails, which a hand-written dict would hide.
"""
from __future__ import annotations

import math
import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from whoop_bridge.protocol import PacketType, SOF, crc8, crc32   # noqa: E402


def _frame(packet_type: int, seq: int, inner_body: bytes) -> bytes:
    """Wrap a body in the real frame: SOF, length, crc8, then trailing crc32."""
    inner = bytes([packet_type, seq & 0xFF]) + inner_body
    length = len(inner) + 4
    head = struct.pack("<BH", SOF, length) + bytes([crc8(struct.pack("<H", length))])
    return head + inner + struct.pack("<I", crc32(inner))


def historical(unix: int, hr: int, rr_ms: list[int], *, sleepy: bool = False,
               temp_c: float = 33.4, resp: float = 14.2, spo2: int = 9000,
               rng: random.Random | None = None) -> bytes:
    """A HISTORICAL_DATA v24 record, laid out at the documented offsets.

    Offsets in decode.V24_FIELDS are frame-absolute, and the frame body starts
    at offset 6, so the body is built at absolute positions and sliced.
    """
    rng = rng or random.Random(0)
    buf = bytearray(96)
    struct.pack_into("<I", buf, 11, unix)
    buf[21] = min(hr, 255)
    buf[22] = min(len(rr_ms), 8)
    for i, v in enumerate(rr_ms[:8]):
        struct.pack_into("<H", buf, 23 + i * 2, min(int(v), 65535))
    struct.pack_into("<H", buf, 33, 12000 + rng.randint(-400, 400))   # ppg_green
    struct.pack_into("<H", buf, 35, 9000 + rng.randint(-300, 300))    # ppg_red_ir
    gx, gy, gz = ((0.02, 0.95, 0.05) if sleepy
                  else (rng.gauss(0, .3), rng.gauss(.7, .25), rng.gauss(0, .3)))
    struct.pack_into("<fff", buf, 40, gx, gy, gz)
    buf[55] = 1                                                       # skin_contact
    struct.pack_into("<fff", buf, 56, gx, gy, gz)
    struct.pack_into("<H", buf, 68, spo2)                             # spo2_red
    struct.pack_into("<H", buf, 70, spo2 + 2000)                      # spo2_ir
    struct.pack_into("<H", buf, 72, int(temp_c * 100))                # skin_temp_raw
    struct.pack_into("<H", buf, 74, 5 if sleepy else 300)             # ambient_light
    struct.pack_into("<H", buf, 80, int(resp * 10))                   # resp_rate_raw
    struct.pack_into("<H", buf, 82, 90)                               # signal_quality
    return _frame(PacketType.HISTORICAL_DATA, 24, bytes(buf[6:]))


def battery_event(pct: float, mv: int = 4050, charging: bool = False) -> bytes:
    buf = bytearray(30)
    buf[6] = 3                                    # BATTERY_LEVEL
    struct.pack_into("<H", buf, 17, int(pct * 10))
    struct.pack_into("<H", buf, 21, mv)
    buf[26] = 1 if charging else 0
    return _frame(PacketType.EVENT, 0, bytes(buf[6:]))


def wrist_event(on: bool) -> bytes:
    buf = bytearray(12)
    buf[6] = 9 if on else 10
    return _frame(PacketType.EVENT, 0, bytes(buf[6:]))


def day_of_frames(start_unix: int, minutes: int, *, seed: int = 3,
                  sleep_from: int = 0, sleep_to: int = 420,
                  workout: tuple[int, int] | None = None) -> list[bytes]:
    """A day's worth of one-minute records, with a night and maybe a workout."""
    rng = random.Random(seed)
    out = []
    for m in range(minutes):
        asleep = sleep_from <= m < sleep_to
        if workout and workout[0] <= m < workout[1]:
            frac = (m - workout[0]) / max(1, workout[1] - workout[0])
            hr = int(125 + 35 * math.sin(math.pi * frac) + rng.gauss(0, 3))
            spread = 12
        elif asleep:
            hr = int(52 + rng.gauss(0, 2) - 3 * math.cos(m / 90))
            spread = 45
        else:
            hr = int(72 + 10 * math.sin(m / 120) + rng.gauss(0, 4))
            spread = 22
        mean = 60000 / max(hr, 1)
        rr = [int(max(300, rng.gauss(mean, spread))) for _ in range(4)]
        out.append(historical(start_unix + m * 60, hr, rr, sleepy=asleep, rng=rng))
    return out
