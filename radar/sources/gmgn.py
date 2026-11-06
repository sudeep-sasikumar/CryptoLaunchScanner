"""GMGN OpenAPI (openapi.gmgn.ai): the same numbers GMGN's token page shows, for every chain.

Two jobs:
  trenches  - the newest launches on every launchpad of a chain (PONS, LetsBonk, Meteora, four.meme,
              Clanker, Zora ...) seconds after creation: how launches outside pump.fun are found.
  token     - per token: holders, top 10, dev holding / closed, insiders (rat traders), bundlers,
              phishing (entrapment) wallets, snipers, bots, fresh wallets, smart money and KOLs,
              creator history, DexScreener payments, image / social reuse, contract safety, LP burn.

Needs a free API key (GMGN_API_KEY, from gmgn.ai/ai). Read-only, no request signing. The free plan
allows 5 weight per second. Each route's weight is what GMGN's own X-RateLimit-Remaining header showed
(2026-11-05), not the documented value, which is lower: token info 2, security 2, trenches 3, signals 1.
The client spends only `budget` of the plan, slows down when GMGN says the bucket is nearly empty and
sends nothing at all while a rate-limit pause runs (every request then would lengthen it, up to 5 min).
GMGN asks for IPv4, so this client binds to IPv4. Every string in a response (names, links) is written
by token creators: data only.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx

log = logging.getLogger("gmgn")
HOST = "https://openapi.gmgn.ai"
# quote currencies GMGN's own client sends per chain for the trenches feed
QUOTE_TYPES = {"sol": [4, 5, 3, 1, 13, 0], "bsc": [6, 7, 1, 16, 8, 3, 9, 10, 2, 17, 18, 0],
               "base": [11, 3, 12, 13, 0], "eth": [20, 11, 8, 3, 12, 1, 0], "robinhood": [11, 20, 24, 12, 0]}
CHAIN = {"solana": "sol", "robinhood": "robinhood", "bsc": "bsc", "base": "base", "eth": "eth"}   # ours -> GMGN's
OURS = {v: k for k, v in CHAIN.items()}
WEIGHT = {"/v1/token/info": 2, "/v1/token/security": 2, "/v1/trenches": 3, "/v1/market/token_signal": 1}
TRENCH_LISTS = ("new_creation", "near_completion", "completed")


def _f(x) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _pct(x) -> float | None:
    v = _f(x)
    return None if v is None else v * 100


class Gmgn:
    def __init__(self, cfg, api_key: str):
        self.cfg, self.key = cfg, api_key
        self.enabled = bool(cfg.enabled and api_key)
        # leaky bucket: `weight_per_sec` x `budget` units per second, one unit every `base_gap` seconds
        self.rate = max(float(cfg.weight_per_sec) * float(cfg.get("budget") or 0.7), 0.5)
        self.base_gap = 1.0 / self.rate
        self.gap = self.base_gap
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.banned_until = 0.0
        self.calls = self.errors = self.limited = 0
        self.remaining: float | None = None             # GMGN's bucket after the last answer
        self.last_limit: dict | None = None             # the last rate-limit answer: code, when, pause
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=6.0),
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1),     # IPv4 only
            headers={"X-APIKEY": api_key, "User-Agent": "LaunchRadar/1.0", "Content-Type": "application/json"})

    async def close(self) -> None:
        await self.client.aclose()

    def backlog_s(self) -> float:
        return max(0.0, self._next - time.monotonic(), self.banned_until - time.time())

    async def _wait(self, weight: int) -> None:
        """Take the next free slot in the pacing queue. A request whose slot falls inside a rate-limit pause
        queues again after the pause, so the waiting requests are spaced out instead of all firing the
        moment it ends (that burst is what earned the next pause)."""
        while True:
            ban = self.banned_until - time.time()
            if ban > 0:
                await asyncio.sleep(ban + 0.1)
                continue
            async with self._lock:
                now = time.monotonic()
                delay = self._next - now
                self._next = max(now, self._next) + self.gap * weight
            if delay > 0:
                await asyncio.sleep(delay)
            if time.time() >= self.banned_until:
                return

    async def _call(self, method: str, path: str, weight: int, query: dict | None = None, body: dict | None = None):
        if not self.enabled:
            return None
        weight = WEIGHT.get(path, weight)
        await self._wait(weight)
        q = dict(query or {}, timestamp=int(time.time()), client_id=str(uuid.uuid4()))
        self.calls += 1
        try:
            r = await self.client.request(method, HOST + path, params=q, json=body)
        except (httpx.HTTPError, OSError) as e:
            self.errors += 1
            log.debug("%s failed: %s", path, e)
            return None
        left = _f(r.headers.get("x-ratelimit-remaining"))
        if left is not None:
            self.remaining = left
        if r.status_code == 429:
            self.limited += 1
            try:
                body_j = r.json()
            except ValueError:
                body_j = {}
            body_j = body_j if isinstance(body_j, dict) else {}
            reset = _f(body_j.get("reset_at")) or _f(r.headers.get("x-ratelimit-reset"))
            until = reset if reset and reset > time.time() else time.time() + 10
            code = str(body_j.get("error") or body_j.get("code") or 429)[:40]
            if until > self.banned_until + 1:
                log.warning("GMGN rate limit (%s) on %s - no GMGN calls for %.0f s", code, path, until - time.time())
            self.banned_until = max(self.banned_until, until)
            self._next = max(self._next, time.monotonic() + (until - time.time()))   # queue restarts after it
            self.last_limit = {"ts": time.time(), "path": path, "code": code, "pause_s": round(until - time.time(), 1)}
            self.gap = min(self.gap * 1.5, 4 * self.base_gap)   # and slow down for a while
            return None
        self.gap = max(self.base_gap, self.gap * 0.98)
        if left is not None and left < 3:
            # GMGN says its bucket is nearly empty: let it refill before the next request
            async with self._lock:
                self._next = max(self._next, time.monotonic() + (3 - left) / float(self.cfg.weight_per_sec))
        try:
            j = r.json()
        except ValueError:
            self.errors += 1
            return None
        if r.status_code >= 400 or (isinstance(j, dict) and j.get("code") not in (0, None)):
            self.errors += 1
            log.debug("%s -> %s %s", path, r.status_code, str(j)[:200])
            return None
        return j.get("data") if isinstance(j, dict) and "data" in j else j

    # ---------------------------------------------------------------- discovery
    async def trenches(self, chain: str, platforms: list[str], limit: int) -> dict[str, list[dict]]:
        """GMGN's three launch lists for the given launchpads of one chain, in one request: new_creation
        (the newest ~60, about 5 minutes of pump.fun), near_completion (close to graduating) and completed
        (graduated). Every item carries GMGN's holder, wallet-label and creator figures at this moment."""
        section = {"filters": ["offchain", "onchain"], "launchpad_platform_v2": True, "limit": limit}
        if platforms:
            section["launchpad_platform"] = platforms
        if chain in QUOTE_TYPES:
            section["quote_address_type"] = QUOTE_TYPES[chain]
        data = await self._call("POST", "/v1/trenches", 3, {"chain": chain},
                                {"version": "v2", **{k: dict(section) for k in TRENCH_LISTS}})
        if not isinstance(data, dict):
            return {}
        data = {**data, "near_completion": data.get("near_completion") or data.get("pump") or []}  # docs call it "pump"
        return {k: [i for i in data.get(k) or [] if isinstance(i, dict) and i.get("address")] for k in TRENCH_LISTS}

    async def signals(self, chain: str, groups: list[dict]) -> list[dict]:
        """GMGN's signal feed (smart money buys, KOL buys, price spikes, DexScreener ads, CTO ...), newest
        first, at most 50 per group."""
        data = await self._call("POST", "/v1/market/token_signal", 1, None, {"chain": chain, "groups": groups})
        items = data if isinstance(data, list) else (data or {}).get("list") if isinstance(data, dict) else None
        return [i for i in items or [] if isinstance(i, dict) and i.get("token_address") and i.get("id")]

    # ---------------------------------------------------------------- per token
    async def token(self, chain: str, address: str, with_security: bool = True) -> dict | None:
        """GMGN's token page as factors (gm_*), or None when GMGN does not know the token yet."""
        c = CHAIN.get(chain, chain)
        info = await self._call("GET", "/v1/token/info", 1, {"chain": c, "address": address})
        if not isinstance(info, dict) or not info.get("symbol"):
            return None                      # an empty info block means GMGN has no record (per GMGN's own docs)
        sec = await self._call("GET", "/v1/token/security", 1, {"chain": c, "address": address}) if with_security else None
        return parse_token(info, sec if isinstance(sec, dict) else None)


