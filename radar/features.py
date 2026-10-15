"""Every factor the scanner records about a token, computed only from what was knowable at
the moment of the snapshot. Missing values stay None: the learner handles gaps natively.

Naming: launch_* fairness of the launch, dist_* holder distribution, flow_* volume and
trades, px_* chart shape, wq_* wallet quality (learned), cr_* creator history (learned),
id_* identity / uniqueness / socials, sec_* contract safety, ctx_* market context.
"""
from __future__ import annotations

import calendar
import math
import re
import time
from collections import Counter

from .state import (B_BQ, B_C, B_H, B_IDX, B_L, B_NB, B_NEW, B_NS, B_SQ, BOUGHT, CURVE_TOKENS, FIRST_SLOT, FIRST_TS,
                    NB, NET, NS, SUPPLY, DexToken, PumpToken)

_NORM = re.compile(r"[^a-z0-9]+")


def norm_name(s: str) -> str:
    return _NORM.sub("", (s or "").lower())[:40]


def social_key(url: str) -> tuple[str, int]:
    """(handle or domain, kind) for an X / Telegram / website link. X kinds: 1 profile, 2 a single
    post, 3 community, 4 search or anything else (a post or search link is not the coin's own account)."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://(www\.|mobile\.)?", "", u).split("?")[0].rstrip("/")
    if not u:
        return "", 0
    parts = u.split("/")
    host = parts[0]
    if host in ("x.com", "twitter.com"):
        if len(parts) >= 3 and parts[1] == "i" and parts[2] == "communities":
            return "/".join(parts[:4]), 3
        if len(parts) >= 4 and parts[2] == "status":
            return parts[1], 2
        if len(parts) == 2 and parts[1] not in ("search", "home", "hashtag", "i"):
            return parts[1], 1
        return u, 4
    if host in ("t.me", "telegram.me"):
        return (parts[1] if len(parts) > 1 else ""), 1
    return host, 1


def _pct(x: float) -> float:
    return x / SUPPLY * 100.0


def _trend(bars: list, n: int) -> tuple[float | None, float | None, float | None, float | None]:
    """Over the last n closed bars: slope of log price per bar, R^2, volatility, share of up bars."""
    closes = [b[B_C] for b in bars[-n:] if b[B_C] > 0]
    k = len(closes)
    if k < 3:
        return None, None, None, None
    ys = [math.log(c) for c in closes]
    xm, ym = (k - 1) / 2, sum(ys) / k
    sxx = sum((i - xm) ** 2 for i in range(k))
    sxy = sum((i - xm) * (y - ym) for i, y in enumerate(ys))
    syy = sum((y - ym) ** 2 for y in ys)
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    rets = [ys[i] - ys[i - 1] for i in range(1, k)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    return slope, r2, vol, sum(1 for r in rets if r > 0) / len(rets)


def _price_at(tok: PumpToken, ts: float) -> float | None:
    """Close of the last bar that ended at or before ts."""
    target = int(ts // tok.bar_s)
    for b in reversed(tok.bars):
        if b[B_IDX] < target:
            return b[B_C]
    return tok.p0


def identity_features(tok, now: float) -> dict:
    """Name / image / social uniqueness - shared by both families. tok.seen was captured at
    creation, tok.meta arrives a little later from the metadata fetch."""
    s, m = tok.seen or {}, tok.meta or {}
    f = {
        "id_name_prev_n": s.get("name_n"), "id_name_burst_n": s.get("name_win"),
        "id_name_age_s": (tok.created_ts - s["name_first"]) if s.get("name_first") else None,
        "id_name_after_n": s.get("name_after"),      # copycats launched since: a sign the meme is spreading
        "id_sym_prev_n": s.get("sym_n"), "id_uri_prev_n": s.get("uri_n"),
        "id_name_len": len(tok.name or ""), "id_sym_len": len(tok.symbol or ""),
        "id_name_words": len((tok.name or "").split()),
        "id_name_ascii": int((tok.name or "").isascii()),
        "id_sym_eq_name": int(norm_name(tok.name) == norm_name(tok.symbol)),
        "id_meta_ok": int(bool(m.get("ok"))),
    }
    if m:
        f.update({
            "id_has_x": int(bool(m.get("x"))), "id_x_kind": m.get("x_kind", 0), "id_has_tg": int(bool(m.get("tg"))),
            "id_has_web": int(bool(m.get("web"))),
            "id_socials_n": int(bool(m.get("x"))) + int(bool(m.get("tg"))) + int(bool(m.get("web"))),
            "id_x_prev_n": m.get("x_prev"), "id_tg_prev_n": m.get("tg_prev"), "id_web_prev_n": m.get("web_prev"),
            "id_img_prev_n": m.get("img_prev"), "id_tg_members": m.get("tg_members"),
            "id_desc_len": len(m.get("desc") or ""),
        })
    for k in ("dex_paid", "dex_boost"):
        if k in m:
            f["id_" + k] = m[k]
    for k, v in (m.get("llm") or {}).items():
        f[("_llm" if k.startswith("_") else "llm_") + k] = v
    return f


def pump_features(tok: PumpToken, cp: int, now: float, ctx: dict) -> dict:
    w, devs, slot0 = tok.wallets, tok.devs, tok.created_slot
    age = max(now - tok.created_ts, 1.0)
    f: dict = {"cp": cp, "age_s": age, "chain": "solana", "launchpad": "pump.fun", "source": "stream"}
    f.update(tok.flags)
    f["quote_kind"] = ctx.get("quote_kind", 3)
    # contract safety, asked of pump.fun coins exactly as of any other token (GoPlus Solana)
    f.update({k: v for k, v in (tok.security or {}).items() if not k.startswith(("_", "n_"))})
    if not tok.graduated_ts:
        f["sec_lp_locked_pct"] = 100.0          # liquidity sits in the bonding curve: nobody can pull it

    # ---- price / chart
    price, usd = tok.price, ctx.get("quote_usd")
    f["px_mcap_usd"] = tok.mcap_q() * usd if usd else None
    f["px_mult"] = price / tok.p0
    f["px_high_mult"] = tok.max_price / tok.p0
    f["px_dd_pct"] = (1 - price / tok.max_price) * 100
    f["px_since_high_s"] = now - tok.max_ts
    f["px_curve_pct"] = (1 - tok.curve_left / CURVE_TOKENS) * 100
    for label, back in (("30s", 30), ("60s", 60), ("half", age / 2)):
        ref = _price_at(tok, now - back)
        f[f"px_ret_{label}"] = (price / ref - 1) * 100 if ref else None
    bars = tok.bars + ([tok.bar] if tok.bar else [])
    f["px_slope"], f["px_r2"], f["px_vol"], f["px_up_share"] = _trend(bars, 20)
    lows = [b[B_L] for b in bars[-9:]]
    f["px_higher_lows"] = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    f["px_bars_n"] = len(bars)

    # ---- flow
    total_q = tok.buy_q + tok.sell_q
    f["flow_buys"], f["flow_sells"] = tok.n_buys, tok.n_sells
    f["flow_buy_share_n"] = tok.n_buys / max(tok.n_buys + tok.n_sells, 1)
    f["flow_buy_share_q"] = tok.buy_q / total_q if total_q else None
    f["flow_vol_usd"] = total_q * usd if usd else None
    f["flow_net_usd"] = (tok.buy_q - tok.sell_q) * usd if usd else None
    f["flow_vol_per_mcap"] = total_q / tok.mcap_q() if tok.mcap_q() else None
    f["flow_trades_per_min"] = (tok.n_buys + tok.n_sells) / age * 60
    cut = int((now - 60) // tok.bar_s)
    prev_cut = int((now - 120) // tok.bar_s)
    last = [b for b in bars if b[B_IDX] >= cut]
    prev = [b for b in bars if prev_cut <= b[B_IDX] < cut]
    lq = sum(b[B_BQ] + b[B_SQ] for b in last)
    pq = sum(b[B_BQ] + b[B_SQ] for b in prev)
    ln = sum(b[B_NB] + b[B_NS] for b in last)
    f["flow_last60_trades"] = ln
    f["flow_last60_vol_share"] = lq / total_q if total_q else None
    f["flow_last60_buy_share"] = sum(b[B_BQ] for b in last) / lq if lq else None
    f["flow_accel"] = lq / pq if pq else None
    f["flow_last60_new_buyers"] = sum(b[B_NEW] for b in last)
    # the same names every launchpad gets, so one scorecard and one model cover all of them
    f["flow_buy_share_recent"] = f["flow_last60_buy_share"]
    f["flow_buys_per_min"] = sum(b[B_NB] for b in last)
    f["px_liq_usd"] = tok.real_q * usd if usd else None
    f["px_liq_per_mcap"] = tok.real_q / tok.mcap_q() if tok.mcap_q() else None
    f["px_on_curve"] = 0 if tok.graduated_ts else 1
    f["flow_idle_s"] = now - tok.last_trade_ts
    f["flow_max_buy_pct"] = _pct(tok.max_buy_tok)
    f["flow_max_sell_pct"] = _pct(tok.max_sell_tok)
    f["flow_first_buy_s"] = (tok.first_buy_ts - tok.created_ts) if tok.first_buy_ts else None
    buys_q = sorted(t[5] for t in tok.early if t[3])
    if buys_q:
        f["flow_med_buy_usd"] = buys_q[len(buys_q) // 2] * usd if usd else None
        f["flow_avg_buy_usd"] = sum(buys_q) / len(buys_q) * usd if usd else None

    # ---- launch fairness
    dev_net = dev_bought = slot0_bought = slot0_net = snip_bought = snip_net = 0.0
    slot0_n = snip_n = 0
    holders = []
    exited = repeat = 0
    for a, r in w.items():
        if a in devs:
            dev_net += max(r[NET], 0.0)
            dev_bought += r[BOUGHT]
            continue
        d = r[FIRST_SLOT] - slot0
        if d <= 0:
            slot0_n += 1
            slot0_bought += r[BOUGHT]
            slot0_net += max(r[NET], 0.0)
        elif d <= tok.sniper_slots:
            snip_n += 1
            snip_bought += r[BOUGHT]
            snip_net += max(r[NET], 0.0)
        if r[NET] > 1.0:
            holders.append(r[NET])
        elif r[BOUGHT] > 0:
            exited += 1
        if r[NB] >= 2:
            repeat += 1
    f["launch_dev_buy_pct"] = _pct(dev_bought)
    f["launch_dev_hold_pct"] = _pct(dev_net)
    f["launch_dev_sold_pct"] = (tok.role_sold["dev"] / dev_bought * 100) if dev_bought > 0 else 0.0
    f["launch_slot0_n"], f["launch_slot0_pct"] = slot0_n, _pct(slot0_bought)
    f["launch_slot0_hold"] = slot0_net / slot0_bought if slot0_bought > 0 else None
    f["launch_sniper_n"], f["launch_sniper_pct"] = snip_n, _pct(snip_bought)
    f["launch_sniper_hold"] = snip_net / snip_bought if snip_bought > 0 else None
    f["launch_bundle_pct"] = _pct(slot0_bought + snip_bought)
    f["launch_bundle_held_pct"] = _pct(slot0_net + snip_net)
    f["launch_phantom_sold_pct"] = _pct(tok.phantom_sold)
    # coordinated buying after the open: several wallets in one slot, or many identical sizes
    per_slot: dict[int, set] = {}
    slot_tok: Counter = Counter()
    sizes: Counter = Counter()
    n_early_buys = 0
    for ts, slot, a, is_buy, t, q in tok.early:
        if not is_buy or a in devs:
            continue
        n_early_buys += 1
        per_slot.setdefault(slot, set()).add(a)
        slot_tok[slot] += t
        if q > 0:
            sizes[float(f"{q:.2g}")] += 1
    f["launch_cluster_pct"] = _pct(sum(slot_tok[s] for s, ws in per_slot.items() if len(ws) >= 3 and s > slot0))
    top_size = sizes.most_common(1)
    f["launch_same_size_share"] = (top_size[0][1] / n_early_buys) if top_size and top_size[0][1] >= 3 else 0.0

    # ---- distribution
    holders.sort(reverse=True)
    f["dist_holders"] = len(holders)
    f["dist_buyers"] = len(w)
    f["dist_exited_share"] = exited / len(w) if w else None
    f["dist_repeat_share"] = repeat / len(w) if w else None
    f["dist_holders_per_min"] = len(holders) / age * 60
    held = sum(holders)
    f["dist_top1_pct"] = _pct(holders[0]) if holders else 0.0
    f["dist_top5_pct"] = _pct(sum(holders[:5]))
    f["dist_top10_pct"] = _pct(sum(holders[:10]) + 0.0)
    f["dist_top10_with_dev_pct"] = _pct(sum(sorted(holders[:10] + [dev_net], reverse=True)[:10]))
    f["dist_hhi"] = sum((h / held) ** 2 for h in holders) if held else None
    f["dist_median_pct"] = _pct(holders[len(holders) // 2]) if holders else None
    f["dist_sold_out_pct"] = _pct(CURVE_TOKENS - tok.curve_left)     # share of supply out of the curve

    # ---- learned: wallets and creator
    wq = ctx.get("wq") or {}
    f.update({k: v for k, v in wq.items() if not k.startswith("_")})
    if wq.get("_smart"):
        f["wq_smart_holding_n"] = sum(1 for a in wq["_smart"] if a in w and w[a][NET] > 1.0)
    f.update(creator_features(tok, ctx))
    f.update(identity_features(tok, now))
    f.update(context_features(now, ctx))
    return f


def apply_gmgn(f: dict, g: dict | None, feed: dict | None, own_trades: bool) -> dict:
    """Add GMGN's numbers (gm_*) and fill the shared factor names the launchpad's own data left
    empty. pump.fun factors rebuilt trade by trade keep priority (`own_trades`); elsewhere GMGN's
    holder, bundler, sniper and safety figures stand in. Everything GMGN says is also kept under its
    own gm_* name, so the learner can weigh both."""
    for src in (feed, g):
        if src:
            f.update({k: v for k, v in src.items() if not k.startswith("_")})
    g, feed = g or {}, feed or {}

    def fill(k, v):
        if v is not None and f.get(k) is None:
            f[k] = v

    fill("hx_insider_pct", g.get("gm_insider_pct"))
    fill("hx_phishing_pct", g.get("gm_phishing_pct"))
    fill("dist_holders", g.get("gm_holders"))
    fill("dist_top10_with_dev_pct", g.get("gm_top10_pct"))
    fill("launch_dev_hold_pct", g.get("gm_dev_hold_pct"))
    fill("launch_slot0_pct", g.get("gm_bundler_pct"))
    fill("launch_sniper_pct", g.get("gm_sniper_hold_pct"))
    fill("px_curve_pct", g.get("gm_progress_pct"))
    if g.get("gm_sec_ok"):
        fill("sec_checked", 1)
        fill("sec_honeypot", g.get("gm_honeypot"))
        fill("sec_tax_max", g.get("gm_tax_max"))
        fill("sec_open_source", g.get("gm_open_source"))
        if f.get("chain") == "solana":                 # mint / freeze authority are Solana ideas
            rm, rf = g.get("gm_renounced_mint"), g.get("gm_renounced_freeze")
            if rm is not None:
                fill("sec_mintable", 1 - rm)
            if rf is not None:
                fill("sec_blacklist", 1 - rf)
            if rm is not None and rf is not None:
                fill("sec_renounced", int(rm and rf))
        else:
            fill("sec_blacklist", g.get("gm_blacklist"))
            fill("sec_renounced", g.get("gm_renounced"))
        if not f.get("px_on_curve"):
            fill("sec_lp_locked_pct", g.get("gm_burnt_pct"))
    # identity: reuse counts and paid promotion - take the larger of ours and GMGN's
    for ours, theirs in (("id_img_prev_n", ("gm_image_dup_n", "gm_image_dup_feed_n")),
                         ("id_x_prev_n", ("gm_twitter_dup_n",)), ("id_tg_prev_n", ("gm_telegram_dup_n",)),
                         ("id_web_prev_n", ("gm_website_dup_n",)), ("id_dex_paid", ("gm_dex_paid",))):
        vals = [v for v in [f.get(ours)] + [g.get(t, feed.get(t)) for t in theirs] if v is not None]
        if vals:
            f[ours] = max(vals)
    if not f.get("id_meta_ok") and g.get("gm_ok"):
        f["id_meta_ok"] = 1
        f["id_has_x"], f["id_has_tg"], f["id_has_web"] = g.get("gm_has_x"), g.get("gm_has_tg"), g.get("gm_has_web")
        f["id_socials_n"] = sum(int(bool(g.get(k))) for k in ("gm_has_x", "gm_has_tg", "gm_has_web"))
        if g.get("gm_has_x"):
            f["id_x_kind"] = 2 if feed.get("gm_x_is_post") else 1
    return f


# which provider stands in first for a shared factor (GMGN is applied before these, our own data before all)
INTEL_ORDER = ("cx", "st", "gti", "rc")


def apply_intel(f: dict, results: list[dict | None]) -> dict:
    """Add Codex / Solana Tracker / GeckoTerminal / RugCheck results (each under its own names) and fill
    the shared factor names still empty - same rule as apply_gmgn: measured beats reported."""
    rs = [r for r in results if r]
    for r in rs:
        f.update({k: v for k, v in r.items() if not k.startswith(("_", "n_"))})

    def first(key):
        for r in rs:
            if r.get(key) is not None:
                return r[key]
        return None

    def fill(k, v):
        if v is not None and f.get(k) is None:
            f[k] = v

    fill("dist_holders", first("n_holders"))
    fill("dist_top10_with_dev_pct", first("n_top10_pct"))
    fill("launch_dev_hold_pct", first("n_dev_pct"))
    fill("launch_slot0_pct", first("n_bundler_pct"))
    fill("launch_sniper_pct", first("n_sniper_pct"))
    fill("hx_insider_pct", first("n_insider_pct"))
    # contract safety from Solana Tracker / GeckoTerminal when neither GMGN nor GoPlus answered
    if not f.get("sec_checked"):
        mint = first("st_mint_authority") if any("st_ok" in r for r in rs) else first("gti_mint_authority")
        freeze = first("st_freeze_authority") if any("st_ok" in r for r in rs) else first("gti_freeze_authority")
        if f.get("chain") == "solana" and mint is not None and freeze is not None:
            f.update({"sec_checked": 1, "sec_mintable": mint, "sec_blacklist": freeze,
                      "sec_renounced": int(not mint and not freeze)})
            fill("sec_honeypot", first("gti_honeypot"))
    if first("st_rugged"):
        f["rc_rugged"] = 1                             # the shared "flagged as rugged" safety veto
    if not f.get("px_on_curve"):
        fill("sec_lp_locked_pct", first("st_lp_burn_pct"))
    return f


def creator_features(tok, ctx: dict) -> dict:
    """The creator's record, learned from their earlier launches (any launchpad)."""
    s, cr = tok.seen or {}, ctx.get("creator")
    if "cr_prev" not in s:
        return {}
    f = {"cr_prev_n": s.get("cr_prev"), "cr_day_n": s.get("cr_day")}
    if cr is not None:
        f.update({"cr_snap_n": cr["n_snap"], "cr_grad_n": cr["n_grad"], "cr_win_n": cr["n_win"],
                  "cr_dead_n": cr["n_dead"], "cr_best_mult": cr["best_mult"]})
    return f


