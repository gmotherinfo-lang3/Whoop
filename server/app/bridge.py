"""Serve bridge updates and settings, so the laptop never needs hand-editing.

The laptop half is already in this image (it is what /setup hands out). This
exposes it as an update channel instead: a version the bridge can compare
against, a byte-identical code archive it can fetch, and the settings the
server wants it to run with.

Two things are deliberately NOT pushed from here: the strap's Bluetooth
address, which only the laptop can discover, and any secret. Config that
arrives over the wire can change behaviour but never credentials.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from .bundle import BUNDLE_ROOT, INCLUDE_DIRS, INCLUDE_FILES, SKIP_DIRS, SKIP_SUFFIXES

# A fixed timestamp makes the archive byte-identical for identical source, so
# its hash identifies the code rather than the moment it was zipped.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

_cache: dict[str, Any] = {}


def _files(root: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for name in INCLUDE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix in SKIP_SUFFIXES:
                continue
            if SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            out.append((str(path.relative_to(root)).replace("\\", "/"), path))
    for name in INCLUDE_FILES:
        path = root / name
        if path.is_file():
            out.append((name, path))
    return sorted(out)


def build_code_zip(root: Path | None = None) -> bytes:
    """Code only -- no config, no credentials. Deterministic for a given source."""
    root = root or BUNDLE_ROOT
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, path in _files(root):
            info = zipfile.ZipInfo(rel, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())
    return buf.getvalue()


def bridge_version(root: Path | None = None) -> str:
    """Read __version__ out of the bundled package."""
    root = root or BUNDLE_ROOT
    init = root / "whoop_bridge" / "__init__.py"
    if not init.is_file():
        return "0.0.0"
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init.read_text())
    return match.group(1) if match else "0.0.0"


def release(root: Path | None = None) -> dict[str, Any]:
    """Version and digest of the code this server is serving. Cached by mtime."""
    root = root or BUNDLE_ROOT
    stamp = max((p.stat().st_mtime for _, p in _files(root)), default=0.0)
    if _cache.get("stamp") == stamp and _cache.get("root") == str(root):
        return _cache["release"]

    data = build_code_zip(root)
    info = {
        "version": bridge_version(root),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "files": len(_files(root)),
    }
    _cache.update({"stamp": stamp, "root": str(root), "release": info, "zip": data})
    return info


def cached_zip(root: Path | None = None) -> bytes:
    release(root)
    return _cache["zip"]


# Settings the server is allowed to dictate. Everything else -- the strap
# address, tokens, file paths -- stays local to the laptop.
PUSHABLE = {
    "live_hr": ("BRIDGE_LIVE_HR", bool),
    "backfill": ("BRIDGE_BACKFILL", bool),
    "ack_and_trim": ("BRIDGE_ACK_AND_TRIM", bool),
    "include_imu": ("BRIDGE_INCLUDE_IMU", bool),
    "backfill_interval": ("BRIDGE_BACKFILL_INTERVAL", float),
    "forward_interval": ("BRIDGE_FORWARD_INTERVAL", float),
    "heartbeat_interval": ("BRIDGE_HEARTBEAT_INTERVAL", float),
    "batch_size": ("BRIDGE_BATCH_SIZE", int),
    "log_level": ("BRIDGE_LOG_LEVEL", str),
    "update_check_interval": ("BRIDGE_UPDATE_CHECK_INTERVAL", float),
}


def _coerce(raw: str, kind: type) -> Any:
    if kind is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return kind(raw)


def pushed_config() -> dict[str, Any]:
    """Settings the operator has set on the server, from BRIDGE_* env vars."""
    out: dict[str, Any] = {}
    for key, (env, kind) in PUSHABLE.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            out[key] = _coerce(raw, kind)
        except (TypeError, ValueError):
            continue
    return out
