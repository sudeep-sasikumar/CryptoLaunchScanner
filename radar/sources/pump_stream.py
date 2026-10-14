"""pump.fun live feed: one websocket subscription to the program's logs on a Solana RPC node.

Every successful transaction that touches the program arrives with its log lines; the create /
trade / graduation events are decoded from them (pump_events.py). This is the whole launchpad,
in order, with slot numbers - which is what makes bundle and sniper detection possible.

The free public RPC works but gives no guarantee; a dropped connection is detected, reported to
the engine (tokens live during the gap are marked tainted) and re-established.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets

from .pump_events import PUMP_PROGRAM, decode_logs

log = logging.getLogger("pump")


class PumpStream:
    def __init__(self, ws_url: str, stall_s: float, on_event: Callable[[str, dict, int, str], None],
                 on_gap: Callable[[float], None]):
        self.ws_url, self.stall_s = ws_url, stall_s
        self.on_event, self.on_gap = on_event, on_gap
        self.connected = False
        self.last_msg = 0.0
        self.msgs = self.events = self.reconnects = 0
        self.bytes = 0

    async def run(self) -> None:
        backoff = 1.0
        down_since: float | None = None
        while True:
            try:
                async with websockets.connect(self.ws_url, max_size=None, ping_interval=20, ping_timeout=20,
                                              open_timeout=15) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}))
                    self.connected, self.last_msg = True, time.time()
                    if down_since is not None:
                        self.on_gap(time.time() - down_since)
                        down_since = None
                    log.info("pump.fun stream connected (%s)", self.ws_url.split("?")[0])
                    backoff = 1.0
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.stall_s)
                        self.last_msg = time.time()
                        self.msgs += 1
                        self.bytes += len(raw)
                        if '"err":null' not in raw:          # failed transaction: no state changed
                            continue
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 - any failure means the same thing: reconnect
                log.warning("pump.fun stream lost (%s: %s) - reconnecting in %.0fs", type(e).__name__, e, backoff)
            self.connected = False
            self.reconnects += 1
            if down_since is None:
                down_since = self.last_msg or time.time()
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