def context_features(now: float, ctx: dict) -> dict:
    t = time.gmtime(now)
    return {"ctx_hour": t.tm_hour, "ctx_dow": t.tm_wday, "ctx_launches_per_min": ctx.get("launches_per_min"),
            "ctx_recent_hit_rate": ctx.get("recent_hit_rate"), "ctx_recent_grads": ctx.get("recent_grads")}


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def dex_features(tok: DexToken, cp: int, now: float, ctx: dict) -> dict:
    ts, p = tok.ticks[-1]
    age = max(now - tok.created_ts, 1.0)
    f: dict = {"cp": cp, "age_s": age, "chain": tok.chain, "launchpad": tok.launchpad}
    tx, vol, chg = p.get("txns") or {}, p.get("volume") or {}, p.get("priceChange") or {}
    liq = _num((p.get("liquidity") or {}).get("usd"))
    mcap = _num(p.get("marketCap")) or _num(p.get("fdv"))
    f["px_mcap_usd"], f["px_liq_usd"] = mcap, liq
    f["px_liq_per_mcap"] = liq / mcap if liq and mcap else None
    f["px_mult"] = tok.price / tok.p0 if tok.p0 else None
    f["px_high_mult"] = tok.max_price / tok.p0 if tok.p0 else None
    f["px_dd_pct"] = (1 - tok.price / tok.max_price) * 100 if tok.max_price else None
    f["px_since_high_s"] = now - tok.max_ts
    f["px_chg_m5"], f["px_chg_h1"] = _num(chg.get("m5")), _num(chg.get("h1"))
    closes = [float(q.get("priceUsd") or 0) for _, q in tok.ticks[-20:]]
    fake_bars = [[i, c, c, c, c] for i, c in enumerate(closes)]
    f["px_slope"], f["px_r2"], f["px_vol"], f["px_up_share"] = _trend(fake_bars, 20)
    for win in ("m5", "h1"):
        b, s = (tx.get(win) or {}).get("buys", 0), (tx.get(win) or {}).get("sells", 0)
        f[f"flow_buys_{win}"], f[f"flow_sells_{win}"] = b, s
        f[f"flow_buy_share_{win}"] = b / (b + s) if b + s else None
        f[f"flow_vol_{win}_usd"] = _num(vol.get(win))
    f["flow_vol_per_mcap"] = f["flow_vol_h1_usd"] / mcap if f["flow_vol_h1_usd"] and mcap else None
    f["flow_trades_per_min"] = (f["flow_buys_h1"] + f["flow_sells_h1"]) / min(age, 3600) * 60
    f["flow_m5_share"] = f["flow_vol_m5_usd"] / f["flow_vol_h1_usd"] if f["flow_vol_h1_usd"] else None
    f["flow_idle_s"] = now - tok.last_change_ts if tok.last_change_ts else None
    f["id_dex_boost"] = (p.get("boosts") or {}).get("active", 0)
    f["id_dex_paid"] = int(bool((p.get("info") or {}).get("socials") or (p.get("info") or {}).get("websites")))

    t = tok.trades or {}
    for k in ("launch_block0_n", "launch_block0_pct", "launch_sniper_n", "launch_cluster_share",
              "launch_same_size_share", "dist_buyers", "dist_sellers", "dist_top1_buy_share", "dist_top5_buy_share",
              "flow_med_buy_usd", "flow_max_buy_usd", "flow_trades_seen"):
        if k in t:
            f[k] = t[k]
    wq = ctx.get("wq") or {}
    f.update({k: v for k, v in wq.items() if not k.startswith("_")})
    s = tok.security or {}
    f.update({k: v for k, v in s.items() if not k.startswith(("_", "n_"))})

    # the same factor names as pump.fun coins, from what this launchpad lets us see
    f["source"] = "poll"
    f["flow_buy_share_recent"] = f["flow_buy_share_m5"]
    f["flow_buys_per_min"] = f["flow_buys_m5"] / 5
    vol_now = f["flow_vol_m5_usd"]
    older = next((q for t_, q in reversed(tok.ticks) if ts - t_ >= 270), None)   # the previous 5-minute window
    vol_then = _num((older.get("volume") or {}).get("m5")) if older else None
    f["flow_accel"] = vol_now / vol_then if vol_now is not None and vol_then else None
    f["px_on_curve"] = int(bool(ctx.get("on_curve")))
    if f["px_on_curve"] and f.get("sec_lp_locked_pct") is None:
        f["sec_lp_locked_pct"] = 100.0                      # a bonding curve holds the liquidity
    f["launch_slot0_pct"] = t.get("launch_block0_pct")
    f["launch_sniper_pct"] = t.get("launch_sniper_pct")
    f["launch_cluster_pct"] = t["launch_cluster_share"] * 100 if t.get("launch_cluster_share") is not None else None
    holders = s.get("sec_holders") or s.get("rc_holders")
    f["dist_holders"] = holders
    # holder figures lag a new token on EVM scanners (at first the deployer "holds" everything)
    if (holders or 0) >= 20 or s.get("rc_top10_pct") is not None:
        top10 = s.get("sec_top10_pct")
        f["dist_top10_with_dev_pct"] = top10 if top10 is not None else s.get("rc_top10_pct")
        top1 = s.get("sec_top1_pct")
        f["dist_top1_pct"] = top1 if top1 is not None else s.get("rc_top1_pct")
        dev = s.get("sec_creator_pct")
        f["launch_dev_hold_pct"] = dev if dev is not None else s.get("rc_creator_pct")
    f.update(creator_features(tok, ctx))
    f.update(identity_features(tok, now))
    f.update(context_features(now, ctx))
    return f


