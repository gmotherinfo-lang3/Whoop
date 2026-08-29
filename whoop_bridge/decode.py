"""Decode WHOOP 4.0 packets into named records.

Field offsets below are FRAME-ABSOLUTE (measured from the 0xAA start byte),
matching how the protocol is documented in PROTOCOL.md.

Every record keeps `raw_hex`, so if a field mapping is later corrected the
already-forwarded data can be re-decoded rather than re-collected.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone
from typing import Any

from .protocol import Frame, PacketType

# Plausible-unix window used to reject misparsed timestamps.
UNIX_MIN, UNIX_MAX = 1_600_000_000, 1_900_000_000

# HISTORICAL_DATA version 24 -- the WHOOP 4.0 biometric record.
# (name, frame_offset, struct_format)
V24_FIELDS: tuple[tuple[str, int, str], ...] = (
    ("heart_rate",     21, "B"),
    ("ppg_green",      33, "<H"),
    ("ppg_red_ir",     35, "<H"),
    ("gravity_x",      40, "<f"),
    ("gravity_y",      44, "<f"),
    ("gravity_z",      48, "<f"),
    ("skin_contact",   55, "B"),
    ("gravity2_x",     56, "<f"),
    ("gravity2_y",     60, "<f"),
    ("gravity2_z",     64, "<f"),
    ("spo2_red",       68, "<H"),
    ("spo2_ir",        70, "<H"),
    ("skin_temp_raw",  72, "<H"),
    ("ambient_light",  74, "<H"),
    ("led_drive_1",    76, "<H"),
    ("led_drive_2",    78, "<H"),
    ("resp_rate_raw",  80, "<H"),
    ("signal_quality", 82, "<H"),
)

# REALTIME_RAW_DATA IMU variant, keyed by total frame length.
IMU_AXES = (
    ("accel_x", 89, "accel"), ("accel_y", 289, "accel"), ("accel_z", 489, "accel"),
    ("gyro_x", 692, "gyro"), ("gyro_y", 892, "gyro"), ("gyro_z", 1092, "gyro"),
)
IMU_SAMPLES = 100
ACCEL_SCALE = 0.000244140625   # 1/4096 g per LSB
GYRO_SCALE = 0.06103515625     # 2000/32768 deg/s per LSB


def _get(raw: bytes, off: int, fmt: str) -> Any:
    size = struct.calcsize(fmt)
    if off + size > len(raw):
        return None
    return struct.unpack_from(fmt, raw, off)[0]


def _iso(ts: int | None) -> str | None:
    if ts is None or not (UNIX_MIN < ts < UNIX_MAX):
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _rr(raw: bytes, count_off: int, first_off: int) -> list[int]:
    count = _get(raw, count_off, "B") or 0
    out = []
    # Bound the loop by the real buffer so a corrupt count cannot run away.
    for i in range(min(count, max(0, (len(raw) - first_off)) // 2)):
        out.append(struct.unpack_from("<H", raw, first_off + i * 2)[0])
    return out


def decode(frame: Frame, source: str, *, include_imu: bool = False) -> dict[str, Any]:
    """Turn a Frame into a flat, JSON-ready record."""
    raw = frame.raw
    rec: dict[str, Any] = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "packet_type": frame.packet_type,
        "packet": frame.type_name,
        "seq": frame.seq,
        "crc_ok": frame.crc_ok,
    }

    if frame.packet_type == PacketType.HISTORICAL_DATA:
        rec.update(_historical(raw, frame.seq))
    elif frame.packet_type == PacketType.REALTIME_DATA:
        rec.update(_realtime(raw))
    elif frame.packet_type == PacketType.REALTIME_RAW_DATA:
        rec.update(_realtime_raw(raw, include_imu))
    elif frame.packet_type == PacketType.EVENT:
        rec["event"] = _get(raw, 6, "B")
        rec["event_time"] = _iso(_get(raw, 8, "<I"))
    elif frame.packet_type == PacketType.COMMAND_RESPONSE:
        rec["resp_cmd"] = _get(raw, 6, "B")
    elif frame.packet_type == PacketType.METADATA:
        rec["meta_type"] = _get(raw, 6, "B")

    # Always retain the bytes. Large IMU/optical frames are the exception:
    # keeping them would dominate the payload, so they are summarised instead.
    if len(raw) <= 256 or include_imu:
        rec["raw_hex"] = raw.hex()
    else:
        rec["raw_len"] = len(raw)

    # Content-addressed id, assigned here rather than at send time. This makes
    # it stable across forwarder retries AND across re-offloads of the same
    # record by the strap, so the receiver can de-duplicate on it.
    rec["record_id"] = hashlib.sha256(raw).hexdigest()[:32]
    return rec


def _historical(raw: bytes, version: int) -> dict[str, Any]:
    """HISTORICAL_DATA (type 47). Version is carried in the frame's seq byte."""
    out: dict[str, Any] = {"kind": "historical", "version": version}
    # V12 shares V24's layout; other versions have a different one, so decode
    # only the timestamp and leave the rest to raw_hex.
    if version not in (12, 24):
        out["unix"] = _get(raw, 11, "<I")
        out["device_time"] = _iso(out["unix"])
        out["decoded"] = False
        return out

    ts = _get(raw, 11, "<I")
    out["unix"] = ts
    out["device_time"] = _iso(ts)
    for name, off, fmt in V24_FIELDS:
        val = _get(raw, off, fmt)
        if val is not None:
            out[name] = round(val, 6) if fmt == "<f" else val
    rr = _rr(raw, 22, 23)
    if rr:
        out["rr_intervals_ms"] = rr
    out["decoded"] = True
    return out


