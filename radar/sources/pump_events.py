"""Decode pump.fun program events (Anchor/Borsh) from the "Program data:" log lines.

Layouts follow the official IDL (github.com/pump-fun/pump-public-docs, idl/pump.json).
The program appends fields over time, so decoding is tolerant in both directions: bytes
beyond the known layout are ignored, and an event that ends early keeps the fields read
so far (the leading fields - mint, amounts, user, reserves - have never moved).
"""
from __future__ import annotations

import base64
import struct

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_DECIMALS = 6

DISC_TRADE = bytes.fromhex("bddb7fd34ee661ee")
DISC_CREATE = bytes.fromhex("1b72a94ddeeb6376")
DISC_COMPLETE = bytes.fromhex("5f72619cd42e9808")

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


class _Reader:
    def __init__(self, data: bytes, pos: int = 8):
        self.d, self.o = data, pos

    def take(self, n: int) -> bytes:
        if self.o + n > len(self.d):
            raise EOFError
        b = self.d[self.o:self.o + n]
        self.o += n
        return b

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def u8(self) -> int:
        return self.take(1)[0]

    def boolean(self) -> bool:
        return self.take(1)[0] != 0

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def string(self) -> str:
        n = struct.unpack("<I", self.take(4))[0]
        if n > 4096:
            raise EOFError
        return self.take(n).decode("utf-8", "replace")


def _read(r: _Reader, out: dict, fields: tuple) -> None:
    """Read (name, kind) fields in order into out; stop quietly where the event ends."""
    try:
        for name, kind in fields:
            out[name] = getattr(r, kind)()
    except EOFError:
        pass


_TRADE_HEAD = (("mint", "pubkey"), ("sol_amount", "u64"), ("token_amount", "u64"), ("is_buy", "boolean"),
               ("user", "pubkey"), ("timestamp", "i64"), ("virtual_sol_reserves", "u64"),
               ("virtual_token_reserves", "u64"), ("real_sol_reserves", "u64"), ("real_token_reserves", "u64"))
_TRADE_MID = (("fee_recipient", "pubkey"), ("fee_basis_points", "u64"), ("fee", "u64"), ("creator", "pubkey"),
              ("creator_fee_basis_points", "u64"), ("creator_fee", "u64"), ("track_volume", "boolean"),
              ("total_unclaimed_tokens", "u64"), ("total_claimed_tokens", "u64"), ("current_sol_volume", "u64"),
              ("last_update_timestamp", "i64"), ("ix_name", "string"), ("mayhem_mode", "boolean"),
              ("cashback_fee_basis_points", "u64"), ("cashback", "u64"), ("buyback_fee_basis_points", "u64"),
              ("buyback_fee", "u64"))
_TRADE_TAIL = (("quote_mint", "pubkey"), ("quote_amount", "u64"), ("virtual_quote_reserves", "u64"),
               ("real_quote_reserves", "u64"), ("holder_rewards_bps", "u64"), ("holder_rewards", "u64"),
               ("creator_fee_unclaimed", "u64"))
_CREATE = (("name", "string"), ("symbol", "string"), ("uri", "string"), ("mint", "pubkey"),
           ("bonding_curve", "pubkey"), ("user", "pubkey"), ("creator", "pubkey"), ("timestamp", "i64"),
           ("virtual_token_reserves", "u64"), ("virtual_sol_reserves", "u64"), ("real_token_reserves", "u64"),
           ("token_total_supply", "u64"), ("token_program", "pubkey"), ("is_mayhem_mode", "boolean"),
           ("is_cashback_enabled", "boolean"), ("quote_mint", "pubkey"), ("virtual_quote_reserves", "u64"),
           ("creator_fee_bps", "u64"), ("is_holder_reward", "boolean"), ("depth", "u8"))
_COMPLETE = (("user", "pubkey"), ("mint", "pubkey"), ("bonding_curve", "pubkey"), ("timestamp", "i64"),
             ("quote_mint", "pubkey"))


def decode_trade(d: bytes) -> dict | None:
    out: dict = {}
    r = _Reader(d)
    _read(r, out, _TRADE_HEAD)
    if "real_token_reserves" not in out:
        return None
    _read(r, out, _TRADE_MID)
    if "buyback_fee" in out:
        try:                                   # shareholders: vec<(pubkey, u16)>
            n = struct.unpack("<I", r.take(4))[0]
            r.take(34 * n)
        except EOFError:
            return out
        _read(r, out, _TRADE_TAIL)
    return out


def decode_create(d: bytes) -> dict | None:
    out: dict = {}
    _read(_Reader(d), out, _CREATE)
    return out if "user" in out else None


def decode_complete(d: bytes) -> dict | None:
    out: dict = {}
    _read(_Reader(d), out, _COMPLETE)
    return out if "mint" in out else None


def decode_logs(logs: list[str]) -> list[tuple[str, dict]]:
    """All create / trade / complete events in the log lines of one transaction, in order."""
    events = []
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            d = base64.b64decode(line[14:])
        except ValueError:
            continue
        head = d[:8]
        if head == DISC_TRADE:
            kind, e = "trade", decode_trade(d)
        elif head == DISC_CREATE:
            kind, e = "create", decode_create(d)
        elif head == DISC_COMPLETE:
            kind, e = "complete", decode_complete(d)
        else:
            continue
        if e:
            events.append((kind, e))
    return events
