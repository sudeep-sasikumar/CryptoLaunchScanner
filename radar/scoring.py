"""Day-one judgement: hard vetoes plus a hand-weighted scorecard (0-100).

The scorecard is the starting opinion, built from what usually separates runners from rugs.
It is used alone until the learner has enough labelled history (model.py); after that the
learned probability decides and this score is shown beside it. Safety vetoes always apply;
opinion vetoes step aside for the model (see gate()).
"""
from __future__ import annotations


def ramp(x, bad: float, good: float) -> float | None:
    """0 at `bad`, 1 at `good`, linear between (either direction). None stays None."""
    if x is None:
        return None
    if good == bad:
        return 1.0 if x >= good else 0.0
    return min(1.0, max(0.0, (x - bad) / (good - bad)))


def band(x, lo_bad: float, lo_good: float, hi_good: float, hi_bad: float) -> float | None:
    """1 inside [lo_good, hi_good], falling to 0 at lo_bad / hi_bad: for 'not too little, not too much'."""
    if x is None:
        return None
    return min(ramp(x, lo_bad, lo_good), ramp(x, hi_bad, hi_good))


def _cp_scale(f: dict, per_min: float, floor: float) -> float:
    """A count that should grow with age: what is 'good' at this checkpoint."""
    return max(floor, per_min * min(f.get("age_s", 60), 900) / 60)


# (label shown in alerts, weight, scoring function) - weights sum to 100 per family
PUMP_CARD = [
    # fair launch (26)
    ("no bundle",             8, lambda f: ramp(f.get("launch_slot0_pct"), 12, 0)),
    ("few snipers",           5, lambda f: ramp(f.get("launch_sniper_pct"), 15, 2)),
    ("dev not dumping",       5, lambda f: ramp(f.get("launch_dev_sold_pct"), 50, 0)),
    ("dev bag modest",        3, lambda f: ramp(f.get("launch_dev_hold_pct"), 15, 3)),
    ("no coordinated buying", 3, lambda f: min(ramp(f.get("launch_cluster_pct"), 12, 1),
                                               ramp(f.get("launch_same_size_share"), 0.35, 0.05))),
    ("holders bought, not airdropped", 2, lambda f: ramp(f.get("launch_phantom_sold_pct"), 3, 0)),
    # distribution (20)
    ("top 10 not concentrated", 8, lambda f: ramp(f.get("dist_top10_with_dev_pct"), 45, 16)),
    ("no whale",              4, lambda f: ramp(f.get("dist_top1_pct"), 10, 3)),
    ("many holders",          5, lambda f: ramp(f.get("dist_holders"), _cp_scale(f, 6, 12), _cp_scale(f, 40, 60))),
    ("buyers are staying",    3, lambda f: ramp(f.get("dist_exited_share"), 0.6, 0.2)),
    # wallet quality (14) - neutral until reputations exist
    ("smart wallets in",      8, lambda f: None if not f.get("wq_known_share") else ramp(f.get("wq_smart_n"), 0, 3)),
    ("few toxic wallets",     6, lambda f: None if not f.get("wq_known_share") else ramp(f.get("wq_toxic_share"), 0.3, 0.0)),
    # demand and chart (25)
    ("buy pressure",          5, lambda f: ramp(f.get("flow_buy_share_q"), 0.45, 0.65)),
    ("new buyers arriving",   5, lambda f: ramp(f.get("flow_last60_new_buyers"), 3, 30)),
    ("holding near highs",    6, lambda f: ramp(f.get("px_dd_pct"), 45, 10)),
    ("steady uptrend",        4, lambda f: None if f.get("px_slope") is None
                                           else ramp(f["px_slope"], -0.02, 0.02) * (0.5 + 0.5 * (f.get("px_r2") or 0))),
    ("volume building",       3, lambda f: ramp(f.get("flow_accel"), 0.4, 1.5)),
    ("past the first minute", 2, lambda f: ramp(f.get("px_curve_pct"), 2, 8)),
    # identity (15)
    ("has socials",           4, lambda f: None if not f.get("id_meta_ok") else ramp(f.get("id_socials_n"), 0, 2)),
    ("own X account",         2, lambda f: None if not f.get("id_meta_ok") else (1.0 if f.get("id_x_kind") == 1 else 0.3)),
    # a copy launched while the original is still hot is the classic pump.fun trap: no credit at all
    ("original name",         3, lambda f: 0.0 if (f.get("id_name_burst_n") or 0) >= 1 else ramp(f.get("id_name_prev_n"), 4, 0)),
    ("original image / socials", 3, lambda f: None if not f.get("id_meta_ok") else min(
        ramp(f.get("id_img_prev_n") or 0, 2, 0), ramp(f.get("id_x_prev_n") or 0, 2, 0))),
    ("creator record",        3, lambda f: None if not f.get("cr_prev_n") else ramp(
        (f.get("cr_win_n") or 0) - 0.5 * (f.get("cr_dead_n") or 0), -2, 1)),
]