def parse_token(info: dict, sec: dict | None) -> dict:
    """Map GMGN's token info / security into factor names. Rates come as 0-1; stored as percent."""
    st, tags, dev = info.get("stat") or {}, info.get("wallet_tags_stat") or {}, info.get("dev") or {}
    link, price = info.get("link") or {}, info.get("price") or {}
    created = _f(st.get("creator_created_count"))
    opened = _f(dev.get("creator_open_count"))
    f = {
        "gm_ok": 1,
        "gm_holders": _f(st.get("holder_count") or info.get("holder_count")),
        "gm_top10_pct": _pct(st.get("top_10_holder_rate")),
        "gm_dev_hold_pct": _pct(st.get("dev_team_hold_rate")),
        "gm_creator_hold_pct": _pct(st.get("creator_hold_rate")),
        "gm_dev_closed": None if not dev.get("creator_token_status")
        else int(dev.get("creator_token_status") in ("creator_close", "sell", "close")),
        "gm_insider_pct": _pct(st.get("top_rat_trader_percentage")),
        "gm_bundler_pct": _pct(st.get("top_bundler_trader_percentage")),
        "gm_phishing_pct": _pct(st.get("top_entrapment_trader_percentage")),
        "gm_sniper_hold_pct": _pct(st.get("top70_sniper_hold_rate")),
        "gm_bot_pct": _pct(st.get("bot_degen_rate")),
        "gm_fresh_pct": _pct(st.get("fresh_wallet_rate")),
        "gm_vault_pct": _pct(st.get("private_vault_hold_rate")),
        "gm_smart_n": _f(tags.get("smart_wallets")),
        "gm_kol_n": _f(tags.get("renowned_wallets")),
        "gm_sniper_n": _f(tags.get("sniper_wallets")),
        "gm_insider_n": _f(tags.get("rat_trader_wallets")),
        "gm_bundler_n": _f(tags.get("bundler_wallets")),
        "gm_fresh_n": _f(tags.get("fresh_wallets")),
        "gm_whale_n": _f(tags.get("whale_wallets")),
        "gm_signal_n": _f(st.get("signal_count")),
        "gm_calls_n": _f(st.get("degen_call_count")),
        "gm_creator_created_n": created,
        "gm_creator_open_n": opened,
        "gm_creator_open_ratio": (opened / created) if created and opened is not None else None,
        "gm_creator_x_tokens_n": _f(dev.get("twitter_create_token_count")),
        "gm_creator_x_deleted_n": _f(dev.get("twitter_del_post_token_count")),
        "gm_dev_funded_age_s": (time.time() - _f(dev["fund_from_ts"])) if _f(dev.get("fund_from_ts")) else None,
        "gm_cto": _f(dev.get("cto_flag")),
        "gm_dex_paid": int(any(_f(dev.get(k)) for k in ("dexscr_ad", "dexscr_update_link", "dexscr_trending_bar"))),
        "gm_dex_boost": _f(dev.get("dexscr_boost_fee")),
        "gm_image_dup_n": _f(info.get("image_dup_count")),
        "gm_visits": _f(info.get("visiting_count")),
        "gm_progress_pct": _pct(info.get("launchpad_progress")),
        "gm_liq_usd": _f(info.get("liquidity")),
        "gm_has_x": int(bool(link.get("twitter_username"))),
        "gm_has_tg": int(bool(link.get("telegram"))),
        "gm_has_web": int(bool(link.get("website"))),
        "gm_buys_5m": _f(price.get("buys_5m")), "gm_sells_5m": _f(price.get("sells_5m")),
        "gm_buy_vol_5m": _f(price.get("buy_volume_5m")), "gm_sell_vol_5m": _f(price.get("sell_volume_5m")),
        "gm_hot": _f(price.get("hot_level")),
        "gm_creator_best_mc": _best_other(dev, info.get("address")),
        "_gm_launchpad": str(info.get("launchpad_platform") or info.get("launchpad") or "")[:40],
        "_gm_creator": str(dev.get("creator_address") or "")[:64],
    }
    if sec:
        lock = sec.get("lock_summary") or {}
        locked = sum(_f(d.get("percent")) or 0 for d in lock.get("lock_detail") or [] if isinstance(d, dict))
        f.update({
            "gm_sec_ok": 1,
            "gm_honeypot": None if sec.get("honeypot") in (None, "") and sec.get("is_honeypot") is None
            else int(bool(sec.get("is_honeypot") or _f(sec.get("honeypot")) or _f(sec.get("can_not_sell")))),
            "gm_blacklist": None if sec.get("is_blacklist") is None else int(bool(sec.get("is_blacklist"))),
            "gm_open_source": None if sec.get("is_open_source") is None else int(bool(sec.get("is_open_source"))),
            "gm_renounced": None if sec.get("is_renounced") is None else int(bool(sec.get("is_renounced"))),
            "gm_renounced_mint": None if sec.get("renounced_mint") is None else int(bool(sec.get("renounced_mint"))),
            "gm_renounced_freeze": None if sec.get("renounced_freeze_account") is None
            else int(bool(sec.get("renounced_freeze_account"))),
            "gm_tax_max": max(_pct(sec.get("buy_tax")) or 0, _pct(sec.get("sell_tax")) or 0)
            if sec.get("buy_tax") not in (None, "") or sec.get("sell_tax") not in (None, "") else None,
            "gm_burnt_pct": max(_pct(sec.get("burn_ratio")) or 0, locked * 100,
                                100.0 if sec.get("burn_status") == "burn" else 0.0),
            "gm_flags_n": len(sec.get("flags") or []),
            "gm_insider_hold_pct": _pct(sec.get("suspected_insider_hold_rate")),
            "gm_sniper_count": _f(sec.get("sniper_count")),
        })
        if sec.get("rug_ratio") not in (None, ""):
            f["gm_rug_ratio"] = _f(sec.get("rug_ratio"))
        if sec.get("is_wash_trading") is not None:
            f["gm_wash"] = int(bool(sec.get("is_wash_trading")))
    return f


