"""pump.fun live feed: one websocket subscription to the program's logs on a Solana RPC node.

Every successful transaction that touches the program arrives with its log lines; the create /
trade / graduation events are decoded from them (pump_events.py). This is the whole launchpad,
in order, with slot numbers - which is what makes bundle and sniper detection possible.

The free public RPC works but gives no guarantee; a dropped connection is detected, reported to
the engine (tokens live during the gap are marked tainted) and re-established.

A private node (SOLANA_WS_URL) can stop answering for good - e.g. when its plan's credits run out. So
after `switch_after` sessions in a row that delivered nothing, the stream moves to the fallback node
(the free public one) and goes back to the private node after `retry_primary_s`, at the next drop (never
by cutting a working feed). Errors are kept without the URL's query string, which holds the API key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Callable

import websockets

from .pump_events import PUMP_PROGRAM, decode_logs

log = logging.getLogger("pump")


def host(url: str) -> str:
    """The node's address without path or query (where API keys live) - safe to show and log."""
    return re.sub(r"^wss?://", "", url.split("?")[0]).split("/")[0]


def _redact(text: str) -> str:
    return re.sub(r"(api[-_]?key=)[^&\s'\"]+", r"\1***", text, flags=re.I)


class PumpStream:
    def __init__(self, ws_url: str, stall_s: float, on_event: Callable[[str, dict, int, str], None],
                 on_gap: Callable[[float], None], fallback_url: str | None = None, switch_after: int = 3,
                 retry_primary_s: float = 1800.0):
        self.urls = [ws_url] + ([fallback_url] if fallback_url and fallback_url != ws_url else [])
        self.stall_s = stall_s
        self.on_event, self.on_gap = on_event, on_gap
        self.switch_after, self.retry_primary_s = max(int(switch_after), 1), retry_primary_s
        self.i = 0                                    # 0 = primary node, 1 = fallback
        self.since = time.time()                      # on the current node since
        self.fails = 0                                # sessions in a row on this node that delivered nothing
        self.failovers = 0
        self.last_error: str | None = None
        self.last_error_ts: float | None = None
        self.connected = False
        self.last_msg = 0.0
        self.msgs = self.events = self.reconnects = 0
        self.bytes = 0

    @property
    def ws_url(self) -> str:
        return self.urls[self.i]

    def node(self) -> dict:
        return {"node": host(self.ws_url), "fallback": self.i == 1, "since": self.since, "failovers": self.failovers,
                "last_error": self.last_error, "last_error_ts": self.last_error_ts}

    def _ended(self, got: int, now: float) -> None:
        """A session ended after `got` messages: count it, and change node when the rule says so."""
        self.fails = 0 if got > 1 else self.fails + 1     # the first message is only the subscription reply
        if len(self.urls) < 2:
            return
        if self.i == 0 and self.fails >= self.switch_after:
            log.warning("pump.fun stream: %s delivered nothing %d times in a row - switching to %s",
                        host(self.urls[0]), self.fails, host(self.urls[1]))
            self.i, self.since, self.fails = 1, now, 0
            self.failovers += 1
        elif self.i == 1 and now - self.since >= self.retry_primary_s:
            log.info("pump.fun stream: trying %s again", host(self.urls[0]))
            self.i, self.since, self.fails = 0, now, 0

    async def run(self) -> None:
        backoff = 1.0
        down_since: float | None = None
        while True:
            got = 0
            try:
                async with websockets.connect(self.ws_url, max_size=None, ping_interval=20, ping_timeout=20,
                                              open_timeout=15) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}))
                    self.connected, self.last_msg = True, time.time()
                    log.info("pump.fun stream connected (%s)", host(self.ws_url))
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.stall_s)
                        got += 1
                        if got == 1 and '"error"' in raw[:200]:
                            raise RuntimeError(f"subscription refused: {raw[:200]}")
                        if got == 2:                         # real traffic: the gap (if any) is over
                            backoff = 1.0
                            if down_since is not None:
                                self.on_gap(time.time() - down_since)
                                down_since = None
                        self.last_msg = time.time()
                        self.msgs += 1
                        self.bytes += len(raw)
                        if '"err":null' not in raw:          # failed transaction: no state changed
                            continue
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 - any failure means the same thing: reconnect
                err = _redact(f"{type(e).__name__}: {e}")[:300]
                self.last_error, self.last_error_ts = err, time.time()
                log.warning("pump.fun stream lost on %s (%s) - reconnecting in %.0fs", host(self.ws_url), err, backoff)
            self.connected = False
            self.reconnects += 1
            if down_since is None:
                down_since = self.last_msg or time.time()
            self._ended(got, time.time())
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: str) -> None:
        try:
            res = json.loads(raw)["params"]["result"]
            slot, value = res["context"]["slot"], res["value"]
        except (KeyError, ValueError, TypeError):
            return
        for kind, e in decode_logs(value.get("logs") or []):
            self.events += 1
            try:
                self.on_event(kind, e, slot, value.get("signature", ""))
            except Exception:   # noqa: BLE001 - one bad event must not kill the feed
                log.exception("handler failed on %s event", kind)
