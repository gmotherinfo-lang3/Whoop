"""Configuration loading: TOML file with environment-variable overrides.

Secrets (the endpoint URL, bearer token, HMAC secret) can be supplied via the
environment so they never have to be written into a file that might be
committed. Environment values win over the file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    address: str = ""
    include_imu: bool = False
    live_hr: bool = True
    backfill: bool = True
    ack_and_trim: bool = True
    backfill_interval: float = 900.0

    forward_url: str = ""
    forward_token: str = ""
    hmac_secret: str = ""
    batch_size: int = 50
    forward_interval: float = 5.0
    verify_tls: bool = True

    spool_path: str = "whoop-spool.db"
    spool_max_rows: int = 500_000
    log_level: str = "INFO"
    log_file: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        data: dict = {}
        if path:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"config file not found: {p}")
            with p.open("rb") as fh:
                raw = tomllib.load(fh)
            # Flatten the [device] / [forward] / [storage] sections.
            for section in ("device", "forward", "storage", "logging"):
                data.update(raw.get(section, {}))

        cfg = cls()
        for f in cls.__dataclass_fields__:
            if f in data:
                setattr(cfg, f, data[f])

        # Environment overrides -- intended for secrets and service deployment.
        env_map = {
            "WHOOP_ADDRESS": "address",
            "WHOOP_FORWARD_URL": "forward_url",
            "WHOOP_FORWARD_TOKEN": "forward_token",
            "WHOOP_HMAC_SECRET": "hmac_secret",
            "WHOOP_SPOOL_PATH": "spool_path",
            "WHOOP_LOG_LEVEL": "log_level",
            "WHOOP_LOG_FILE": "log_file",
        }
        for env, attr in env_map.items():
            val = os.environ.get(env)
            if val:
                setattr(cfg, attr, val)
        return cfg

    def validate(self) -> list[str]:
        problems = []
        if not self.address:
            problems.append("no device address set (run `whoop-bridge scan` first)")
        if not self.forward_url:
            problems.append("no forward_url set")
        elif not self.forward_url.lower().startswith("https://"):
            problems.append("forward_url must be https:// (biometric data in transit)")
        return problems