def _best_other(dev: dict, address) -> float | None:
    """Peak market cap of the creator's best coin - only when that is an earlier coin, never this one
    (this coin's own peak is the outcome being predicted)."""
    ath = dev.get("ath_token_info") or {}
    if not isinstance(ath, dict) or not ath.get("ath_token") or str(ath.get("ath_token")) == str(address or ""):
        return None
    return _f(ath.get("ath_mc"))


def trench_stats(item: dict, chain: str) -> dict:
    """GMGN's figures on a launch-list item, under the same gm_* names token info uses, so a token seen in
    the lists needs no token-info request. Rates are 0-1 there too; stored as percent."""
    created, opened = _f(item.get("creator_created_count")), _f(item.get("creator_created_open_count"))
    status = item.get("creator_token_status")
    f = {
        "gm_ok": 1,
        "gm_holders": _f(item.get("holder_count")),
        "gm_top10_pct": _pct(item.get("top_10_holder_rate")),
        "gm_dev_hold_pct": _pct(item.get("dev_team_hold_rate")),
        "gm_creator_hold_pct": _pct(item.get("creator_balance_rate")),
        "gm_dev_closed": None if not status else int(status in ("creator_close", "sell", "close")),
        "gm_insider_pct": _pct(item.get("rat_trader_amount_rate")),
        "gm_bundler_pct": _pct(item.get("bundler_trader_amount_rate")),
        "gm_phishing_pct": _pct(item.get("entrapment_ratio")),
        "gm_sniper_hold_pct": _pct(item.get("top70_sniper_hold_rate")),
        "gm_insider_hold_pct": _pct(item.get("suspected_insider_hold_rate")),
        "gm_bot_pct": _pct(item.get("bot_degen_rate")),
        "gm_fresh_pct": _pct(item.get("fresh_wallet_rate")),
        "gm_vault_pct": _pct(item.get("private_vault_hold_rate")),
        "gm_smart_n": _f(item.get("smart_degen_count")),
        "gm_kol_n": _f(item.get("renowned_count")),
        "gm_creator_created_n": created,
        "gm_creator_open_n": opened,
        "gm_creator_open_ratio": (opened / created) if created and opened is not None else None,
        "gm_creator_x_deleted_n": _f(item.get("twitter_del_post_token_count")),
        "gm_dev_funded_age_s": (time.time() - _f(item["fund_from_ts"])) if _f(item.get("fund_from_ts")) else None,
        "gm_cto": _f(item.get("cto_flag")),
        "gm_dex_paid": int(any(_f(item.get(k)) for k in ("dexscr_ad", "dexscr_update_link", "dexscr_trending_bar"))),
        "gm_dex_boost": _f(item.get("dexscr_boost_fee")),
        "gm_progress_pct": _pct(item.get("progress")),
        "gm_liq_usd": _f(item.get("liquidity")),
        "gm_rug_ratio": _f(item.get("rug_ratio")),
        "gm_wash": None if item.get("is_wash_trading") is None else int(bool(item.get("is_wash_trading"))),
        "gm_has_x": int(bool(item.get("twitter"))),
        "gm_has_tg": int(bool(item.get("telegram"))),
        "gm_has_web": int(bool(item.get("website"))),
        "_gm_creator": str(item.get("creator") or "")[:64],
    }
    if chain == "sol" and item.get("renounced_mint") is not None and item.get("renounced_freeze_account") is not None:
        # Solana's contract check is mint and freeze authority (no honeypots there): the list answers it
        rm, rf = _f(item.get("renounced_mint")), _f(item.get("renounced_freeze_account"))
        if rm is not None and rf is not None:
            f.update({"gm_sec_ok": 1, "gm_renounced_mint": int(bool(rm)), "gm_renounced_freeze": int(bool(rf)),
                      "gm_tax_max": max(_pct(item.get("buy_tax")) or 0, _pct(item.get("sell_tax")) or 0),
                      "gm_burnt_pct": 100.0 if item.get("burn_status") == "burn" else None})
    return {k: v for k, v in f.items() if v is not None}


