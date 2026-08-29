"""Tell the server the laptop and strap are alive.

Without this the dashboard cannot tell "the strap is off your wrist" from
"the laptop is asleep" -- both simply look like no new data. The heartbeat
makes that distinction explicit, and carries the battery level so the strap's
charge is visible from the phone.

It is deliberately separate from the record forwarder: status must keep
flowing even when the spool is empty or the endpoint is rejecting records.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("whoop.heartbeat")


class Heartbeat:
    def __init__(self, bridge, spool, *, url: str, token: str | None = None,
                 interval: float = 30.0, timeout: float = 10.0,
                 verify_tls: bool = True, cf_access_client_id: str | None = None,
                 cf_access_client_secret: str | None = None):
        self.bridge = bridge
        self.spool = spool
        # Derived from the ingest URL so there is nothing extra to configure.
        self.url = url.rsplit("/ingest", 1)[0].rstrip("/") + "/status"
        self.token = token
        self.interval = interval
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.cf_id = cf_access_client_id
        self.cf_secret = cf_access_client_secret
        self._stop = asyncio.Event()

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "whoop-bridge/1.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.cf_id and self.cf_secret:
            h["CF-Access-Client-Id"] = self.cf_id
            h["CF-Access-Client-Secret"] = self.cf_secret
        return h

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls) as client:
            while not self._stop.is_set():
                try:
                    await client.post(self.url, json=self.bridge.status(self.spool.depth()),
                                      headers=self._headers())
                except httpx.HTTPError as exc:
                    # A missed heartbeat is not worth retrying: the next one is
                    # along shortly and stale status is shown as stale anyway.
                    log.debug("heartbeat failed (%s)", exc.__class__.__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
