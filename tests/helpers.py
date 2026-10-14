"""Builders shared by the tests: synthetic pump.fun events and a small config."""
from __future__ import annotations

import base64
import struct

from radar.config import Config, ROOT, apply_overrides
from radar.sources.pump_events import DISC_CREATE, DISC_TRADE

import yaml

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYSTEM = "11111111111111111111111111111111"


def cfg(overrides: str = "") -> Config:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    apply_overrides(raw, overrides)
    return Config(raw)


def key(n: int) -> bytes:
    """A distinct 32-byte public key."""
    return n.to_bytes(32, "big")


def addr(n: int) -> str:
    from radar.sources.pump_events import b58encode
    return b58encode(key(n))


def _s(text: str) -> bytes:
    b = text.encode()
    return struct.pack("<I", len(b)) + b


def trade_bytes(mint: int, user: int, is_buy: bool, quote_raw: int, token_raw: int, vq: int, vt: int, ts: int,
                real_tok: int = 0, tail: bool = True) -> bytes:
    d = DISC_TRADE + key(mint) + struct.pack("<QQ", quote_raw, token_raw) + bytes([is_buy]) + key(user)
    d += struct.pack("<qQQQQ", ts, vq, vt, 0, real_tok)
    if not tail:
        return d
    d += key(900) + struct.pack("<QQ", 95, 0) + key(901) + struct.pack("<QQ", 30, 0) + bytes([0])
    d += struct.pack("<QQQq", 0, 0, 0, 0) + _s("buy" if is_buy else "sell") + bytes([0]) + struct.pack("<QQQQ", 0, 0, 0, 0)
    d += struct.pack("<I", 0)                                           # no shareholders
    d += bytes(32) + struct.pack("<QQQQQQ", quote_raw, vq, 0, 0, 0, 0)  # quote mint = native SOL
    return d


def create_bytes(mint: int, user: int, name: str = "Test", symbol: str = "TST", uri: str = "https://x.test/m",
                 ts: int = 1_000_000) -> bytes:
    d = DISC_CREATE + _s(name) + _s(symbol) + _s(uri) + key(mint) + key(800) + key(user) + key(user)
    d += struct.pack("<qQQQQ", ts, 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 1_000_000_000_000_000)
    d += key(801) + bytes([0, 0]) + bytes(32) + struct.pack("<QQ", 30_000_000_000, 0) + bytes([0, 0])
    return d


def log_line(data: bytes) -> str:
    return "Program data: " + base64.b64encode(data).decode()


class Curve:
    """A tiny constant-product bonding curve producing realistic trade events."""

    def __init__(self, mint: int = 1, dev: int = 100, ts: int = 1_000_000, slot: int = 5000):
        self.mint, self.dev, self.ts, self.slot = mint, dev, ts, slot
        self.vq, self.vt = 30_000_000_000, 1_073_000_000_000_000
        self.real = 793_100_000_000_000
        self.k = self.vq * self.vt

    def create_event(self, **kw) -> dict:
        from radar.sources.pump_events import decode_create
        return decode_create(create_bytes(self.mint, self.dev, ts=self.ts, **kw))

    def buy(self, user: int, sol: float, dt: float = 0, dslot: int = 0) -> tuple[dict, int]:
        q = int(sol * 1e9)
        new_vq = self.vq + q
        new_vt = self.k // new_vq
        tok = self.vt - new_vt
        self.vq, self.vt, self.real = new_vq, new_vt, self.real - tok
        return self._event(user, True, q, tok, dt, dslot)

    def sell(self, user: int, tokens: float, dt: float = 0, dslot: int = 0) -> tuple[dict, int]:
        tok = int(tokens * 1e6)
        new_vt = self.vt + tok
        new_vq = self.k // new_vt
        q = self.vq - new_vq
        self.vq, self.vt, self.real = new_vq, new_vt, self.real + tok
        return self._event(user, False, q, tok, dt, dslot)

    def _event(self, user: int, is_buy: bool, q: int, tok: int, dt: float, dslot: int) -> tuple[dict, int]:
        from radar.sources.pump_events import decode_trade
        e = decode_trade(trade_bytes(self.mint, user, is_buy, q, tok, self.vq, self.vt, int(self.ts + dt), self.real))
        return e, self.slot + dslot
