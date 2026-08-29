"""Push spooled records to a configured HTTPS endpoint.

Design notes:
  * Records are batched to keep request count low on a long-running bridge.
  * Delivery is at-least-once: rows are deleted only after a 2xx. Each record
    carries a stable `record_id` so the receiver can de-duplicate.
  * Backoff is exponential with a cap, so an endpoint outage does not turn into
    a hot retry loop.
  * An optional HMAC-SHA256 signature lets the receiver verify the payload
    really came from this bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid

import httpx

log = logging.getLogger("whoop.forward")

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class Forwarder:
    def __init__(self, spool, *, url: str, token: str | None = None,
                 hmac_secret: str | None = None, batch_size: int = 50,
                 interval: float = 5.0, timeout: float = 15.0,
                 max_backoff: float = 300.0, verify_tls: bool = True):
        if not url:
            raise ValueError("no forward URL configured")
        if not url.lower().startswith("https://"):
            # Biometric data over plain HTTP would be readable on the wire.
            raise ValueError(f"forward URL must be https://, got {url!r}")
        self.spool = spool
        self.url = url
        self.token = token
        self.hmac_secret = hmac_secret.encode() if hmac_secret else None
        self.batch_size = batch_size
        self.interval = interval
        self.timeout = timeout
        self.max_backoff = max_backoff
        self.verify_tls = verify_tls
        self._stop = asyncio.Event()

    def _headers(self, body: bytes) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "User-Agent": "whoop-bridge/1.0",
            "Idempotency-Key": hashlib.sha256(body).hexdigest(),
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.hmac_secret:
            h["X-Signature-SHA256"] = hmac.new(self.hmac_secret, body, hashlib.sha256).hexdigest()
        return h

    async def run(self) -> None:
        backoff = self.interval
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls) as client:
            while not self._stop.is_set():
                batch = self.spool.peek(self.batch_size)
                if not batch:
                    await self._sleep(self.interval)
                    backoff = self.interval
                    continue

                ids = [i for i, _ in batch]
                records = [r for _, r in batch]
                for r in records:
                    r.setdefault("record_id", str(uuid.uuid4()))
                body = json.dumps({"records": records}, separators=(",", ":")).encode()

                try:
                    resp = await client.post(self.url, content=body, headers=self._headers(body))
                except httpx.HTTPError as exc:
                    log.warning("forward failed (%s); %d queued; retry in %.0fs",
                                exc.__class__.__name__, self.spool.depth(), backoff)
                    self.spool.bump_attempts(ids)
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
                    continue

                if 200 <= resp.status_code < 300:
                    self.spool.ack(ids)
                    log.info("forwarded %d record(s); %d queued", len(ids), self.spool.depth())
                    backoff = self.interval
                    await self._sleep(0.1)
                elif resp.status_code in RETRYABLE:
                    log.warning("endpoint returned %d; retry in %.0fs", resp.status_code, backoff)
                    self.spool.bump_attempts(ids)
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
                else:
                    # 4xx that will never succeed on retry (bad auth, bad schema).
                    # Keep the rows and stop hammering; the operator must fix config.
                    log.error("endpoint returned %d (not retryable): %s",
                              resp.status_code, resp.text[:200])
                    self.spool.bump_attempts(ids)
                    await self._sleep(self.max_backoff)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()
