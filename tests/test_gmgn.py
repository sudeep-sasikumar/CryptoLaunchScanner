"""GMGN responses (recorded from the live API) read into factors, and those factors used by the
scorecard and vetoes the same way for every launchpad."""
from __future__ import annotations

import json
import time
from pathlib import Path

from radar.features import apply_gmgn, dex_features
from radar.scoring import CARD, gate, prior_score
from radar.sources.gmgn import from_trenches, parse_token
from radar.state import DexToken

from .helpers import cfg

C = cfg()
FIX = json.loads((Path(__file__).parent / "fixtures" / "gmgn_tokens.json").read_text(encoding="utf-8"))


def test_robinhood_token_reads_like_the_gmgn_page():
    fx = FIX["factor_robinhood"]
    g = parse_token(fx["info"], fx["security"])
    st = fx["info"]["stat"]
    assert g["gm_ok"] == 1 and g["gm_sec_ok"] == 1
    assert abs(g["gm_top10_pct"] - float(st["top_10_holder_rate"]) * 100) < 1e-9
    assert abs(g["gm_phishing_pct"] - float(st["top_entrapment_trader_percentage"]) * 100) < 1e-9
    assert g["gm_insider_pct"] == float(st["top_rat_trader_percentage"]) * 100
    assert g["gm_holders"] == st["holder_count"] and g["gm_dev_closed"] == 1 and g["gm_cto"] == 1
    assert g["gm_honeypot"] == 0 and g["gm_renounced"] == 1 and g["gm_open_source"] == 1 and g["gm_tax_max"] == 0
    assert g["gm_burnt_pct"] >= 95 and g["gm_dex_paid"] == 1 and g["gm_image_dup_n"] == fx["info"]["image_dup_count"]
    assert g["gm_creator_open_ratio"] == 1.0


def test_pump_token_gets_solana_specific_safety_from_gmgn():
    fx = FIX["pump_sol"]
    g = parse_token(fx["info"], fx["security"])
    f = {"chain": "solana", "px_on_curve": 1, "dist_holders": 50}        # our own trade-built holder count
    apply_gmgn(f, g, None, own_trades=True)
    assert f["dist_holders"] == 50                                        # our measurement keeps priority
    assert f["gm_holders"] == g["gm_holders"]                             # ... GMGN's is kept beside it
    assert f["sec_checked"] == 1 and f["sec_mintable"] == 1 - g["gm_renounced_mint"]
    assert f["sec_blacklist"] == 1 - g["gm_renounced_freeze"]
    assert "sec_lp_locked_pct" not in f                                   # on the curve: burn status does not apply


def test_other_launchpad_gets_holders_bundles_snipers_and_safety_from_gmgn():
    fx = FIX["factor_robinhood"]
    now = time.time()
    tok = DexToken(id="robinhood:" + fx["info"]["address"], chain="robinhood", address=fx["info"]["address"], pool="",
                   dex="uniswap_v4", launchpad="pons", name="Factor", symbol="FACTOR", created_ts=now - 200)
    for i in range(12):
        tok.on_pair(now - 180 + i * 15, {"priceUsd": str(6e-5 * (1 + i / 30)), "marketCap": 60000 + i * 500,
                                         "liquidity": {"usd": 23000},
                                         "txns": {"m5": {"buys": 19, "sells": 38}, "h1": {"buys": 196, "sells": 160}},
                                         "volume": {"m5": 1886, "h1": 17908}})
    f = dex_features(tok, 180, now, {"on_curve": False})
    feed = from_trenches({"x_user_follower": 828, "tg_call_count": 2, "twitter_dup": 0, "image_dup": 2, "rug_ratio": 0.1})
    apply_gmgn(f, parse_token(fx["info"], fx["security"]), feed, own_trades=False)
    assert f["dist_top10_with_dev_pct"] == f["gm_top10_pct"] and f["dist_holders"] == f["gm_holders"]
    assert f["launch_slot0_pct"] == f["gm_bundler_pct"] and f["launch_sniper_pct"] == f["gm_sniper_hold_pct"]
    assert f["sec_checked"] == 1 and f["sec_renounced"] == 1 and f["sec_lp_locked_pct"] >= 95
    assert f["gm_x_followers"] == 828 and f["id_img_prev_n"] >= 2 and f["id_dex_paid"] == 1
    assert gate(f, C, final=True) == f"phishing wallets hold {f['gm_phishing_pct']:.0f}%"   # 38% phishing: blocked
    assert gate(f, C, learned=True, final=True) is None                    # an opinion veto: a model may lift it
    items = {label: fn for label, _, fn in CARD}
    assert items["few insiders / phishing wallets"](f) == 0.0              # 38% phishing scores nothing
    assert 0 <= prior_score(f)[0] <= 100


def test_serial_launcher_and_wash_trading_vetoes():
    base = {"px_mcap_usd": 20000, "sec_checked": 1}
    assert "serial launcher" in gate({**base, "gm_creator_created_n": 27380, "gm_creator_open_ratio": 409 / 27380}, C)
    assert gate({**base, "gm_creator_created_n": 30, "gm_creator_open_ratio": 0.2}, C) is None
    assert "wash" in gate({**base, "gm_wash": 1}, C, learned=True)                # safety: always applies