def _realtime(raw: bytes) -> dict[str, Any]:
    """REALTIME_DATA (type 40): live heart rate and RR intervals."""
    ts = _get(raw, 6, "<I")
    out: dict[str, Any] = {
        "kind": "realtime",
        "unix": ts,
        "device_time": _iso(ts),
        "subseconds": _get(raw, 10, "<H"),
        "heart_rate": _get(raw, 12, "B"),
    }
    rr = _rr(raw, 13, 14)
    if rr:
        out["rr_intervals_ms"] = rr
    return out


def _realtime_raw(raw: bytes, include_imu: bool) -> dict[str, Any]:
    """REALTIME_RAW_DATA (type 43): IMU or PPG waveform, selected by frame length."""
    ts = _get(raw, 11, "<I")
    out: dict[str, Any] = {
        "kind": "realtime_raw",
        "cmd": _get(raw, 6, "B"),
        "unix": ts,
        "device_time": _iso(ts),
        "subseconds": _get(raw, 15, "<H"),
        "frame_len": len(raw),
    }

    if len(raw) == 1917:  # IMU variant
        out["stream"] = "imu"
        out["heart_rate"] = _get(raw, 21, "B")
        rr = _rr(raw, 22, 23)
        if rr:
            out["rr_intervals_ms"] = rr
        if include_imu:
            for name, off, kind in IMU_AXES:
                scale = ACCEL_SCALE if kind == "accel" else GYRO_SCALE
                n = min(IMU_SAMPLES, max(0, len(raw) - off) // 2)
                out[name] = [
                    round(struct.unpack_from("<h", raw, off + i * 2)[0] * scale, 6)
                    for i in range(n)
                ]
            out["imu_units"] = {"accel": "g", "gyro": "deg/s", "rate_hz": 100}
    elif len(raw) == 1921:  # optical / PPG waveform variant
        out["stream"] = "ppg"
        if include_imu:
            samples = []
            off, stride = 42, 4
            while off + 3 <= len(raw) and len(samples) < 419:
                # 24-bit signed little-endian; byte[3] is an unmapped aux byte.
                v = raw[off] | (raw[off + 1] << 8) | (raw[off + 2] << 16)
                samples.append(v - 0x1000000 if v & 0x800000 else v)
                off += stride
            out["ppg_waveform"] = samples
            out["ppg_units"] = {"dtype": "s24_ac_coupled", "rate_hz": 437}
    else:
        out["stream"] = "unknown"
    return out
