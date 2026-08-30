"""What first-run setup writes, kept apart from the window that collects it.

Separate from setup_window because none of this needs a GUI toolkit: it is
file handling and string tidying, and it should be testable — and reusable by
the CLI — without importing Tk.
"""

from __future__ import annotations

from pathlib import Path


def normalise_code(code: str) -> str:
    """A pairing code, however it was read off a screen and typed.

    The server tidies it too, but doing it here means a code pasted with a
    space or in lower case works against any version of the server, and the
    laptop never sends something the person can see is wrong.
    """
    import re
    cleaned = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    return f"{cleaned[:4]}-{cleaned[4:8]}" if len(cleaned) == 8 else cleaned


def normalise_server(value: str) -> str:
    """However someone types their server's address, make it a base URL."""
    value = (value or "").strip().rstrip("/")
    if value.endswith("/ingest"):
        value = value[: -len("/ingest")]
    if value and not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def existing_server(config_path: str | Path) -> str:
    """Pre-fill from the config the download already carries."""
    path = Path(config_path)
    if not path.exists():
        return ""
    try:
        import tomllib
        with path.open("rb") as fh:
            url = tomllib.load(fh).get("forward", {}).get("forward_url", "")
        return str(url).rsplit("/ingest", 1)[0]
    except Exception:                                     # noqa: BLE001
        return ""


def set_config_value(path: str | Path, section: str, key: str, value: str) -> None:
    """Set one key in one section, leaving the rest of the file alone.

    Edited in place rather than regenerated, so comments and anything already
    tuned survive. The file is meant to stay readable and hand-editable —
    setup is a convenience, not the only way in.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    in_section = False
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_section and not done:
                out.append(f'{key} = "{value}"')
                done = True
            in_section = stripped == f"[{section}]"
        if in_section and not done and (stripped.startswith(f"{key} ")
                                        or stripped.startswith(f"{key}=")):
            line = f'{key} = "{value}"'
            done = True
        out.append(line)
    if not done:
        if f"[{section}]" not in "\n".join(out):
            out.append(f"[{section}]")
        out.append(f'{key} = "{value}"')
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def needs_setup(config_path: str | Path) -> bool:
    """True when the config cannot yet run: no key of its own, or no strap."""
    from whoop_bridge.config import Config
    path = Path(config_path)
    if not path.exists():
        return True
    try:
        cfg = Config.load(path)
    except Exception:                                     # noqa: BLE001
        return True
    return not (cfg.forward_token and cfg.address)


def claim_pairing(server: str, code: str, device_name: str) -> dict:
    """Exchange a pairing code for this laptop's own key."""
    import httpx
    resp = httpx.post(f"{server}/pair/claim",
                      json={"code": normalise_code(code), "device_name": device_name},
                      timeout=30.0)
    if resp.status_code == 200:
        return resp.json()
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    raise RuntimeError(detail or f"The server answered {resp.status_code}.")


def write_pairing(path: Path, server: str, got: dict) -> None:
    """Put the device token into the config, leaving everything else alone.

    Written in place rather than regenerated, so a config someone has already
    tuned -- their strap's address especially -- survives re-pairing.
    """
    template = path.read_text(encoding="utf-8") if path.exists() else _blank_config()
    replacements = {
        "forward_url": got.get("ingest_url") or f"{server}/ingest",
        "forward_token": got["token"],
    }
    lines, seen = [], set()
    in_forward = False
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_forward = stripped == "[forward]"
        if in_forward:
            for key, value in replacements.items():
                if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
                    line = f'{key} = "{value}"'
                    seen.add(key)
                    break
        lines.append(line)
    missing = [f'{k} = "{v}"' for k, v in replacements.items() if k not in seen]
    if missing:
        lines.append("")
        lines.append("[forward]" if "[forward]" not in template else "")
        lines.extend(missing)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _blank_config() -> str:
    return ('[device]\naddress = ""\n\n[forward]\nforward_url = ""\n'
            'forward_token = ""\n\n[storage]\nspool_path = "whoop-spool.db"\n')
