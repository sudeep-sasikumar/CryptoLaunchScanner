"""Day-one judgement: hard vetoes plus a hand-weighted scorecard (0-100).

One scorecard and one set of vetoes for every launchpad. New coins behave alike wherever they
launch (they run fast or they die), so pump.fun coins get the same contract-safety checks as
everything else, and other launchpads get the same launch-fairness and demand factors as
pump.fun. Where a launchpad does not let us measure a factor, it scores neutral (half marks).

The scorecard is the starting opinion, built from what usually separates runners from rugs.
It is used alone until the learner has enough labelled history (model.py); after that the
learned probability decides and this score is shown beside it. Safety vetoes always apply;
opinion vetoes step aside for the model (see gate()).
"""
from __future__ import annotations

import math


def ramp(x, bad: float, good: float) -> float | None:
    """0 at `bad`, 1 at `good`, linear between (either direction). None stays None."""
    if x is None:
        return None
    if good == bad:
        return 1.0 if x >= good else 0.0
    return min(1.0, max(0.0, (x - bad) / (good - bad)))


def _cp_scale(f: dict, per_min: float, floor: float) -> float:
    """A count that should grow with age: what is 'good' at this checkpoint."""
    return max(floor, per_min * min(f.get("age_s", 60), 900) / 60)


def _flag_ok(v) -> float | None:
    """1 when a risk flag is clear, 0 when raised, None when unknown."""
    return None if v is None else 1.0 - min(1.0, float(v))


def _worst(*scores) -> float | None:
    known = [s for s in scores if s is not None]
    return min(known) if known else None


def _log_ramp(x, bad: float, good: float) -> float | None:
    """ramp() on a log10 scale - for counts that matter by order of magnitude (followers)."""
    return None if x is None else ramp(math.log10(max(float(x), 0.0) + 1), math.log10(bad + 1), math.log10(good + 1))


def _smart(f: dict) -> float | None:
    """Smart money in: our own learned smart wallets, or GMGN's smart-money and KOL counts."""
    known = [v for v in (f.get("wq_smart_n") if f.get("wq_known_share") else None,
                         (f.get("gm_smart_n") or 0) + (f.get("gm_kol_n") or 0) if f.get("gm_ok") else None)
             if v is not None]
    return ramp(max(known), 0, 3) if known else None


def _creator(f: dict) -> float | None:
    """The creator's track record: our own (from coins we followed) first, else GMGN's launch history.
    A serial launcher whose coins almost never graduate scores zero."""
    if f.get("cr_prev_n"):
        return ramp((f.get("cr_win_n") or 0) - 0.5 * (f.get("cr_dead_n") or 0), -2, 1)
    made, ratio = f.get("gm_creator_created_n"), f.get("gm_creator_open_ratio")
    if made is None or made <= 1:
        return None                                    # a first launch: no record either way
    if ratio is None:
        return None
    return 0.0 if made >= 20 and ratio < 0.05 else ramp(ratio, 0.0, 0.15)


