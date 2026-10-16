"""Codex, Solana Tracker and GeckoTerminal token info read into the shared factor names.
Fixtures: Solana Tracker's documented example response, two live GeckoTerminal responses."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from radar.db import DB
from radar.features import apply_gmgn, apply_intel
from radar.scoring import gate, prior_score
from radar.sources.intel import Codex, parse_gecko_info, parse_solanatracker

from .helpers import cfg

C = cfg()
FIX = json.loads((Path(__file__).parent / "fixtures" / "intel_responses.json").read_text(encoding="utf-8"))


def test_solana_tracker_risk_becomes_snipers_bundlers_insiders_top10_dev():
    st = parse_solanatracker(FIX["solanatracker_token"])
    risk = FIX["solanatracker_token"]["risk"]
    assert st["st_ok"] == 1 and st["st_top10_pct"] == risk["top10"]
    assert st["st_sniper_pct"] == risk["snipers"]["totalPercentage"]
    assert st["st_insider_pct"] == risk["insiders"]["totalPercentage"]
    assert st["st_dev_pct"] == risk["dev"]["percentage"] and st["st_rugged"] == int(risk["rugged"])
    assert st["st_mint_authority"] == 0 and st["st_freeze_authority"] == 0          # both null in the example
    f = {"chain": "solana", "px_on_curve": 0}
    apply_intel(f, [st])
    assert f["dist_top10_with_dev_pct"] == risk["top10"] and f["hx_insider_pct"] == risk["insiders"]["totalPercentage"]
    assert f["launch_sniper_pct"] == risk["snipers"]["totalPercentage"]
    assert f["sec_checked"] == 1 and f["sec_renounced"] == 1                       # authorities from the pool data
    assert not any(k.startswith("n_") for k in f)                                  # internal names never stored


def test_gecko_info_gives_holders_but_its_top10_is_not_trusted_as_ours():
    g = parse_gecko_info(FIX["geckoterminal_info_pump"])
    assert g["gti_ok"] == 1 and g["gti_holders"] == FIX["geckoterminal_info_pump"]["holders"]["count"]
    assert g["gti_top10_pct"] > 90                       # the bonding curve counted as a holder
    f = {"chain": "solana", "px_on_curve": 1}
    apply_intel(f, [g])
    assert f["dist_holders"] == g["gti_holders"] and "dist_top10_with_dev_pct" not in f
    assert f["gti_top10_pct"] == g["gti_top10_pct"]     # still kept for the learner
    assert f["sec_checked"] == 1 and f["sec_mintable"] == 0 and f["sec_blacklist"] == 0   # "no" / "no" from GeckoTerminal


def test_codex_detects_the_percentage_scale_and_fills_the_shared_names(tmp_path):
    cx = Codex.__new__(Codex)
    cx.scale = None
    row = {"holders": 412, "top10HoldersPercent": 0.31, "sniperCount": 6, "sniperHeldPercentage": 0.04,
           "bundlerCount": 3, "bundlerHeldPercentage": 0.12, "insiderCount": 2, "insiderHeldPercentage": 0.08,
           "suspiciousCount": 1, "suspiciousHeldPercentage": 0.01, "devHeldPercentage": 0.02,
           "token": {"address": "So1", "networkId": 1399811149}}
    r = cx.parse(row)                                    # 0.31 top 10 can only be a fraction: scaled to 31%
    assert cx.scale == 100 and round(r["cx_top10_pct"], 6) == 31 and round(r["cx_insider_pct"], 6) == 8
    cx2 = Codex.__new__(Codex)
    cx2.scale = None
    r2 = cx2.parse({**row, "top10HoldersPercent": 31.0, "insiderHeldPercentage": 8.0})
    assert cx2.scale == 1 and r2["cx_top10_pct"] == 31 and r2["cx_insider_pct"] == 8
    f = {"chain": "solana", "dist_holders": 50}         # our own measurement wins
    apply_intel(f, [r2])
    assert f["dist_holders"] == 50 and f["dist_top10_with_dev_pct"] == 31 and f["hx_insider_pct"] == 8
    assert f["launch_slot0_pct"] == r2["cx_bundler_pct"]


def test_codex_request_batches_tokens_and_respects_the_daily_budget(tmp_path):
    db = DB(tmp_path / "t.db")
    sent = []

    class Http:
        def limiter(self, *a):
            return None

        async def get_json(self, url, **kw):
            sent.append(kw["json_body"])
            ids = kw["json_body"]["variables"]["tokens"]
            return {"data": {"filterTokens": {"results": [
                {"top10HoldersPercent": 25.0, "holders": 10, "token": {"address": i.split(":")[0]}} for i in ids]}}}
    cx = Codex(Http(), cfg("sources.codex.calls_per_day=2").sources.codex, "key", db)
    got = asyncio.run(cx.lookup_many([("solana", "AbC"), ("robinhood", "0xDEF"), ("tron", "T1")]))
    assert sent[0]["variables"]["tokens"] == ["AbC:1399811149", "0xDEF:4663"]     # unknown chains skipped
    assert set(got) == {"abc", "0xdef"} and got["abc"]["cx_top10_pct"] == 25
    asyncio.run(cx.lookup_many([("solana", "x")]))
    assert asyncio.run(cx.lookup_many([("solana", "y")])) == {} and len(sent) == 2     # budget of 2 a day spent


def test_gmgn_first_then_the_others_and_vetoes_use_whichever_answered():
    f = {"chain": "solana", "px_mcap_usd": 20000, "sec_checked": 1}
    apply_gmgn(f, None, None, own_trades=False)
    st = parse_solanatracker({**FIX["solanatracker_token"],
                              "risk": {**FIX["solanatracker_token"]["risk"],
                                       "top10": 20.0, "insiders": {"count": 9, "totalPercentage": 41.0}}})
    apply_intel(f, [st])
    assert f["hx_insider_pct"] == 41.0 and gate(f, C) == "insiders hold 41%"
    assert "few insiders / phishing wallets" in prior_score(f)[2]
