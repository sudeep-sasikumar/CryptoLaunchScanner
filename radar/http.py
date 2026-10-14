"""Shared HTTP client: one rate limiter per API, polite retries, never raises to the caller."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("http")
UA = "Mozilla/5.0 (compatible; LaunchRadar/1.0)"


class RateLimiter:
    """Spaces calls evenly (60 / per_min seconds apart). After a 429 the gap doubles for a
    while and then recovers, so a limit that turns out lower than configured is respected."""

    def __init__(self, per_min: float):
        self.base = 60.0 / max(per_min, 0.1)
        self.gap = self.base
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.calls = self.errors = self.limited = 0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.gap
        if delay > 0:
            await asyncio.sleep(delay)

    def backlog_s(self) -> float:
        """How far ahead the queue is booked - callers skip optional work when this is long."""
        return max(0.0, self._next - time.monotonic())

    def penalize(self, retry_after: float = 0.0) -> None:
        self.limited += 1
        self.gap = min(self.gap * 2, 120.0)
        self._next = max(self._next, time.monotonic() + max(retry_after, self.gap))

    def recover(self) -> None:
        if self.gap > self.base:
            self.gap = max(self.base, self.gap * 0.97)


class Http:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=6.0), follow_redirects=True,
                                        headers={"User-Agent": UA, "Accept": "application/json"},
                                        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20))
        self.limiters: dict[str, RateLimiter] = {}

    def limiter(self, name: str, per_min: float) -> RateLimiter:
        if name not in self.limiters:
            self.limiters[name] = RateLimiter(per_min)
        return self.limiters[name]

    async def close(self) -> None:
        await self.client.aclose()

    async def get_json(self, url: str, *, limiter: RateLimiter | None = None, params: dict | None = None,
                       headers: dict | None = None, timeout: float | None = None, retries: int = 1,
                       method: str = "GET", json_body: dict | None = None):
        """JSON body, or None on any failure (logged at debug: failures are routine here)."""
        for attempt in range(retries + 1):
            if limiter:
                await limiter.wait()
                limiter.calls += 1
            try:
                r = await self.client.request(method, url, params=params, headers=headers, json=json_body,
                                              timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            except (httpx.HTTPError, OSError) as e:
                if limiter:
                    limiter.errors += 1
                log.debug("%s failed: %s", url, e)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                ra = r.headers.get("retry-after", "")
                if limiter:
                    limiter.penalize(float(ra) if ra.replace(".", "", 1).isdigit() else 0.0)
                else:
                    await asyncio.sleep(2.0)
                continue
            if r.status_code >= 400:
                if limiter:
                    limiter.errors += 1
                log.debug("%s -> HTTP %s", url, r.status_code)
                if r.status_code < 500:
                    return None
                continue
            if limiter:
                limiter.recover()
            try:
                return r.json()
            except ValueError:
                return None
        return None

    async def get_bytes(self, url: str, *, timeout: float = 6.0, max_bytes: int = 3_000_000) -> bytes | None:
        try:
            async with self.client.stream("GET", url, timeout=timeout, headers={"Accept": "*/*"}) as r:
                if r.status_code >= 400:
                    return None
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        return None
                return bytes(buf)
        except (httpx.HTTPError, OSError):
            return None
