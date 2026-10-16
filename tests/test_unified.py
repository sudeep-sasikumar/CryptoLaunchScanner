"""Every launchpad is judged the same way: one scorecard, one veto list, the same factor names."""
from __future__ import annotations

import asyncio
import time

from radar.features import dex_features, pump_features
from radar.scoring import CARD, gate, prior_score
from radar.sources.security import Security
from radar.state import DexToken

from .test_core import _clean_launch
from .helpers import cfg

C = cfg()
OPEN = cfg("call.pull_proof.only=false")
SAFE = {"sec_checked": 1, "sec_honeypot": 0, "sec_tax_max": 0.0, "sec_mintable": 0, "sec_blacklist": 0,
        "sec_renounced": 1}


def test_one_card_of_100_points():
    assert sum(w for _, w, _ in CARD) == 100


def test_pump_coins_get_the_safety_checks_and_vetoes():
    c, tok = _clean_launch()
    tok.security = dict(SAFE)
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": 100.0, "quote_kind": 0})
    assert f["sec_checked"] == 1 and f["sec_lp_locked_pct"] == 100 and f["px_on_curve"] == 1
    assert gate(f, C, final=True) is None
    tok.security = {**SAFE, "sec_mintable": 1, "sec_renounced": 0}          # a mint authority left in place
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": 100.0, "quote_kind": 0})
    assert "mint" in gate(f, C) and "mint" in gate(f, C, learned=True)     # a safety veto: the model cannot lift it
    tok.security = None                                                     # the check never answered
    f = pump_features(tok, 60, c.ts + 60, {"quote_usd": 100.0, "quote_kind": 0})
    assert gate(f, C) is None and "not checked" in gate(f, C, final=True)  # recorded, but never called


def test_goplus_solana_answer_is_read_as_the_same_safety_factors():
    sec = Security.__new__(Security)
    sec.go_on = True
    sec.go_lim = None

    class FakeHttp:
        def __init__(self, result):
            self.result = result

        async def get_json(self, *a, **k):
            return {"code": 1, "result": {"mint": self.result}}

    clean = {"mintable": {"status": "0"}, "freezable": {"status": "0"}, "balance_mutable_authority": {"status": "0"},
             "metadata_mutable": {"status": "0"}, "non_transferable": "0", "transfer_fee": {}, "transfer_hook": [],
             "default_account_state": "1", "metadata": {"uri": "https://x.test/meta.json"}}
    sec.http = FakeHttp(clean)
    r = asyncio.run(sec.goplus_solana("mint"))
    assert r["sec_checked"] == 1 and r["sec_honeypot"] == 0 and r["sec_tax_max"] == 0 and r["sec_renounced"] == 1
    assert r["_uri"] == "https://x.test/meta.json"
    risky = {**clean, "freezable": {"status": "1"}, "transfer_fee": {"current_fee_rate": "0.15"},
             "transfer_hook": ["Hook111"]}
    sec.http = FakeHttp(risky)
    r = asyncio.run(sec.goplus_solana("mint"))
    assert r["sec_blacklist"] == 1 and r["sec_honeypot"] == 1 and r["sec_tax_max"] == 15 and r["sec_renounced"] == 0
    f = {"px_mcap_usd": 20000, **r}
    assert gate(f, C) == "honeypot / cannot sell"


def test_other_launchpads_get_the_pump_style_factors_under_the_same_names():
    now = time.time()
    tok = DexToken(id="robinhood:0xabc", chain="robinhood", address="0xabc", pool="0xpool", dex="pons-v2",
                   launchpad="pons", name="Fresh", symbol="FRSH", created_ts=now - 200)
    for i in range(25):                                         # 25 polls, 15 s apart, rising
        tok.on_pair(now - 375 + i * 15, {"priceUsd": str(0.001 * (1 + i / 20)), "marketCap": 30000 + i * 800,
                                         "liquidity": {"usd": 9000},
                                         "txns": {"m5": {"buys": 40 + i, "sells": 20}, "h1": {"buys": 60 + i, "sells": 25}},
                                         "volume": {"m5": 2000 + i * 100, "h1": 6000}})
    tok.security = {**SAFE, "sec_holders": 85, "sec_top10_pct": 30.0, "sec_top1_pct": 6.0, "sec_creator_pct": 4.0}
    tok.trades = {"launch_block0_pct": 5.0, "launch_sniper_pct": 3.0, "launch_cluster_share": 0.02,
                  "launch_same_size_share": 0.1}
    f = dex_features(tok, 180, now, {"on_curve": True})
    for k in ("flow_buy_share_recent", "flow_buys_per_min", "flow_accel", "launch_slot0_pct", "launch_sniper_pct",
              "launch_dev_hold_pct", "dist_top10_with_dev_pct", "dist_top1_pct", "dist_holders", "px_slope",
              "px_dd_pct", "sec_lp_locked_pct"):
        assert f.get(k) is not None, k
    assert f["source"] == "poll" and f["px_on_curve"] == 1 and f["flow_buys_per_min"] == 64 / 5
    assert f["launch_slot0_pct"] == 5.0 and f["dist_top10_with_dev_pct"] == 30.0
    assert gate(f, C, final=True) is None
    assert prior_score(f)[0] > 60                               # healthy on every count it can see


def test_holder_figures_from_an_evm_scanner_wait_until_holders_exist():
    now = time.time()
    tok = DexToken(id="base:0xdef", chain="base", address="0xdef", pool="0xp", dex="uniswap-v4", launchpad="clanker",
                   name="X", symbol="X", created_ts=now - 100)
    tok.on_pair(now, {"priceUsd": "0.01", "marketCap": 50000, "liquidity": {"usd": 20000},
                      "txns": {"m5": {"buys": 10, "sells": 2}, "h1": {"buys": 12, "sells": 2}}, "volume": {"m5": 500, "h1": 600}})
    tok.security = {**SAFE, "sec_holders": 3, "sec_top10_pct": 100.0, "sec_creator_pct": 100.0}
    f = dex_features(tok, 60, now, {})
    assert f.get("dist_top10_with_dev_pct") is None and f.get("launch_dev_hold_pct") is None   # stale, not used
    assert gate(f, OPEN) is None                                   # pull filter off: nothing else blocks it