DEX_CARD = [
    # safety (34)
    ("sellable, no honeypot", 10, lambda f: None if f.get("sec_checked") is None else 1.0 - (f.get("sec_honeypot") or 0)),
    ("low tax",               6, lambda f: ramp(f.get("sec_tax_max"), 10, 0)),
    ("ownership renounced",   5, lambda f: f.get("sec_renounced")),
    ("no mint / blacklist",   6, lambda f: None if f.get("sec_checked") is None
                                           else 1.0 - min(1.0, (f.get("sec_mintable") or 0) + (f.get("sec_blacklist") or 0))),
    ("verified contract",     3, lambda f: f.get("sec_open_source")),
    ("LP burned / locked",    4, lambda f: ramp(f.get("sec_lp_locked_pct"), 20, 90)),
    # liquidity and distribution (22)
    ("real liquidity",        6, lambda f: ramp(f.get("px_liq_usd"), 4000, 30000)),
    ("liquidity vs. mcap",    4, lambda f: ramp(f.get("px_liq_per_mcap"), 0.03, 0.2)),
    ("top 10 not concentrated", 6, lambda f: ramp(f.get("sec_top10_pct"), 55, 18) if (f.get("sec_holders") or 0) >= 20 else None),
    ("no bundle",             6, lambda f: ramp(f.get("launch_block0_pct"), 40, 3)),
    # wallet quality (8)
    ("smart wallets in",      5, lambda f: None if not f.get("wq_known_share") else ramp(f.get("wq_smart_n"), 0, 3)),
    ("few toxic wallets",     3, lambda f: None if not f.get("wq_known_share") else ramp(f.get("wq_toxic_share"), 0.3, 0.0)),
    # demand and chart (24)
    ("buy pressure",          6, lambda f: ramp(f.get("flow_buy_share_h1"), 0.42, 0.62)),
    ("many buyers",           5, lambda f: ramp(f.get("flow_buys_h1"), 20, 250)),
    ("holding near highs",    6, lambda f: ramp(f.get("px_dd_pct"), 45, 10)),
    ("volume still coming",   4, lambda f: ramp(f.get("flow_m5_share"), 0.03, 0.3)),
    ("steady uptrend",        3, lambda f: None if f.get("px_slope") is None else ramp(f["px_slope"], -0.02, 0.02)),
    # identity (12)
    ("has socials",           4, lambda f: None if not f.get("id_meta_ok") and not f.get("id_dex_paid")
                                           else max(ramp(f.get("id_socials_n"), 0, 2) or 0.0, float(f.get("id_dex_paid") or 0))),
    ("original name",         4, lambda f: ramp(f.get("id_name_prev_n"), 4, 0)),
    ("dex profile paid / boosted", 4, lambda f: max(float(f.get("id_dex_paid") or 0), ramp(f.get("id_dex_boost"), 0, 50) or 0)),
]

CARDS = {"pump": PUMP_CARD, "dex": DEX_CARD}
NEUTRAL = 0.5


