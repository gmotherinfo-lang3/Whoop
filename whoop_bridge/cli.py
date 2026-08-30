"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

import click

from .config import Config
from .connection import WhoopBridge, scan
from .forwarder import Forwarder
from .heartbeat import Heartbeat
from .updater import Updater, apply_pending, relaunch
from .spool import Spool


def _setup_logging(level: str, log_file: str = "") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


@click.group()
def main() -> None:
    """Bridge a WHOOP 4.0 strap to a cloud endpoint over Bluetooth LE."""


@main.command("scan")
@click.option("--timeout", default=12.0, help="Scan duration in seconds.")
def scan_cmd(timeout: float) -> None:
    """List nearby WHOOP straps and their Bluetooth addresses."""
    _setup_logging("INFO")
    devices = asyncio.run(scan(timeout))
    if not devices:
        click.echo("No WHOOP strap found.")
        click.echo("Check: strap charged, not connected in the WHOOP phone app, "
                   "and Windows Bluetooth is on.")
        raise SystemExit(1)
    for address, name in devices:
        click.echo(f"{address}  {name}")


@main.command("run")
@click.option("--config", "-c", "config_path", default="config.toml", help="Config file path.")
@click.option("--dry-run", is_flag=True, help="Collect and spool, but do not forward anywhere.")
def run_cmd(config_path: str, dry_run: bool) -> None:
    """Run the bridge: connect, sync, and forward."""
    # Apply a staged update before anything is imported for real work, then
    # re-exec so the new code is what actually runs.
    if not os.environ.get("WHOOP_SKIP_UPDATE"):
        applied = apply_pending()
        if applied:
            click.echo(f"applied update {applied}, restarting")
            os.environ["WHOOP_SKIP_UPDATE"] = "1"
            relaunch()

    cfg = Config.load(config_path)
    _setup_logging(cfg.log_level, cfg.log_file)
    log = logging.getLogger("whoop")

    updater = Updater(cfg, interval=cfg.update_check_interval)
    if cfg.auto_update and cfg.forward_url:
        # One synchronous check at startup: settings the server pushes should
        # apply to this run, not the next one.
        try:
            import httpx
            base = cfg.forward_url.rsplit("/ingest", 1)[0].rstrip("/")
            resp = httpx.get(f"{base}/api/bridge/config", headers=updater._headers(),
                             timeout=15.0, verify=cfg.verify_tls)
            if resp.status_code == 200:
                changed = updater.apply_pushed_config(resp.json().get("config") or {})
                if changed:
                    log.info("applied settings from server: %s", ", ".join(changed))
                    _setup_logging(cfg.log_level, cfg.log_file)
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            log.debug("could not fetch server settings: %s", exc)

    problems = [p for p in cfg.validate() if not (dry_run and "forward_url" in p)]
    if problems:
        for p in problems:
            click.echo(f"config error: {p}", err=True)
        raise SystemExit(2)

    spool = Spool(cfg.spool_path)
    dropped = spool.trim(cfg.spool_max_rows)
    if dropped:
        log.warning("spool over limit; dropped %d oldest record(s)", dropped)

    bridge = WhoopBridge(
        cfg.address, spool,
        include_imu=cfg.include_imu, live_hr=cfg.live_hr, backfill=cfg.backfill,
        ack_and_trim=cfg.ack_and_trim, backfill_interval=cfg.backfill_interval,
    )
    heartbeat = None if dry_run else Heartbeat(
        bridge, spool, url=cfg.forward_url, token=cfg.forward_token or None,
        interval=cfg.heartbeat_interval, verify_tls=cfg.verify_tls,
        cf_access_client_id=cfg.cf_access_client_id or None,
        cf_access_client_secret=cfg.cf_access_client_secret or None,
    )
    forwarder = None if dry_run else Forwarder(
        spool, url=cfg.forward_url, token=cfg.forward_token or None,
        hmac_secret=cfg.hmac_secret or None, batch_size=cfg.batch_size,
        interval=cfg.forward_interval, verify_tls=cfg.verify_tls,
        cf_access_client_id=cfg.cf_access_client_id or None,
        cf_access_client_secret=cfg.cf_access_client_secret or None,
    )

    async def go() -> None:
        loop = asyncio.get_running_loop()
        def shutdown() -> None:
            log.info("shutting down")
            bridge.stop()
            if forwarder:
                forwarder.stop()
            if heartbeat:
                heartbeat.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown)
            except NotImplementedError:
                # Windows: add_signal_handler is unsupported; fall back to
                # the default KeyboardInterrupt path.
                signal.signal(sig, lambda *_: shutdown())
        tasks = [asyncio.create_task(bridge.run())]
        if forwarder:
            tasks.append(asyncio.create_task(forwarder.run()))
        if heartbeat:
            tasks.append(asyncio.create_task(heartbeat.run()))
        if cfg.auto_update and cfg.forward_url:
            tasks.append(asyncio.create_task(_update_loop(updater, bridge)))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stats: %s | %d record(s) still queued", bridge.stats, spool.depth())
        spool.close()


