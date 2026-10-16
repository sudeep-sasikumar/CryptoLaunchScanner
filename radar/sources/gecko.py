"""GeckoTerminal (free): the newest pools on each network - how launches outside pump.fun are
discovered - and the trade list of a pool (wallets, block numbers) for bundle / sniper factors.

The free limit is small, so discovery polls always go first and trade lists are fetched only
while the queue is short. With COINGECKO_DEMO_API_KEY the same data comes through CoinGecko's
on-chain API at a higher limit.
"""
from __future__ import annotations

import calendar
import logging
import time

log = logging.getLogger("gecko")


def canonical_launchpad(raw: str, names: list) -> str | None:
    """One name per launchpad whichever source reported it (see `launchpad_names` in config.yaml)."""
    low = (raw or "").lower()
    for needle, name in names:
        if str(needle).lower() in low:
            return str(name)
    return None

PUBLIC = "https://api.geckoterminal.com/api/v2"
DEMO = "https://api.coingecko.com/api/v3/onchain"


class Gecko:
    def __init__(self, http, cfg, demo_key: str = "", names: list | None = None):
        self.http, self.cfg, self.names = http, cfg, names or []
        self.base = DEMO if demo_key else PUBLIC
        self.headers = {"x-cg-demo-api-key": demo_key} if demo_key else None
        self.lim = http.limiter("gecko", cfg.calls_per_min)
        self.seen_pools: dict[str, float] = {}

    def launchpad(self, dex_id: str) -> str | None:
        return canonical_launchpad(dex_id, self.names)

    async def new_pools(self, network: str) -> list[dict]:
        """Pools not returned before: [{chain, pool, dex, launchpad, address, name, symbol, created_ts, quote}]"""
        net = self.cfg.networks[network]
        data = await self.http.get_json(f"{self.base}/networks/{network}/new_pools", limiter=self.lim,
                                        params={"include": "base_token,quote_token", "page": 1}, headers=self.headers)
        if not isinstance(data, dict) or "data" not in data:
            return []
        tokens = {t["id"]: t.get("attributes") or {} for t in data.get("included") or [] if t.get("type") == "token"}
        now, out = time.time(), []
        for p in data["data"]:
            pid = p.get("id", "")
            if pid in self.seen_pools:
                continue
            self.seen_pools[pid] = now
            a, rel = p.get("attributes") or {}, p.get("relationships") or {}
            dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
            pad = self.launchpad(dex)
            if dex in (net.get("skip_dexes") or []) or (net.get("only_launchpads") and pad is None):
                continue
            base = tokens.get(((rel.get("base_token") or {}).get("data") or {}).get("id", ""), {})
            quote = tokens.get(((rel.get("quote_token") or {}).get("data") or {}).get("id", ""), {})
            if not str(base.get("address") or "").isalnum() or not str(a.get("address") or "").isalnum():
                continue                                # addresses end up in links: accept only plain ones
            try:
                created = float(calendar.timegm(time.strptime(a["pool_created_at"], "%Y-%m-%dT%H:%M:%SZ")))
            except (KeyError, TypeError, ValueError):
                created = now
            if now - created > 1800:                    # an old pool resurfacing is not a launch
                continue
            out.append({"chain": network, "pool": a.get("address", ""), "dex": dex, "launchpad": pad or dex,
                        "address": base["address"], "name": base.get("name") or "", "symbol": base.get("symbol") or "",
                        "created_ts": created, "quote": quote.get("symbol") or ""})
        for k in [k for k, t in self.seen_pools.items() if now - t > 7200]:
            del self.seen_pools[k]
        return out

    async def reserves(self, network: str, addresses: list[str]) -> dict[str, float] | None:
        """Total USD held across each token's pools right now, keyed by lower-case address (up to 30
        per request). A token GeckoTerminal no longer lists maps to 0. None = the lookup failed."""
        data = await self.http.get_json(f"{self.base}/networks/{network}/tokens/multi/{','.join(addresses)}",
                                        limiter=self.lim, headers=self.headers)
        if not isinstance(data, dict) or "data" not in data:
            return None
        out = {a.lower(): 0.0 for a in addresses}
        for d in data["data"]:
            a = d.get("attributes") or {}
            try:
                out[str(a.get("address", "")).lower()] = float(a.get("total_reserve_in_usd") or 0)
            except (TypeError, ValueError):
                pass
        return out

    def busy(self, max_backlog_s: float = 20.0) -> bool:
        return self.lim.backlog_s() > max_backlog_s

    async def trades(self, network: str, pool: str) -> list[dict]:
        data = await self.http.get_json(f"{self.base}/networks/{network}/pools/{pool}/trades", limiter=self.lim,
                                        headers=self.headers)
        return data.get("data") or [] if isinstance(data, dict) else []
