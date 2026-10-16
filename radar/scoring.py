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


def _inflow(f: dict) -> float | None:
    """Money that came in: bonding-curve progress while on a curve, else traded volume vs market cap."""
    if f.get("px_on_curve") and f.get("px_curve_pct") is not None:
        return ramp(f["px_curve_pct"], 15, 60)
    return ramp(f.get("flow_vol_per_mcap"), 0.4, 1.2)


def _fresh_creator(f: dict) -> float | None:
    """A first-time creator scores best; a serial launcher only earns it back with earlier winners.
    A creator whose earlier coin lost its liquidity scores nothing."""
    if f.get("cr_pulled_n"):
        return 0.0
    if f.get("cr_prev_n") is not None:
        s = ramp(f["cr_prev_n"], 10, 0)
        if f.get("cr_win_n"):
            s = max(s, ramp(f["cr_win_n"], 0, 2))
        return s
    return _creator(f)


# (label shown in alerts, weight, scoring function) - weights sum to 100.
# Rebalanced 2026-10-16 on 3 days of outcomes (tools/analyze.py): weights follow how well each item
# told 2x runners apart on 14-15 Oct, checked on 16 Oct (separation 0.59 -> 0.69). Items that did
# not separate keep a small weight (safety is enforced by the vetoes instead); "top 10 not
# concentrated", "no bundle", "has socials" and "original name" pointed the wrong way and were dropped.
CARD = [
    # contract safety (6) - the vetoes in gate() do the real protecting
    ("sellable, no honeypot", 2, lambda f: _flag_ok(f.get("sec_honeypot"))),
    ("low tax",               1, lambda f: ramp(f.get("sec_tax_max"), 10, 0)),
    ("no mint / freeze / blacklist powers", 2, lambda f: _worst(_flag_ok(f.get("sec_mintable")),
                                                                 _flag_ok(f.get("sec_blacklist")))),
    ("liquidity locked / burnt", 1, lambda f: ramp(f.get("sec_lp_locked_pct"), 20, 90)),
    # momentum (29): coins that run are already taking in money and making fresh highs
    ("money flowing in",      12, _inflow),
    ("price rising",          5, lambda f: ramp(f.get("px_mult"), 1.2, 3.0)),
    ("steady uptrend",        6, lambda f: None if f.get("px_slope") is None
                                           else ramp(f["px_slope"], -0.02, 0.02) * (0.5 + 0.5 * (f.get("px_r2") or 0))),
    ("holding near highs",    3, lambda f: ramp(f.get("px_dd_pct"), 45, 10)),
    ("fresh high",            3, lambda f: ramp(f.get("px_since_high_s"), 120, 10)),
    # demand (18)
    ("buy pressure",          8, lambda f: ramp(f.get("flow_buy_share_recent"), 0.45, 0.65)),
    ("buyers arriving",       3, lambda f: ramp(f.get("flow_buys_per_min"), 3, 30)),
    ("volume building",       4, lambda f: ramp(f.get("flow_accel"), 0.4, 1.5)),
    ("liquidity vs. market cap", 3, lambda f: ramp(f.get("px_liq_per_mcap"), 0.03, 0.2)),
    # holders (21)
    ("many holders",          7, lambda f: ramp(f.get("dist_holders"), _cp_scale(f, 6, 12), _cp_scale(f, 40, 60))),
    ("buyers are staying",    8, lambda f: ramp(f.get("dist_exited_share"), 0.6, 0.2)),
    ("spread across many wallets", 6, lambda f: ramp(f.get("dist_hhi"), 0.15, 0.04)),
    # who is behind it and who is buying (17)
    ("fresh creator / good record", 6, _fresh_creator),
    ("early buyers have picked winners", 6, lambda f: ramp(f.get("wq_lift_mean"), 0.9, 1.3)),
    ("few toxic wallets",     3, lambda f: None if not f.get("wq_known_share") else ramp(f.get("wq_toxic_share"), 0.3, 0.0)),
    ("smart money / KOLs in", 2, _smart),
    # fair launch (6)
    ("few insiders / phishing wallets", 3, lambda f: _worst(
        ramp(f.get("hx_insider_pct"), 25, 2),
        ramp(f.get("hx_phishing_pct"), 25, 2) if f.get("hx_phishing_pct") is not None
        else ramp(f.get("launch_phantom_sold_pct"), 3, 0))),
    ("dev not dumping",       2, lambda f: ramp(f.get("launch_dev_sold_pct"), 50, 0)),
    ("few snipers",           1, lambda f: ramp(f.get("launch_sniper_pct"), 15, 2)),
    # identity (3)
    ("X following",           2, lambda f: _log_ramp(f.get("gm_x_followers"), 20, 3000)),
    ("original image / socials", 1, lambda f: _worst(ramp(f.get("id_img_prev_n"), 2, 0), ramp(f.get("id_x_prev_n"), 2, 0))),
]
assert sum(w for _, w, _ in CARD) == 100
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


def pull_proof(f: dict, pp) -> bool:
    """Can nobody pull this coin's liquidity? True on a bonding curve, after graduation on a launchpad
    that locks or burns the LP, or with a confirmed lock elsewhere."""
    if f.get("px_on_curve"):
        return True
    if f.get("launchpad") in pp["graduated_locked"]:
        return True
    return (f.get("sec_lp_locked_pct") or 0) >= pp["min_locked_pct"]


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
    if (f.get("cr_pulled_n") or 0) >= cfg.gates.get("max_creator_pulls", 1):
        return f"creator's earlier coins lost their liquidity ({f['cr_pulled_n']:.0f}x)"
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
        return _pull_veto(f, cfg)
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
    return _pull_veto(f, cfg)


def _pull_veto(f: dict, cfg) -> str | None:
    """Last check, model or not: with call.pull_proof.only, a coin whose liquidity can be pulled is never called."""
    pp = cfg.call.get("pull_proof") or {}
    if pp.get("only") and not pull_proof(f, pp):
        return "liquidity can be pulled (not on a curve, no locked or burned LP)"
    return None
