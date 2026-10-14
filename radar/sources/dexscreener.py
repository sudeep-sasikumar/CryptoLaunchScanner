"""DexScreener (free, no key): batch price / volume / transaction snapshots for up to 30 tokens
per request on any chain, the latest paid token profiles, and USD prices for quote tokens."""
from __future__ import annotations

import time

from .pump_events import WSOL

BASE = "https://api.dexscreener.com"
SYSTEM = "11111111111111111111111111111111"            # pump.fun's marker for "quoted in native SOL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
PUMP = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
STABLES = {USDC: 6, USDT: 6}


class DexScreener:
    def __init__(self, http, calls_per_min: float):
        self.http = http
        self.lim = http.limiter("dexscreener", calls_per_min)
        self.profile_lim = http.limiter("dexscreener-profiles", 20)
        self.paid: dict[str, float] = {}               # "<chain>:<address>" -> first seen with a paid profile
        self.boosted: dict[str, float] = {}

    async def pairs(self, chain: str, addresses: list[str]) -> dict[str, dict]:
        """Best (deepest) pair for each token address. Addresses with no pair are absent."""
        out: dict[str, dict] = {}
        for i in range(0, len(addresses), 30):
            chunk = addresses[i:i + 30]
            data = await self.http.get_json(f"{BASE}/tokens/v1/{chain}/{','.join(chunk)}", limiter=self.lim)
            if not isinstance(data, list):
                continue
            wanted = {a.lower(): a for a in chunk}
            for p in data:
                addr = wanted.get(((p.get("baseToken") or {}).get("address") or "").lower())
                if addr is None:
                    continue
                liq = (p.get("liquidity") or {}).get("usd") or 0
                best = out.get(addr)
                if best is None or liq > ((best.get("liquidity") or {}).get("usd") or 0):
                    out[addr] = p
        return out

    async def poll_profiles(self) -> None:
        """Remember which tokens have just paid for a DexScreener profile or boost."""
        now = time.time()
        for path, store in (("token-profiles/latest/v1", self.paid), ("token-boosts/latest/v1", self.boosted)):
            data = await self.http.get_json(f"{BASE}/{path}", limiter=self.profile_lim)
            for p in data if isinstance(data, list) else []:
                key = f"{p.get('chainId')}:{p.get('tokenAddress')}"
                store.setdefault(key, now)
            for k in [k for k, t in store.items() if now - t > 86400]:
                del store[k]


class QuoteBook:
    """USD price and decimals of whatever a pump.fun coin is quoted in (SOL, USDC, PUMP, other coins)."""

    def __init__(self, http, dex: DexScreener, rpc_url: str, db):
        self.http, self.dex, self.rpc_url, self.db = http, dex, rpc_url, db
        self.decimals: dict[str, int] = {SYSTEM: 9, WSOL: 9, PUMP: 6, **STABLES, **db.get("quote_decimals", {})}
        self.usd: dict[str, tuple[float, float]] = {m: (1.0, float("inf")) for m in STABLES}
        self.active: dict[str, float] = {SYSTEM: float("inf")}       # quote mint -> last time a launch used it
        self.pending: set[str] = set()

    def kind(self, mint: str) -> int:
        return 0 if mint in (SYSTEM, WSOL, "") else 1 if mint in STABLES else 2 if mint == PUMP else 3

    def qdec(self, mint: str) -> int:
        """Decimals, guessing 6 (every pump.fun coin) until the RPC answer arrives."""
        d = self.decimals.get(mint or SYSTEM)
        if d is None:
            self.pending.add(mint)
            return 6
        return d

    def touch(self, mint: str, ts: float) -> None:
        self.active[mint or SYSTEM] = ts

    def price(self, mint: str, max_age: float = 300.0) -> float | None:
        v = self.usd.get(SYSTEM if mint in ("", WSOL) else mint)
        if v is None or time.time() - v[1] > max_age:
            return None
        return v[0]

    async def refresh(self) -> None:
        now = time.time()
        mints = [m for m, t in self.active.items() if now - t < 3600 and m not in STABLES]
        lookup = {(WSOL if m == SYSTEM else m): m for m in mints}
        pairs = await self.dex.pairs("solana", list(lookup))
        for addr, p in pairs.items():
            try:
                self.usd[lookup[addr]] = (float(p["priceUsd"]), now)
            except (KeyError, TypeError, ValueError):
                pass
        for mint in list(self.pending)[:5]:
            self.pending.discard(mint)
            r = await self.http.get_json(self.rpc_url, method="POST", json_body={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]})
            try:
                self.decimals[mint] = int(r["result"]["value"]["decimals"])
            except (KeyError, TypeError, ValueError):
                self.decimals[mint] = 6
            self.db.put("quote_decimals", {m: d for m, d in self.decimals.items() if m not in (SYSTEM, WSOL)})
