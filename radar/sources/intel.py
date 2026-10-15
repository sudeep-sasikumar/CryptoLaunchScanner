"""Holder intelligence from GMGN-like services, each optional, each read into the same names.

  Codex          (CODEX_API_KEY; free plan, $1 one-time activation, 10k requests/month)
                 every chain incl. Robinhood: holders, top 10, snipers, bundlers, insiders,
                 suspicious wallets, dev - up to 200 tokens per request.
  Solana Tracker (SOLANATRACKER_API_KEY; free plan, 10k requests/month)
                 Solana: snipers, bundlers, insiders, top 10, dev, holders, LP burn, rug flag.
  GeckoTerminal  (no key; a free CoinGecko demo key raises the limit)
                 every chain: holder count, top-10 share, mint / freeze authority, honeypot, trust score.

Each lookup returns its provider's own fields (cx_* / st_* / gti_*) plus normalised ones (n_*) that
features.apply_intel() uses to fill the shared factor names. Free plans count requests per month,
so each provider has a daily budget, spent on the tokens closest to a call first.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("intel")
CODEX_URL = "https://graph.codex.io/graphql"
CODEX_NETWORK = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bsc": 56, "eth": 1}
CODEX_QUERY = """query($tokens: [String], $limit: Int) {
  filterTokens(tokens: $tokens, limit: $limit) {
    results {
      holders top10HoldersPercent
      sniperCount sniperHeldPercentage bundlerCount bundlerHeldPercentage
      insiderCount insiderHeldPercentage suspiciousCount suspiciousHeldPercentage devHeldPercentage
      token { address networkId }
    }
  }
}"""


def _f(x) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


class Budget:
    """Requests per UTC day, kept in the database so a restart does not reset it."""

    def __init__(self, db, name: str, per_day: int):
        self.db, self.key, self.per_day = db, f"budget:{name}", per_day

    def _today(self) -> tuple[str, int]:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        v = self.db.get(self.key) or {}
        return day, (v.get("n", 0) if v.get("day") == day else 0)

    def left(self) -> int:
        return max(0, self.per_day - self._today()[1])

    def spend(self, n: int = 1) -> None:
        day, used = self._today()
        self.db.put(self.key, {"day": day, "n": used + n})


class Codex:
    def __init__(self, http, cfg, api_key: str, db):
        self.http, self.key = http, api_key
        self.enabled = bool(cfg.enabled and api_key)
        self.lim = http.limiter("codex", 60 * cfg.max_per_sec)
        self.budget = Budget(db, "codex", cfg.calls_per_day)
        self.scale: float | None = None            # 1 if percentages arrive as 0-100, 100 if as 0-1

    async def lookup_many(self, tokens: list[tuple[str, str]]) -> dict[str, dict]:
        """{address: factors} for up to 200 (chain, address) pairs in one request."""
        ids = [f"{a}:{CODEX_NETWORK[c]}" for c, a in tokens if c in CODEX_NETWORK][:200]
        if not (self.enabled and ids and self.budget.left()):
            return {}
        self.budget.spend()
        r = await self.http.get_json(CODEX_URL, method="POST", limiter=self.lim,
                                     headers={"Authorization": self.key, "Content-Type": "application/json"},
                                     json_body={"query": CODEX_QUERY, "variables": {"tokens": ids, "limit": len(ids)}})
        if not isinstance(r, dict):
            return {}
        if r.get("errors"):
            log.warning("Codex answered with an error: %s", str(r["errors"])[:300])
        rows = (((r.get("data") or {}).get("filterTokens") or {}).get("results")) or []
        out = {}
        for row in rows:
            addr = ((row or {}).get("token") or {}).get("address")
            if addr:
                out[addr.lower()] = self.parse(row)
        return out

    def parse(self, row: dict) -> dict:
        top10 = _f(row.get("top10HoldersPercent"))
        if self.scale is None and top10 is not None and top10 > 0:
            # the docs do not say whether shares are 0-1 or 0-100; a new coin's top 10 always hold well over 1%
            self.scale = 100.0 if top10 <= 1.0 else 1.0
            log.info("Codex reports holder shares on a 0-%s scale", "1" if self.scale == 100 else "100")
        k = self.scale or 1.0

        def pct(name):
            v = _f(row.get(name))
            return None if v is None else v * k
        f = {"cx_ok": 1, "cx_holders": _f(row.get("holders")), "cx_top10_pct": pct("top10HoldersPercent"),
             "cx_sniper_n": _f(row.get("sniperCount")), "cx_sniper_pct": pct("sniperHeldPercentage"),
             "cx_bundler_n": _f(row.get("bundlerCount")), "cx_bundler_pct": pct("bundlerHeldPercentage"),
             "cx_insider_n": _f(row.get("insiderCount")), "cx_insider_pct": pct("insiderHeldPercentage"),
             "cx_suspicious_n": _f(row.get("suspiciousCount")), "cx_suspicious_pct": pct("suspiciousHeldPercentage"),
             "cx_dev_pct": pct("devHeldPercentage")}
        f.update({"n_holders": f["cx_holders"], "n_top10_pct": f["cx_top10_pct"], "n_dev_pct": f["cx_dev_pct"],
                  "n_insider_pct": f["cx_insider_pct"], "n_bundler_pct": f["cx_bundler_pct"],
                  "n_sniper_pct": f["cx_sniper_pct"]})
        return f


class SolanaTracker:
    def __init__(self, http, cfg, api_key: str, db):
        self.http, self.key = http, api_key
        self.enabled = bool(cfg.enabled and api_key)
        self.lim = http.limiter("solanatracker", 60 * cfg.max_per_sec)
        self.budget = Budget(db, "solanatracker", cfg.calls_per_day)

    async def lookup(self, chain: str, address: str) -> dict | None:
        if not (self.enabled and chain == "solana" and self.budget.left()):
            return None
        self.budget.spend()
        d = await self.http.get_json(f"https://data.solanatracker.io/tokens/{address}", limiter=self.lim,
                                     headers={"x-api-key": self.key}, retries=0)
        return parse_solanatracker(d) if isinstance(d, dict) else None


def parse_solanatracker(d: dict) -> dict | None:
    risk = d.get("risk")
    if not isinstance(risk, dict):
        return None
    pools = [p for p in d.get("pools") or [] if isinstance(p, dict)]
    pool = max(pools, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0), default={})
    sec = pool.get("security") or {}

    def grp(name, field):
        g = risk.get(name)
        return _f(g.get(field)) if isinstance(g, dict) else None
    f = {"st_ok": 1, "st_holders": _f(d.get("holders")), "st_top10_pct": _f(risk.get("top10")),
         "st_dev_pct": _f((risk.get("dev") or {}).get("percentage")),
         "st_sniper_n": grp("snipers", "count"), "st_sniper_pct": grp("snipers", "totalPercentage"),
         "st_bundler_n": grp("bundlers", "count"), "st_bundler_pct": grp("bundlers", "totalPercentage"),
         "st_bundler_initial_pct": grp("bundlers", "totalInitialPercentage"),
         "st_insider_n": grp("insiders", "count"), "st_insider_pct": grp("insiders", "totalPercentage"),
         "st_score": _f(risk.get("score")), "st_rugged": int(bool(risk.get("rugged"))),
         "st_risks_n": len(risk.get("risks") or []), "st_lp_burn_pct": _f(pool.get("lpBurn")),
         "st_mint_authority": None if not pool else int(bool(sec.get("mintAuthority"))),
         "st_freeze_authority": None if not pool else int(bool(sec.get("freezeAuthority")))}
    f.update({"n_holders": f["st_holders"], "n_top10_pct": f["st_top10_pct"], "n_dev_pct": f["st_dev_pct"],
              "n_insider_pct": f["st_insider_pct"], "n_bundler_pct": f["st_bundler_pct"],
              "n_sniper_pct": f["st_sniper_pct"]})
    return f


class GeckoInfo:
    """GeckoTerminal's token info: shares GeckoTerminal's rate limiter with pool discovery."""

    def __init__(self, gecko, cfg):
        self.gecko, self.enabled = gecko, bool(cfg.enabled)

    async def lookup(self, chain: str, address: str, max_backlog_s: float = 30.0) -> dict | None:
        if not self.enabled or self.gecko.busy(max_backlog_s):
            return None
        d = await self.gecko.http.get_json(f"{self.gecko.base}/networks/{chain}/tokens/{address}/info",
                                           limiter=self.gecko.lim, headers=self.gecko.headers, retries=0)
        a = ((d or {}).get("data") or {}).get("attributes") if isinstance(d, dict) else None
        return parse_gecko_info(a) if isinstance(a, dict) else None


def parse_gecko_info(a: dict) -> dict:
    h = a.get("holders") or {}
    dist = h.get("distribution_percentage") or {}

    def yesno(v):
        return None if v in (None, "", "unknown") else int(str(v).lower() in ("yes", "true", "1"))
    f = {"gti_ok": 1, "gti_holders": _f(h.get("count")), "gti_top10_pct": _f(dist.get("top_10")),
         "gti_score": _f(a.get("gt_score")), "gti_honeypot": yesno(a.get("is_honeypot")),
         "gti_mint_authority": yesno(a.get("mint_authority")), "gti_freeze_authority": yesno(a.get("freeze_authority")),
         "gti_has_x": int(bool(a.get("twitter_handle"))), "gti_has_tg": int(bool(a.get("telegram_handle")))}
    # its top 10 counts the bonding curve / liquidity pool as a holder (a fresh pump.fun coin reads ~100%),
    # so only the holder count stands in for the shared factors; the share stays a factor of its own
    f["n_holders"] = f["gti_holders"]
    return f
