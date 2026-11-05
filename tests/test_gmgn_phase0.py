"""GMGN used within its real rate limit (measured weights, its own remaining-budget header, a full stop
during a pause), its three launch lists standing in for per-token requests, its signal feed, and the
pump.fun stream moving off a private node that stopped delivering. No network: GMGN is simulated."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from radar.engine import Engine
from radar.features import apply_gmgn
from radar.sources.dexscreener import SYSTEM
from radar.sources.gmgn import Gmgn, signal_row, trench_stats
from radar.sources.pump_stream import PumpStream, _redact, host

from .helpers import Curve, addr, cfg
from .test_engine import _launch

# a pump.fun item from GMGN's near_completion list (recorded 2026-11-05, trimmed)
ITEM = {"address": "7PriNMSQarKxpDcHHVKBkVh2AyK6rNZmdFF5D6Copump", "rat_trader_amount_rate": 0.0249,
        "bundler_trader_amount_rate": 0.5538, "entrapment_ratio": 0.2063, "top70_sniper_hold_rate": 0.168382692,
        "suspected_insider_hold_rate": 0, "top_10_holder_rate": 0.2826, "dev_team_hold_rate": 0.168382692,
        "creator_balance_rate": 0, "bot_degen_rate": 0.6918, "fresh_wallet_rate": 0.12, "rug_ratio": 0,
        "is_wash_trading": False, "progress": 0.980657251569715, "creator_created_count": 4109,
        "creator_created_open_count": 22, "creator_token_status": "creator_close", "renounced_mint": "1",
        "renounced_freeze_account": "1", "burn_status": "burn", "smart_degen_count": 17, "renowned_count": 0,
        "holder_count": 221, "liquidity": 18984.52, "twitter": "x", "creator": "Dev1"}


def test_launch_list_item_reads_like_token_info():
    g = trench_stats(ITEM, "sol")
    assert round(g["gm_bundler_pct"], 2) == 55.38 and round(g["gm_phishing_pct"], 2) == 20.63
    assert g["gm_smart_n"] == 17 and g["gm_kol_n"] == 0 and g["gm_holders"] == 221
    assert round(g["gm_creator_open_ratio"], 4) == 0.0054 and g["gm_dev_closed"] == 1
    assert g["gm_sec_ok"] == 1 and g["gm_renounced_mint"] == 1 and g["gm_burnt_pct"] == 100.0
    assert "gm_honeypot" not in g                                   # not a Solana idea: not claimed
    assert "gm_sec_ok" not in trench_stats({**ITEM, "address": "0xabc"}, "bsc")   # EVM still needs the check
    f = {"chain": "solana"}
    apply_gmgn(f, {**g, "_gm_ts": time.time() - 12}, None, own_trades=True)
    assert f["sec_checked"] == 1 and 11 <= f["gm_age_s"] <= 13


def test_signal_keeps_only_what_was_known_when_it_fired():
    r = signal_row({"id": "s1", "token_address": "Mint1", "signal_type": 12, "trigger_at": 100, "trigger_mc": 30000,
                    "first_trigger_mc": 25000, "signal_times": 2, "market_cap": 900000, "ath": 2000000})
    assert r == {"id": "s1", "address": "Mint1", "type": 12, "trigger_at": 100, "trigger_mc": 30000, "first_mc": 25000,
                 "times": 2}


def _gmgn(handler) -> Gmgn:
    g = Gmgn(cfg().sources.gmgn, "test-key")
    g.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return g


async def test_rate_limit_pause_stops_every_request_and_low_budget_slows_down():
    hits = []

    def handler(req):
        hits.append(time.time())
        if len(hits) == 1:
            return httpx.Response(429, json={"code": 429, "error": "RATE_LIMIT_EXCEEDED", "reset_at": time.time() + 0.6},
                                  headers={"x-ratelimit-remaining": "0"})
        return httpx.Response(200, json={"code": 0, "data": {"symbol": "X"}}, headers={"x-ratelimit-remaining": "1"})

    g = _gmgn(handler)
    assert await g._call("GET", "/v1/token/info", 1) is None
    assert g.limited == 1 and g.last_limit["code"] == "RATE_LIMIT_EXCEEDED" and g.banned_until > time.time()
    t0 = time.time()
    assert (await g._call("GET", "/v1/token/info", 1))["symbol"] == "X"
    assert hits[1] - t0 >= 0.4                                      # nothing was sent during the pause
    assert g.remaining == 1 and g._next - time.monotonic() > 0.2    # bucket nearly empty: next request waits
    await g.close()


async def test_three_lists_requested_in_one_call():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"code": 0, "data": {"new_creation": [ITEM], "pump": [], "completed": []}})

    g = _gmgn(handler)
    lists = await g.trenches("sol", ["Pump.fun"], 80)
    assert set(seen) >= {"new_creation", "near_completion", "completed"}
    assert [i["address"] for i in lists["new_creation"]] == [ITEM["address"]] and lists["near_completion"] == []
    await g.close()


def test_stream_moves_off_a_dead_private_node_and_back_later():
    s = PumpStream("wss://mainnet.helius-rpc.com/?api-key=SECRET", 30, lambda *a: None, lambda d: None,
                   fallback_url="wss://api.mainnet-beta.solana.com", switch_after=3, retry_primary_s=100)
    assert s.node()["node"] == "mainnet.helius-rpc.com" and "SECRET" not in json.dumps(s.node())
    s._ended(0, 1000)
    s._ended(1, 1001)                                               # only the subscription reply: still nothing
    assert s.i == 0
    s._ended(0, 1002)
    assert s.i == 1 and s.failovers == 1 and host(s.ws_url) == "api.mainnet-beta.solana.com"
    s._ended(5000, 1050)                                            # a normal drop on the fallback, too soon
    assert s.i == 1
    s._ended(5000, 1103)                                            # next drop after retry_primary_s
    assert s.i == 0
    s._ended(5000, 1200)                                            # delivering: no switch
    assert s.i == 0 and s.fails == 0
    assert "SECRET" not in _redact("InvalidStatus: wss://x/?api-key=SECRET rejected")


class ListGmgn:
    enabled = True
    calls = errors = limited = 0

    def __init__(self, lists, sigs=()):
        self.lists, self.sigs, self.lookups = lists, list(sigs), 0

    def backlog_s(self):
        return 0.0

    async def trenches(self, chain, platforms, limit):
        return self.lists

    async def signals(self, chain, groups):
        return self.sigs

    async def token(self, chain, address, with_security=True):
        self.lookups += 1
        return None

    async def close(self):
        pass


def _engine(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;sources.metadata.enabled=false;"
                   "sources.rugcheck.enabled=false;sources.goplus.enabled=false;sources.gecko_info.enabled=false;"
                   "sources.llm.enabled=false"))
    e.quotes.usd[SYSTEM] = (100.0, time.time() + 1e6)
    return e


async def test_followed_coin_in_a_list_needs_no_lookup_and_signals_are_counted(tmp_path):
    e = _engine(tmp_path)
    now = time.time()
    _launch(e, Curve(ts=int(now - 30)))
    item = {**ITEM, "address": addr(1)}
    e.gmgn = ListGmgn({"new_creation": [], "near_completion": [item], "completed": []},
                      [{"id": "a", "token_address": addr(1), "signal_type": 12, "trigger_at": now - 5},
                       {"id": "b", "token_address": addr(1), "signal_type": 20, "trigger_at": now - 3}])
    task = asyncio.create_task(e.gmgn_feed_loop("sol_pump"))
    await asyncio.sleep(0.05)
    task.cancel()
    tok = e.pump[addr(1)]
    assert tok.gm["gm_smart_n"] == 17 and tok.gm["_gm_list"] == "near_completion"
    assert await e._gm_fetch(tok) is tok.gm and e.gmgn.lookups == 0      # fresh list reading: no request
    task = asyncio.create_task(e.gmgn_signal_loop("sol"))
    await asyncio.sleep(0.05)
    task.cancel()
    assert tok.gm["gm_sig_smart_buy_n"] == 1 and tok.gm["gm_sig_kol_buy_n"] == 1
    assert e.db.one("SELECT COUNT(*) n FROM gm_signals")["n"] == 2
    task = asyncio.create_task(e.gmgn_signal_loop("sol"))              # the same events again: not counted twice
    await asyncio.sleep(0.05)
    task.cancel()
    assert tok.gm["gm_sig_smart_buy_n"] == 1
    tok.gm["_gm_ts"] = now - 3600                                      # old reading: token info is asked
    await e._gm_fetch(tok)
    assert e.gmgn.lookups == 1
    await e.http.close()