async def _update_loop(updater, bridge) -> None:
    """Check for updates on a slow timer. Staged only -- applied at next start."""
    log = logging.getLogger("whoop.update")
    while not bridge._stop.is_set():
        await asyncio.to_thread(updater.check)
        if updater.status.get("pending"):
            log.info("update %s ready; it applies when the bridge next starts",
                     updater.status["pending"])
        try:
            await asyncio.wait_for(bridge._stop.wait(), timeout=updater.interval)
        except asyncio.TimeoutError:
            pass


@main.command("update")
@click.option("--config", "-c", "config_path", default="config.toml")
@click.option("--apply", "do_apply", is_flag=True, help="Apply a staged update now.")
def update_cmd(config_path: str, do_apply: bool) -> None:
    """Check the server for a newer bridge, or apply one already staged."""
    cfg = Config.load(config_path)
    _setup_logging(cfg.log_level)
    if do_apply:
        applied = apply_pending()
        click.echo(f"applied {applied}" if applied else "nothing staged")
        return
    status = Updater(cfg).check()
    click.echo(f"installed: {status['installed']}")
    click.echo(f"server:    {status['available'] or '(unknown)'}")
    click.echo(f"state:     {status['state']}")
    if status.get("pending"):
        click.echo("\nRestart the bridge to apply it.")


@main.command("status")
@click.option("--config", "-c", "config_path", default="config.toml")
def status_cmd(config_path: str) -> None:
    """Show how many records are waiting to be forwarded."""
    cfg = Config.load(config_path)
    spool = Spool(cfg.spool_path)
    click.echo(f"spool: {cfg.spool_path}")
    click.echo(f"queued records: {spool.depth()}")
    for rid, rec in spool.peek(3):
        click.echo(f"  [{rid}] {json.dumps(rec)[:160]}")
    spool.close()


@main.command("test-endpoint")
@click.option("--config", "-c", "config_path", default="config.toml")
def test_endpoint_cmd(config_path: str) -> None:
    """POST one synthetic record to verify the endpoint accepts the format."""
    import httpx
    cfg = Config.load(config_path)
    _setup_logging(cfg.log_level)
    if not cfg.forward_url:
        click.echo("no forward_url configured", err=True)
        raise SystemExit(2)
    body = {"records": [{"kind": "test", "packet": "SELF_TEST", "heart_rate": 60}]}
    headers = {"Content-Type": "application/json"}
    if cfg.forward_token:
        headers["Authorization"] = f"Bearer {cfg.forward_token}"
    resp = httpx.post(cfg.forward_url, json=body, headers=headers,
                      timeout=15.0, verify=cfg.verify_tls)
    click.echo(f"HTTP {resp.status_code}")
    click.echo(resp.text[:500])
    raise SystemExit(0 if 200 <= resp.status_code < 300 else 1)




@main.command("pair")
@click.option("--config", "-c", "config_path", default="config.toml",
              help="Config file to write the token into.")
@click.option("--server", default="", help="Your server's address, e.g. https://strap.example.com")
@click.option("--code", default="", help="The pairing code from the app.")
@click.option("--name", default="", help="What to call this laptop in the app.")
def pair_cmd(config_path: str, server: str, code: str, name: str) -> None:
    """Claim a pairing code and write this laptop's own token into the config.

    Replaces copying a shared secret out of the server's settings. The code is
    short-lived and single use, and what comes back belongs to this laptop
    alone -- revoking it later does not disturb any other.
    """
    import socket
    import tomllib

    import httpx

    _setup_logging("INFO")
    path = Path(config_path)
    existing: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            existing = tomllib.load(fh)

    server = (server or existing.get("forward", {}).get("forward_url", "")
              .rsplit("/ingest", 1)[0]).strip().rstrip("/")
    if not server:
        server = click.prompt("Your server's address (e.g. https://strap.example.com)").strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        server = "https://" + server
    if not code:
        code = click.prompt("Pairing code from the app (shown under Settings)")
    if not name:
        name = socket.gethostname() or "Laptop"

    cf_id = existing.get("forward", {}).get("cf_access_client_id", "")
    cf_secret = existing.get("forward", {}).get("cf_access_client_secret", "")
    headers = {"Content-Type": "application/json"}
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret

    try:
        resp = httpx.post(f"{server}/pair/claim", json={"code": code, "device_name": name},
                          headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        raise SystemExit(f"Could not reach {server}: {exc}")

    if resp.status_code == 404:
        raise SystemExit(
            f"{server} answered, but has no pairing endpoint. Update the server first.")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        raise SystemExit(f"Pairing failed: {detail or resp.status_code}")

    got = resp.json()
    _write_pairing(path, server, got)
    click.echo(f"Paired with {got.get('account', 'your account')} as {name!r}.")
    click.echo(f"Token written to {path}.")
    click.echo("Next: whoop-bridge scan   (to find your strap)")


def _write_pairing(path: Path, server: str, got: dict) -> None:
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


if __name__ == "__main__":
    main()
