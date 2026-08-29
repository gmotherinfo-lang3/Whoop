"""Keep the bridge current from the server, without touching the laptop.

The server serves the same code it handed out at setup, plus a version and a
digest. This checks periodically, downloads when the digest differs, verifies
it, and stages it. Nothing is overwritten while the bridge is running: the
staged copy is applied at the next start, so an update can never interrupt a
sync or swap code out from under a live BLE session.

Settings the server pushes are applied the same way -- behaviour only. The
strap address and every credential stay local, so a compromised or
misconfigured server cannot redirect where data goes or who it authenticates
as.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("whoop.update")

STAGE_DIR = ".update"
PENDING = "pending.json"
# Only these may arrive from the server. Anything else is ignored on purpose.
APPLIABLE = {
    "live_hr", "backfill", "ack_and_trim", "include_imu", "backfill_interval",
    "forward_interval", "heartbeat_interval", "batch_size", "log_level",
    "update_check_interval",
}
# Paths an update is allowed to write, relative to the install root.
WRITABLE = ("whoop_bridge/", "tray/", "windows/", "pyproject.toml", "requirements.txt")


def install_root() -> Path:
    """The directory the bridge is installed in (parent of the package)."""
    return Path(__file__).resolve().parent.parent


def _base(url: str) -> str:
    return url.rsplit("/ingest", 1)[0].rstrip("/")


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject anything outside the allowed paths, absolute, or traversing up.

    An update archive is remote input. Without this check a malicious or
    corrupted zip could write anywhere the process can reach.
    """
    ok: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"unsafe path in update archive: {info.filename!r}")
        if not name.startswith(WRITABLE):
            raise ValueError(f"update archive writes outside the bridge: {name!r}")
        ok.append(info)
    return ok


class Updater:
    def __init__(self, config, *, root: Path | None = None,
                 interval: float = 3600.0, timeout: float = 60.0):
        self.cfg = config
        self.root = root or install_root()
        self.interval = interval
        self.timeout = timeout
        self.status: dict[str, Any] = {"state": "idle", "pending": None,
                                       "installed": None, "available": None}

    # --- transport ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "whoop-bridge-updater/1.0"}
        if self.cfg.forward_token:
            h["Authorization"] = f"Bearer {self.cfg.forward_token}"
        if self.cfg.cf_access_client_id and self.cfg.cf_access_client_secret:
            h["CF-Access-Client-Id"] = self.cfg.cf_access_client_id
            h["CF-Access-Client-Secret"] = self.cfg.cf_access_client_secret
        return h

    # --- config -------------------------------------------------------------
    def apply_pushed_config(self, pushed: dict[str, Any]) -> list[str]:
        """Merge server settings onto the live config. Returns what changed."""
        changed = []
        for key, value in (pushed or {}).items():
            if key not in APPLIABLE:
                log.debug("ignoring non-appliable setting from server: %s", key)
                continue
            if getattr(self.cfg, key, None) != value:
                setattr(self.cfg, key, value)
                changed.append(f"{key}={value}")
        return changed

    # --- update -------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        """Ask the server what it is serving, and stage it if it differs."""
        from . import __version__
        self.status["installed"] = __version__
        base = _base(self.cfg.forward_url)
        try:
            with httpx.Client(timeout=self.timeout, verify=self.cfg.verify_tls) as client:
                info = client.get(f"{base}/api/bridge/release", headers=self._headers())
                if info.status_code != 200:
                    self.status["state"] = f"server returned {info.status_code}"
                    return self.status
                release = info.json()
                self.status["available"] = release.get("version")

                if self._already_staged(release["sha256"]):
                    self.status["state"] = "pending restart"
                    return self.status
                if release["sha256"] == self._installed_digest():
                    self.status["state"] = "up to date"
                    return self.status

                log.info("update available: %s -> %s", __version__, release.get("version"))
                blob = client.get(f"{base}/api/bridge/bundle.zip", headers=self._headers())
                blob.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.status["state"] = f"check failed ({exc.__class__.__name__})"
            log.debug("update check failed: %s", exc)
            return self.status

        digest = hashlib.sha256(blob.content).hexdigest()
        if digest != release["sha256"]:
            # Refuse anything that does not match what the server described.
            self.status["state"] = "rejected: digest mismatch"
            log.warning("update digest mismatch; refusing it")
            return self.status

        try:
            self._stage(blob.content, release, digest)
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            self.status["state"] = f"rejected: {exc}"
            log.warning("update rejected: %s", exc)
            return self.status

        self.status["state"] = "pending restart"
        self.status["pending"] = release.get("version")
        log.info("update %s staged; it will be applied at the next start",
                 release.get("version"))
        return self.status

    def _stage_dir(self) -> Path:
        return self.root / STAGE_DIR

    def _already_staged(self, digest: str) -> bool:
        marker = self._stage_dir() / PENDING
        if not marker.is_file():
            return False
        try:
            return json.loads(marker.read_text()).get("sha256") == digest
        except (OSError, json.JSONDecodeError):
            return False

    def _installed_digest(self) -> str | None:
        marker = self.root / STAGE_DIR / "installed.json"
        if not marker.is_file():
            return None
        try:
            return json.loads(marker.read_text()).get("sha256")
        except (OSError, json.JSONDecodeError):
            return None

    def _stage(self, blob: bytes, release: dict[str, Any], digest: str) -> None:
        stage = self._stage_dir()
        payload = stage / "next"
        if payload.exists():
            shutil.rmtree(payload)
        payload.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(BytesIO(blob)) as zf:
            members = _safe_members(zf)          # raises before anything is written
            for info in members:
                zf.extract(info, payload)

        (stage / PENDING).write_text(json.dumps({
            "version": release.get("version"), "sha256": digest,
            "files": len(members),
        }, indent=2))


def apply_pending(root: Path | None = None) -> str | None:
    """Copy a staged update into place. Call at startup, before doing any work.

    Returns the version applied, or None. The marker is cleared first so a
    failure part-way cannot leave the bridge re-applying the same update on
    every start.
    """
    root = root or install_root()
    stage = root / STAGE_DIR
    marker, payload = stage / PENDING, stage / "next"
    if not marker.is_file() or not payload.is_dir():
        return None

    try:
        meta = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        marker.unlink(missing_ok=True)
        return None

    marker.unlink(missing_ok=True)
    try:
        for src in sorted(payload.rglob("*")):
            if not src.is_file():
                continue
            dest = root / src.relative_to(payload)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target then replace, so a crash mid-copy cannot
            # leave a half-written module behind.
            tmp = dest.with_suffix(dest.suffix + ".new")
            shutil.copy2(src, tmp)
            os.replace(tmp, dest)
        (stage / "installed.json").write_text(json.dumps(
            {"version": meta.get("version"), "sha256": meta.get("sha256")}, indent=2))
        shutil.rmtree(payload, ignore_errors=True)
        log.info("applied update %s", meta.get("version"))
        return meta.get("version")
    except OSError as exc:
        log.error("could not apply update: %s", exc)
        return None


def relaunch() -> None:
    """Re-exec so the freshly copied code is the code that runs."""
    log.info("restarting into the updated bridge")
    os.execv(sys.executable, [sys.executable, "-m", "whoop_bridge.cli", *sys.argv[1:]])