def trade_features(trades: list[dict], token_addr: str) -> dict:
    """Wallet-level factors from GeckoTerminal's trade list (newest first, up to 300)."""
    rows = []
    for t in trades:
        a = t.get("attributes") or {}
        try:
            rows.append((a["block_number"], a["tx_from_address"], a["kind"] == "buy",
                         float(a.get("volume_in_usd") or 0),
                         float((a.get("price_to_in_usd") if a["kind"] == "buy" else a.get("price_from_in_usd")) or 0),
                         a.get("block_timestamp", "")))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return {}
    rows.sort(key=lambda r: r[0])
    complete = len(rows) < 300                 # the list is capped: only then do we see the very first block
    first_block = rows[0][0]
    buys = [r for r in rows if r[2]]
    buy_usd: Counter = Counter()
    per_block: dict[int, set] = {}
    sizes: Counter = Counter()
    entries: dict = {}
    for blk, w, _, usd, price, iso in buys:
        buy_usd[w] += usd
        per_block.setdefault(blk, set()).add(w)
        if usd > 0:
            sizes[float(f"{usd:.2g}")] += 1
        if w not in entries and len(entries) < 60:
            entries[w] = (_iso_ts(iso), price, blk == first_block and complete)
    total = sum(buy_usd.values())
    top = sorted(buy_usd.values(), reverse=True)
    b0 = {w for blk, w, is_buy, *_ in rows if is_buy and blk == first_block}
    out = {
        "flow_trades_seen": len(rows), "dist_buyers": len(buy_usd),
        "dist_sellers": len({r[1] for r in rows if not r[2]}),
        "dist_top1_buy_share": top[0] / total if total else None,
        "dist_top5_buy_share": sum(top[:5]) / total if total else None,
        "launch_cluster_share": sum(len(ws) for ws in per_block.values() if len(ws) >= 3) / max(len(buys), 1),
        "launch_same_size_share": (sizes.most_common(1)[0][1] / len(buys))
        if sizes and sizes.most_common(1)[0][1] >= 3 else 0.0,
        "flow_med_buy_usd": sorted(r[3] for r in buys)[len(buys) // 2] if buys else None,
        "flow_max_buy_usd": max((r[3] for r in buys), default=None),
        "entries": entries,
    }
    if complete:
        out["launch_block0_n"] = len(b0)
        out["launch_block0_pct"] = sum(buy_usd[w] for w in b0) / total * 100 if total else None
        snipers = {w for blk, w, is_buy, *_ in rows if is_buy and 0 < blk - first_block <= 3} - b0
        out["launch_sniper_n"] = len(snipers)
        out["launch_sniper_pct"] = sum(buy_usd[w] for w in snipers) / total * 100 if total else None
    return out


def _iso_ts(s: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0
