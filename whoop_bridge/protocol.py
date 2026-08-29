"""WHOOP 4.0 BLE protocol: framing, checksums, commands.

Protocol facts (frame layout, command numbers, field offsets) are factual
observations about bytes on a wire and are documented in PROTOCOL.md. This is
an independent Python implementation -- no third-party source is copied here.
See ATTRIBUTION.md for the reverse-engineering work these facts come from.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

# --- GATT identifiers -------------------------------------------------------
SERVICE_UUIDS = (
    "61080000-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080002-8d6d-82b8-614a-1c8cb0f8dcc6",
)

CHAR_CMD_TO_STRAP = "61080010-8d6d-82b8-614a-1c8cb0f8dcc6"
CHAR_CMD_FROM_STRAP = "61080012-8d6d-82b8-614a-1c8cb0f8dcc6"
CHAR_EVENTS_FROM_STRAP = "61080015-8d6d-82b8-614a-1c8cb0f8dcc6"
CHAR_DATA_FROM_STRAP = "61080018-8d6d-82b8-614a-1c8cb0f8dcc6"
CHAR_MEMFAULT = "6108001b-8d6d-82b8-614a-1c8cb0f8dcc6"

NOTIFY_CHARS = (CHAR_CMD_FROM_STRAP, CHAR_EVENTS_FROM_STRAP, CHAR_DATA_FROM_STRAP)

SOF = 0xAA


class PacketType:
    COMMAND = 35
    COMMAND_RESPONSE = 36
    REALTIME_DATA = 40
    REALTIME_RAW_DATA = 43
    HISTORICAL_DATA = 47
    EVENT = 48
    METADATA = 49
    CONSOLE_LOGS = 50
    REALTIME_IMU_DATA_STREAM = 51
    HISTORICAL_IMU_DATA_STREAM = 52


PACKET_TYPE_NAMES = {
    35: "COMMAND", 36: "COMMAND_RESPONSE", 40: "REALTIME_DATA",
    43: "REALTIME_RAW_DATA", 47: "HISTORICAL_DATA", 48: "EVENT",
    49: "METADATA", 50: "CONSOLE_LOGS", 51: "REALTIME_IMU_DATA_STREAM",
    52: "HISTORICAL_IMU_DATA_STREAM",
}


class Cmd:
    """Command numbers carried at frame offset 6 of a COMMAND packet."""
    LINK_VALID = 1
    GET_MAX_PROTOCOL_VERSION = 2
    TOGGLE_REALTIME_HR = 3
    REPORT_VERSION_INFO = 7
    SET_CLOCK = 10
    GET_CLOCK = 11
    TOGGLE_GENERIC_HR_PROFILE = 14
    TOGGLE_R7_DATA_COLLECTION = 16
    ABORT_HISTORICAL_TRANSMITS = 20
    SEND_HISTORICAL_DATA = 22
    HISTORICAL_DATA_RESULT = 23
    GET_BATTERY_LEVEL = 26
    SET_READ_POINTER = 33
    GET_DATA_RANGE = 34
    GET_HELLO_HARVARD = 35
    ENTER_HIGH_FREQ_SYNC = 96
    EXIT_HIGH_FREQ_SYNC = 97
    GET_EXTENDED_BATTERY_INFO = 98
    TOGGLE_IMU_MODE = 106
    ENABLE_OPTICAL_DATA = 107
    TOGGLE_OPTICAL_MODE = 108
    GET_HELLO = 145
    # Destructive commands are deliberately not exposed:
    # 25 FORCE_TRIM, 29 REBOOT_STRAP, 32 POWER_CYCLE_STRAP, firmware-load ops.


# --- Checksums --------------------------------------------------------------
def crc8(data: bytes) -> int:
    """CRC-8, poly 0x07, init 0x00. Guards the 2-byte length field.

    Verified against captured headers: aa100057 (len 0x0010 -> 0x57)
    and aa0800a8 (len 0x0008 -> 0xa8).
    """
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc32(data: bytes) -> int:
    """Standard zlib CRC-32 (poly 0xEDB88320 reflected, init/xorout 0xFFFFFFFF).

    Note: several public write-ups claim a custom final XOR of 0xF43F44AC.
    That is not what the strap uses -- the frame trailer is plain zlib CRC-32.
    """
    return zlib.crc32(data) & 0xFFFFFFFF


# --- Framing ----------------------------------------------------------------
# Frame: [0]=0xAA [1:3]=length u16 LE [3]=crc8(length) [4]=packet_type
#        [5]=seq [6]=cmd [7:]=payload, then crc32 u32 LE at offset `length`.
# `length` counts the inner bytes (from offset 4) plus the 4-byte CRC-32,
# so the total frame size is length + 4.

def build_command(cmd: int, payload: bytes = b"\x00", seq: int = 0) -> bytes:
    """Assemble a COMMAND frame for CMD_TO_STRAP."""
    inner = bytes([PacketType.COMMAND, seq & 0xFF, cmd]) + payload
    length = len(inner) + 4
    head = struct.pack("<BH", SOF, length)
    head += bytes([crc8(head[1:3])])
    return head + inner + struct.pack("<I", crc32(inner))


def set_clock_payload(now: int) -> bytes:
    """SET_CLOCK payload: [seconds u32 LE][subseconds u32 LE].

    The length matters: a wrong-length SET_CLOCK is acknowledged but not
    latched, leaving the RTC unset -- and an unset RTC makes the strap refuse
    to serve historical (type-47) data at all.
    """
    return struct.pack("<II", int(now), 0)


@dataclass
class Frame:
    packet_type: int
    seq: int
    payload: bytes      # frame[6:length] -- cmd byte first for COMMAND packets
    raw: bytes
    crc_ok: bool

    @property
    def type_name(self) -> str:
        return PACKET_TYPE_NAMES.get(self.packet_type, f"TYPE_{self.packet_type}")

    def hex(self) -> str:
        return self.raw.hex()


def parse_frame(data: bytes) -> Frame | None:
    """Parse one frame, or return None if `data` is not a well-formed frame."""
    if len(data) < 8 or data[0] != SOF:
        return None
    length = struct.unpack_from("<H", data, 1)[0]
    if crc8(data[1:3]) != data[3]:
        return None
    if length < 7 or length + 4 > len(data):
        return None
    inner = data[4:length]
    expected = struct.unpack_from("<I", data, length)[0]
    return Frame(
        packet_type=inner[0],
        seq=inner[1],
        payload=inner[2:],
        raw=data[: length + 4],
        crc_ok=crc32(inner) == expected,
    )