def signal_row(item: dict) -> dict:
    """One signal event as GMGN saw it when it fired - nothing measured later (current market cap, ATH)."""
    return {"id": str(item.get("id"))[:64], "address": str(item.get("token_address"))[:64],
            "type": int(_f(item.get("signal_type")) or 0), "trigger_at": _f(item.get("trigger_at")),
            "trigger_mc": _f(item.get("trigger_mc")), "first_mc": _f(item.get("first_trigger_mc")),
            "times": _f(item.get("signal_times"))}


def from_trenches(item: dict) -> dict:
    """What the launch feed already tells us about a token (socials and reuse change little later)."""
    return {
        "gm_x_followers": _f(item.get("x_user_follower")),
        "gm_tg_calls": _f(item.get("tg_call_count")),
        "gm_callouts": _f(item.get("callout_count")),
        "gm_twitter_dup_n": _f(item.get("twitter_dup")),
        "gm_telegram_dup_n": _f(item.get("telegram_dup")),
        "gm_website_dup_n": _f(item.get("website_dup")),
        "gm_image_dup_feed_n": _f(item.get("image_dup")),
        "gm_x_is_post": _f(item.get("twitter_is_tweet")),
        "gm_x_renamed_n": _f(item.get("twitter_rename_count")),
        "gm_rug_ratio": _f(item.get("rug_ratio")),
        "gm_wash": None if item.get("is_wash_trading") is None else int(bool(item.get("is_wash_trading"))),
        "gm_creator_open_ratio_feed": _f(item.get("creator_created_open_ratio")),
    }
