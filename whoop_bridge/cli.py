"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

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


if __name__ == "__main__":
    main()
