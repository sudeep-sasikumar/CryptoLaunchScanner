"""Contract and holder checks from free third-party scanners.

GoPlus   - EVM chains (Robinhood, BNB, Base ...): honeypot, taxes, owner powers, LP lock, holders.
RugCheck - Solana: risk flags, real on-chain top holders (transfers included), insider networks.

Both lag a brand-new token by a minute or two, so "no answer yet" is normal and stays None.
"""
from __future__ import annotations

GOPLUS_CHAIN = {"eth": "1", "bsc": "56", "base": "8453", "robinhood": "4663", "monad": "143", "arc": "5042",
                "abstract": "2741"}
DEAD = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead", ""}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _flag(d: dict, k: str) -> int | None:
    v = d.get(k)
    return None if v in (None, "") else int(str(v) == "1")


class Security:
    def __init__(self, http, cfg):
        self.http = http
        self.go_on, self.rc_on = cfg.goplus.enabled, cfg.rugcheck.enabled
        self.go_lim = http.limiter("goplus", cfg.goplus.calls_per_min)
        self.rc_lim = http.limiter("rugcheck", cfg.rugcheck.calls_per_min)

    async def goplus(self, chain: str, address: str) -> dict | None:
        cid = GOPLUS_CHAIN.get(chain)
        if not (self.go_on and cid):
            return None
        data = await self.http.get_json(f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
                                        params={"contract_addresses": address}, limiter=self.go_lim)
        try:
            d = next(iter(data["result"].values()))
        except (KeyError, TypeError, StopIteration, AttributeError):
            return None
        buy, sell = _f(d.get("buy_tax")), _f(d.get("sell_tax"))
        owner = (d.get("owner_address") or "").lower()
        out = {
            "sec_checked": 1,
            "sec_open_source": _flag(d, "is_open_source"),
            "sec_proxy": _flag(d, "is_proxy"),
            "sec_mintable": _flag(d, "is_mintable"),
            "sec_blacklist": _flag(d, "is_blacklisted"),
            "sec_honeypot": max(_flag(d, "is_honeypot") or 0, _flag(d, "cannot_sell_all") or 0,
                                _flag(d, "cannot_buy") or 0) if any(
                d.get(k) not in (None, "") for k in ("is_honeypot", "cannot_sell_all", "cannot_buy")) else None,
            "sec_tax_buy": buy * 100 if buy is not None else None,
            "sec_tax_sell": sell * 100 if sell is not None else None,
            "sec_tax_max": max(buy or 0, sell or 0) * 100 if (buy is not None or sell is not None) else None,
            "sec_tax_modifiable": _flag(d, "slippage_modifiable"),
            "sec_pausable": _flag(d, "transfer_pausable"),
            "sec_hidden_owner": _flag(d, "hidden_owner"),
            "sec_renounced": int(owner in DEAD) if "owner_address" in d and d.get("is_open_source") == "1" else None,
            "sec_holders": _f(d.get("holder_count")),
            "sec_creator_pct": (_f(d.get("creator_percent")) or 0) * 100 if d.get("creator_percent") else None,
            "_creator": d.get("creator_address") or "",
        }
        holders = [h for h in d.get("holders") or [] if not h.get("is_contract")]
        if d.get("holders"):
            out["sec_top10_pct"] = sum(_f(h.get("percent")) or 0 for h in holders[:10]) * 100
        lp = d.get("lp_holders") or []
        if lp:
            out["sec_lp_locked_pct"] = sum(
                _f(h.get("percent")) or 0 for h in lp
                if h.get("is_locked") == 1 or (h.get("address") or "").lower() in DEAD) * 100
        return out

    async def rugcheck(self, mint: str) -> dict | None:
        if not self.rc_on:
            return None
        d = await self.http.get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", limiter=self.rc_lim,
                                     timeout=6.0, retries=0)
        if not isinstance(d, dict) or "risks" not in d and "score" not in d:
            return None
        risks = d.get("risks") or []
        top = d.get("topHolders") or []
        return {
            "rc_score": _f(d.get("score_normalised")),
            "rc_rugged": int(bool(d.get("rugged")) or any("rug" in (r.get("name") or "").lower() for r in risks)),
            "rc_risks_n": len(risks),
            "rc_danger_n": sum(1 for r in risks if r.get("level") == "danger"),
            "rc_holders": _f(d.get("totalHolders")),
            "rc_insiders_n": _f(d.get("graphInsidersDetected")),
            "rc_insider_holders_n": sum(1 for h in top if h.get("insider")),
            "rc_top10_pct": sum(_f(h.get("pct")) or 0 for h in top[1:11]) if len(top) > 1 else None,
            "rc_creator_tokens_n": len(d.get("creatorTokens") or []),
            "rc_liq_usd": _f(d.get("totalMarketLiquidity")),
            "_risks": [r.get("name") for r in risks][:5],
        }