# (label shown in alerts, weight, scoring function) - weights sum to 100
CARD = [
    # contract safety (14) - pump.fun coins nearly always pass; still checked, like everything else
    ("sellable, no honeypot", 4, lambda f: _flag_ok(f.get("sec_honeypot"))),
    ("low tax",               3, lambda f: ramp(f.get("sec_tax_max"), 10, 0)),
    ("no mint / freeze / blacklist powers", 3, lambda f: _worst(_flag_ok(f.get("sec_mintable")),
                                                                 _flag_ok(f.get("sec_blacklist")))),
    ("ownership renounced",   2, lambda f: f.get("sec_renounced")),
    ("liquidity locked / burnt", 2, lambda f: ramp(f.get("sec_lp_locked_pct"), 20, 90)),
    # fair launch (24) - the GMGN-style checks
    ("no bundle",             5, lambda f: ramp(f.get("launch_slot0_pct"), 12, 0)),
    ("few snipers",           3, lambda f: ramp(f.get("launch_sniper_pct"), 15, 2)),
    ("few insiders",          4, lambda f: ramp(f.get("hx_insider_pct"), 25, 2)),
    ("few phishing wallets",  4, lambda f: ramp(f.get("hx_phishing_pct"), 25, 2) if f.get("hx_phishing_pct") is not None
                                           else ramp(f.get("launch_phantom_sold_pct"), 3, 0)),
    ("dev not dumping",       3, lambda f: ramp(f.get("launch_dev_sold_pct"), 50, 0)),
    ("dev bag modest",        2, lambda f: ramp(f.get("launch_dev_hold_pct"), 15, 3)),
    ("no coordinated buying", 3, lambda f: _worst(ramp(f.get("launch_cluster_pct"), 12, 1),
                                                  ramp(f.get("launch_same_size_share"), 0.35, 0.05))),
    # holder distribution (14)
    ("top 10 not concentrated", 6, lambda f: ramp(f.get("dist_top10_with_dev_pct"), 45, 16)),
    ("no whale",              2, lambda f: ramp(f.get("dist_top1_pct"), 10, 3)),
    ("many holders",          3, lambda f: ramp(f.get("dist_holders"), _cp_scale(f, 6, 12), _cp_scale(f, 40, 60))),
    ("buyers are staying",    3, lambda f: ramp(f.get("dist_exited_share"), 0.6, 0.2)),
    # wallet quality (12)
    ("smart money / KOLs in", 7, _smart),
    ("few toxic wallets",     5, lambda f: None if not f.get("wq_known_share") else ramp(f.get("wq_toxic_share"), 0.3, 0.0)),
    # demand and chart (22)
    ("buy pressure",          5, lambda f: ramp(f.get("flow_buy_share_recent"), 0.45, 0.65)),
    ("buyers arriving",       4, lambda f: ramp(f.get("flow_buys_per_min"), 3, 30)),
    ("holding near highs",    5, lambda f: ramp(f.get("px_dd_pct"), 45, 10)),
    ("steady uptrend",        3, lambda f: None if f.get("px_slope") is None
                                           else ramp(f["px_slope"], -0.02, 0.02) * (0.5 + 0.5 * (f.get("px_r2") or 0))),
    ("volume building",       3, lambda f: ramp(f.get("flow_accel"), 0.4, 1.5)),
    ("liquidity vs. market cap", 2, lambda f: ramp(f.get("px_liq_per_mcap"), 0.03, 0.2)),
    # identity and buzz (14)
    ("has socials",           3, lambda f: None if not f.get("id_meta_ok") and not f.get("id_dex_paid")
                                           else max(ramp(f.get("id_socials_n"), 0, 2) or 0.0, float(f.get("id_dex_paid") or 0))),
    ("own X account",         1, lambda f: None if not f.get("id_meta_ok") else (1.0 if f.get("id_x_kind") == 1 else 0.3)),
    ("X following",           2, lambda f: _log_ramp(f.get("gm_x_followers"), 20, 3000)),
    # a copy launched while the original is still hot is the classic trap: no credit at all
    ("original name",         3, lambda f: 0.0 if (f.get("id_name_burst_n") or 0) >= 1 else ramp(f.get("id_name_prev_n"), 4, 0)),
    ("original image / socials", 2, lambda f: _worst(ramp(f.get("id_img_prev_n"), 2, 0), ramp(f.get("id_x_prev_n"), 2, 0))),
    ("creator record",        3, _creator),
]
NEUTRAL = 0.5


def prior_score(f: dict) -> tuple[float, list[str], list[str]]:
    """(score 0-100, strongest points, weakest points). Unknown factors count as neutral."""
    total = 0.0
    good, bad = [], []
    for label, weight, fn in CARD:
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


def gate(f: dict, cfg, learned: bool = False, final: bool = False) -> str | None:
    """Reason this snapshot must not be called, or None. The same list for every launchpad,
    each check using whatever that launchpad lets us measure.

    Safety vetoes (cannot sell, tax, owner powers, flagged as rugged, no price, mcap range, thin
    pool) always apply. Opinion vetoes (bundle overhang, concentration, dev behaviour ...) apply
    only while the scorecard decides: every snapshot is labelled whether vetoed or not, so once a
    model is in use (`learned`) it has seen how those cases really end and judges them itself.
    `final` is the last check before a call: then a contract that was never checked also blocks."""
    g = cfg.gates
    if final and cfg.call.require_safety_check and not f.get("sec_checked"):
        return "contract safety not checked (GoPlus gave no answer)"
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
    if f.get("sec_honeypot"):
        return "honeypot / cannot sell"
    if (f.get("sec_tax_max") or 0) > g["max_tax_pct"]:
        return f"tax {f['sec_tax_max']:.0f}%"
    if f.get("sec_mintable") and not f.get("sec_renounced"):
        return "someone can still mint more"
    if f.get("sec_blacklist") and not f.get("sec_renounced"):
        return "someone can freeze or blacklist holders"
    if f.get("gm_wash"):
        return "GMGN: wash trading detected"
    # a bonding curve always buys back; only a pool can be too thin to sell into
    if not f.get("px_on_curve") and f.get("px_liq_usd") is not None and f["px_liq_usd"] < g["min_liquidity_usd"]:
        return "liquidity too thin"
    if learned:
        return None
    if (f.get("px_dd_pct") or 0) > g["max_drawdown_pct"]:
        return "already far below its high"
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
    if (f.get("hx_insider_pct") or 0) > g["max_insider_pct"]:
        return f"insiders hold {f['hx_insider_pct']:.0f}%"
    if (f.get("hx_phishing_pct") or 0) > g["max_phishing_pct"]:
        return f"phishing wallets hold {f['hx_phishing_pct']:.0f}%"
    if (f.get("gm_rug_ratio") or 0) > g["max_rug_ratio"]:
        return f"GMGN rug ratio {f['gm_rug_ratio']:.2f}"
    made, ratio = f.get("gm_creator_created_n"), f.get("gm_creator_open_ratio")
    if made and made >= g["serial_creator_min_launches"] and (ratio or 0) < g["serial_creator_max_grad_ratio"]:
        return f"serial launcher: {made:,.0f} coins, {(ratio or 0) * 100:.1f}% graduated"
    return None