def prior_score(family: str, f: dict) -> tuple[float, list[str], list[str]]:
    """(score 0-100, strongest points, weakest points). Unknown factors count as neutral."""
    total = 0.0
    good, bad = [], []
    for label, weight, fn in CARDS[family]:
        try:
            s = fn(f)
        except (TypeError, KeyError):
            s = None
        if s is None:
            total += weight * NEUTRAL
            continue
        s = float(s)
        total += weight * s
        if s >= 0.8:
            good.append((weight * s, label))
        elif s <= 0.3:
            bad.append((weight * (1 - s), label))
    # optional LLM read of the meme: nudges the score by up to +/-5
    viral = f.get("llm_viral")
    if viral is not None:
        total += (viral - 5) / 5 * 5
    good.sort(reverse=True)
    bad.sort(reverse=True)
    return max(0.0, min(100.0, total)), [g[1] for g in good[:4]], [b[1] for b in bad[:3]]


def gate(family: str, f: dict, cfg, learned: bool = False) -> str | None:
    """Reason this snapshot must not be called, or None.

    Safety vetoes (cannot sell, tax, owner powers, flagged as rugged, no price, mcap out of
    range) always apply. Opinion vetoes (bundle overhang, concentration, dev behaviour ...)
    apply only while the scorecard decides: every snapshot is labelled whether vetoed or not,
    so once a model is in use (`learned`) it has seen how those cases really end and judges them."""
    g = cfg.gates[family]
    mc = f.get("px_mcap_usd")
    lim = cfg.call.mcap_usd
    if mc is None:
        if not cfg.call.allow_unknown_quote:
            return "no USD price for its quote token"
    elif mc < lim["min"]:
        return f"mcap below ${lim['min']:,}"
    elif mc > lim["max"]:
        return f"mcap above ${lim['max']:,}"
    if f.get("rc_rugged"):
        return "RugCheck: flagged as rugged"
    if family == "dex":
        if f.get("sec_honeypot"):
            return "honeypot / cannot sell"
        if (f.get("sec_tax_max") or 0) > g["max_tax_pct"]:
            return f"tax {f['sec_tax_max']:.0f}%"
        if (f.get("px_liq_usd") or 0) < g["min_liquidity_usd"] and f.get("px_liq_usd") is not None:
            return "liquidity too thin"
        if f.get("sec_mintable") and not f.get("sec_renounced"):
            return "owner can mint more"
        if f.get("sec_blacklist") and not f.get("sec_renounced"):
            return "owner can blacklist holders"
    if learned:
        return None
    if (f.get("px_dd_pct") or 0) > cfg.gates.pump["max_drawdown_pct"]:
        return "already far below its high"
    if family == "pump":
        if (f.get("launch_bundle_held_pct") or 0) > g["max_bundle_held_pct"]:
            return f"bundle / sniper wallets still hold {f['launch_bundle_held_pct']:.0f}% of supply"
        if (f.get("dist_top10_with_dev_pct") or 0) > g["max_top10_pct"]:
            return f"top 10 hold {f['dist_top10_with_dev_pct']:.0f}%"
        if (f.get("launch_dev_hold_pct") or 0) > g["max_dev_hold_pct"]:
            return f"dev holds {f['launch_dev_hold_pct']:.0f}%"
        if (f.get("launch_dev_sold_pct") or 0) > g["max_dev_sold_pct"] and (f.get("launch_dev_buy_pct") or 0) >= 1:
            return "dev sold most of their bag"
        if (f.get("cr_day_n") or 0) > g["max_creator_launches_24h"]:
            return f"creator launched {f['cr_day_n']} coins in 24 h"
        if (f.get("wq_toxic_share") or 0) > 0.4:
            return "early buyers are mostly known toxic wallets"
    else:
        # holder figures lag a new token (at first the deployer "holds" everything): trust them once holders exist
        fresh = (f.get("sec_holders") or 0) >= 20
        if fresh and (f.get("sec_top10_pct") or 0) > g["max_top10_pct"]:
            return f"top 10 hold {f['sec_top10_pct']:.0f}%"
        if fresh and (f.get("sec_creator_pct") or 0) > g["max_creator_pct"]:
            return f"creator holds {f['sec_creator_pct']:.0f}%"
    return None
