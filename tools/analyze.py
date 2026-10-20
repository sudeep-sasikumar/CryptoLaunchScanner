"""What do the coins that ran 2x / 4x / 10x / 100x have in common, and can we pick them early?

    .venv\\Scripts\\python.exe tools\\analyze.py                 (reads var/research/vps/*.jsonl.gz)
    .venv\\Scripts\\python.exe tools\\analyze.py --days 7 --checkpoint 180

Reads the day files tools/sync_research.py copied from the VPS and writes a plain report to
var/research/report-<date>.md (also printed). One row per token: its first finished snapshot at
the chosen checkpoint (default: the earliest one we have), i.e. only what was known at that moment.
"Ran Nx" means: from that snapshot's price (+3% slippage) it reached N times before falling to half,
within 6 hours. Findings are evidence to test, not settings to switch on blindly.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar.export import NAME  # noqa: E402

TIERS = (2, 4, 10, 100)
BUCKETS = [("under 2x", 0, 2), ("2-4x", 2, 4), ("4-10x", 4, 10), ("10-100x", 10, 100), ("100x+", 100, float("inf"))]
CATEGORICAL = ("launchpad", "chain", "source")
SKIP = {"cp", "ctx_dow"}                      # bookkeeping, not traits

LABELS = {
    "age_s": "age (s)", "dist_holders": "holders", "dist_top10_pct": "top 10 hold %",
    "dist_top10_with_dev_pct": "top 10 incl. dev %", "dist_top1_pct": "largest holder %",
    "dist_exited_share": "share of buyers who already sold", "flow_buy_share_recent": "recent buy share",
    "flow_buys_per_min": "buys / min", "flow_trades_per_min": "trades / min", "flow_accel": "volume acceleration",
    "flow_vol_per_mcap": "volume / market cap", "flow_vol_m5_usd": "5-min volume $", "px_dd_pct": "drop from high %",
    "px_mcap_usd": "market cap $", "px_mult": "price vs first seen", "px_high_mult": "high vs first seen",
    "px_slope": "trend slope", "px_r2": "trend smoothness", "px_liq_usd": "liquidity $",
    "px_liq_per_mcap": "liquidity / market cap", "launch_slot0_pct": "bundled at launch %",
    "launch_sniper_pct": "sniped %", "launch_cluster_pct": "coordinated buying %",
    "launch_dev_sold_pct": "dev sold %", "launch_dev_hold_pct": "dev holds %", "hx_insider_pct": "insiders %",
    "hx_phishing_pct": "phishing wallets %", "hx_bundler_pct": "bundlers %", "hx_sniper_pct": "snipers %",
    "id_socials_n": "number of socials", "id_has_x": "has X", "id_has_tg": "has Telegram", "id_has_web": "has website",
    "id_name_prev_n": "same name launched before", "id_img_prev_n": "image reused", "id_name_burst_n": "copies in a burst",
    "id_dex_paid": "paid DexScreener profile", "gm_x_followers": "X followers", "wq_smart_n": "smart wallets in",
    "wq_toxic_share": "toxic wallet share", "cr_prev_n": "creator's earlier launches",
    "cr_win_n": "creator's earlier winners", "cr_day_n": "creator launches today", "sec_tax_max": "max tax %",
    "ctx_hour": "hour (UTC)", "ctx_launches_per_min": "launches / min (market)", "prior": "scorecard score",
    "prob": "model chance of 2x",
}


def label(k: str) -> str:
    return LABELS.get(k, k)


# ------------------------------------------------------------------------------------------- loading
def junk(s: dict, skip_pads=("fluxbeam",), min_mcap: float = 1000) -> bool:
    """Same filters the scanner now applies (sources.skip_launchpads, label.min_mcap_usd): older
    files still hold pool-farm launches and mispriced pools whose 'multiples' are not real."""
    if s.get("launchpad") in skip_pads:
        return True
    if s.get("family") == "pump" and (s.get("f") or {}).get("px_on_curve") == 0:
        return True                    # taken after graduation on the frozen curve price (fixed 2026-10-20)
    mc = s.get("mcap_usd")
    return s.get("family") == "dex" and ((mc is not None and mc < min_mcap) or (s.get("f") or {}).get("px_liq_usd") == 0)


def load(folder: Path, days: int | None = None) -> tuple[list[dict], list[dict], list[str]]:
    files = sorted(p for p in folder.glob("*.jsonl.gz") if NAME.fullmatch(p.name))
    if days:
        files = files[-days:]
    snaps, calls, bad = [], [], set()
    for p in files:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("kind") == "snapshot":
                    if junk(r):
                        bad.add(r.get("id"))
                    else:
                        snaps.append(r)
                elif r.get("kind") == "call":
                    calls.append(r)
    calls = [c for c in calls if c.get("snapshot_id") not in bad]
    return snaps, calls, [p.name[:10] for p in files]


def per_token(snaps: list[dict], checkpoint: int | None) -> list[dict]:
    """One row per token: the earliest snapshot (or the one at `checkpoint`), with the token's best
    peak over all of its snapshots kept separately."""
    best: dict[str, dict] = {}
    top: dict[str, float] = defaultdict(float)
    for s in snaps:
        top[s["token_id"]] = max(top[s["token_id"]], s.get("peak_mult") or 0)
        if checkpoint is not None and s.get("cp") != checkpoint:
            continue
        cur = best.get(s["token_id"])
        if cur is None or s["ts"] < cur["ts"]:
            best[s["token_id"]] = s
    rows = sorted(best.values(), key=lambda s: s["ts"])
    for r in rows:
        r["best_any"] = top[r["token_id"]]
    return rows


# ------------------------------------------------------------------------------------------- statistics
def auc(x: np.ndarray, y: np.ndarray) -> float | None:
    """Chance that a random winner has a higher value than a random loser (0.5 = no signal)."""
    ok = ~np.isnan(x)
    x, y = x[ok], y[ok]
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 < 5 or n0 < 5:
        return None
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    ranks = np.empty(len(x))
    i = 0
    while i < len(xs):                       # average ranks for ties
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def num(v) -> float:
    if v is None or isinstance(v, str):
        return np.nan
    if isinstance(v, bool):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def matrix(rows: list[dict]) -> tuple[np.ndarray, list[str]]:
    keys = sorted({k for r in rows for k, v in (r.get("f") or {}).items()
                   if k not in SKIP and k not in CATEGORICAL and not isinstance(v, str)})
    keys += ["prior", "prob"]
    X = np.array([[num((r.get("f") or {}).get(k)) if k not in ("prior", "prob") else num(r.get(k)) for k in keys]
                  for r in rows], dtype=float).reshape(len(rows), len(keys))
    return X, keys


def fmt(v: float | None, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 100:
        return f"{v:.0f}"
    return f"{v:.{digits}g}"


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "-"


def table(head: list[str], body: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return out


# ------------------------------------------------------------------------------------------- report
def load_pulls(folder: Path, days: int | None = None) -> list[dict]:
    """Liquidity look-ups (6 / 24 / 48 h after launch) of DEX coins, newest state per token."""
    files = sorted(p for p in folder.glob("*.jsonl.gz") if NAME.fullmatch(p.name))
    out: dict[str, dict] = {}
    for p in files[-days:] if days else files:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("kind") == "pull":
                    out[r["id"]] = r
    return list(out.values())


def pull_section(pulls: list[dict]) -> list[str]:
    L = ["## 6. Who pulls liquidity", ""]
    if not pulls:
        return L + ["_No liquidity look-ups in these files yet (they start 6 h after a coin launches)._", ""]
    by: dict[str, list] = defaultdict(list)
    for r in pulls:
        by[r.get("launchpad") or "?"].append(r)
    body = []
    for pad, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        gone = sum(1 for r in rs if r.get("pull_state") == 1)
        body.append([pad, len(rs), gone, pct(gone, len(rs))])
    L += ["DEX coins that once held $1,000+ looked up again; 'gone' = pools now hold under $50.", ""]
    L += table(["launchpad / pool type", "looked up", "liquidity gone", "share"], body) + [""]
    creators: dict[str, int] = defaultdict(int)
    for r in pulls:
        if r.get("pull_state") == 1 and r.get("creator"):
            creators[r["creator"]] += 1
    repeat = {c: n for c, n in creators.items() if n > 1}
    L += [f"Creators behind a lost-liquidity coin: {len(creators)}; with more than one: {len(repeat)}.", ""]
    return L


def report(rows: list[dict], calls: list[dict], days: list[str], checkpoint: int | None, top_n: int = 25,
           pulls: list[dict] | None = None) -> str:
    L: list[str] = []
    n = len(rows)
    peak = np.array([r.get("peak_mult") or 0.0 for r in rows])
    when = "earliest snapshot" if checkpoint is None else f"snapshot at {checkpoint // 60} min"
    L += [f"# What the runners have in common - {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC", "",
          f"Data: {len(days)} day(s) ({days[0] if days else '-'} to {days[-1] if days else '-'}), {n} tokens, "
          f"one row per token ({when}). 'Ran Nx' = reached N times the price at that moment (+3% slippage) "
          "before falling to half, within 6 h.", ""]
    if n == 0:
        L.append("No finished snapshots yet. Run tools\\sync_research.py first.")
        return "\n".join(L)

    # 1. how many ran
    L += ["## 1. How many ran", ""]
    body = []
    for name, lo, hi in BUCKETS:
        k = int(((peak >= lo) & (peak < hi)).sum())
        body.append([name, k, pct(k, n)])
    L += table(["outcome", "tokens", "share"], body)
    L += ["", "At least: " + ", ".join(f"{t}x: {int((peak >= t).sum())} ({pct(int((peak >= t).sum()), n)})"
                                         for t in TIERS), ""]

    # 2. by launchpad / chain / source
    L += ["## 2. Where the runners come from", ""]
    for cat in CATEGORICAL:
        groups: dict[str, list[float]] = defaultdict(list)
        for r, p in zip(rows, peak):
            groups[str((r.get("f") or {}).get(cat) or r.get(cat) or "?")].append(p)
        body = []
        for g, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            a = np.array(ps)
            body.append([g, len(a)] + [pct(int((a >= t).sum()), len(a)) for t in TIERS])
        if len(body) > 1 or cat == "launchpad":
            L += [f"**{cat}**", ""] + table([cat, "tokens"] + [f"{t}x+" for t in TIERS], body[:15]) + [""]

    # 3. traits
    X, keys = matrix(rows)
    L += ["## 3. Traits that separate runners from the rest", "",
          "Typical (median) value in each group, and how well the factor separates on its own "
          "(separation: 0.50 = none, 1.00 = perfect; below 0.50 means *lower* is better). "
          "Coverage = share of tokens where the factor was known.", ""]
    for t in (2, 4, 10):
        y = (peak >= t).astype(int)
        if y.sum() < 5:
            L += [f"_{t}x+: fewer than 5 tokens - not enough to compare yet._", ""]
            continue
        scored = []
        for j, k in enumerate(keys):
            col = X[:, j]
            known = ~np.isnan(col)
            if known.mean() < 0.2 or np.nanstd(col) == 0:
                continue
            a = auc(col, y)
            if a is not None:
                scored.append((abs(a - 0.5), a, j, k, known.mean()))
        scored.sort(reverse=True)
        body = []
        for _, a, j, k, cov in scored[:top_n]:
            col = X[:, j]
            win, rest = col[(y == 1) & ~np.isnan(col)], col[(y == 0) & ~np.isnan(col)]
            body.append([label(k), fmt(float(np.median(win))) if len(win) else "-",
                         fmt(float(np.median(rest))) if len(rest) else "-",
                         f"{a:.2f}", "higher" if a > 0.5 else "lower", f"{100 * cov:.0f}%"])
        L += [f"### Ran {t}x+ ({int(y.sum())} tokens) vs the rest", ""]
        L += table(["factor", f"{t}x+ median", "rest median", "separation", "better when", "coverage"], body) + [""]

    big = peak >= 10
    if big.sum() >= 3:
        L += ["### The biggest runners (10x+), one by one", ""]
        body = []
        for r, p in sorted(zip(rows, peak), key=lambda rp: -rp[1])[:20]:
            f = r.get("f") or {}
            body.append([r.get("symbol") or "?", r.get("launchpad") or f.get("launchpad") or "?", f"{p:.1f}x",
                         fmt(num(r.get("mcap_usd"))), fmt(num(f.get("dist_holders"))),
                         fmt(num(f.get("dist_top10_with_dev_pct", f.get("dist_top10_pct")))),
                         fmt(num(f.get("launch_slot0_pct"))), fmt(num(f.get("id_socials_n"))),
                         fmt(num(r.get("prior"))), "yes" if r.get("called") else "no"])
        L += table(["token", "launchpad", "ran", "mcap then", "holders", "top10 %", "bundled %", "socials",
                    "score", "called"], body) + [""]

    # 4. does our filter pick them?
    L += ["## 4. Does the radar pick them?", "",
          "Share of tokens that ran, among the top-ranked ones (later rows are a stricter filter).", ""]
    for col_name in ("prior", "prob"):
        s = np.array([num(r.get(col_name)) for r in rows])
        ok = ~np.isnan(s)
        if ok.sum() < 20:
            continue
        sv, pv = s[ok], peak[ok]
        order = np.argsort(-sv)
        body = []
        for share in (1.0, 0.2, 0.1, 0.05, 0.02, 0.01):
            k = max(1, int(round(share * len(sv))))
            sel = pv[order[:k]]
            body.append([f"top {share * 100:g}%" if share < 1 else "all", k, f">= {fmt(float(sv[order[k - 1]]))}"]
                        + [pct(int((sel >= t).sum()), k) for t in TIERS])
        L += [f"**by {label(col_name)}**", ""]
        L += table(["slice", "tokens", "cut-off"] + [f"{t}x+" for t in TIERS], body) + [""]

    if calls:
        mult = np.array([num(c.get("peak_mult")) for c in calls])
        ended = ~np.isnan(mult)
        L += [f"**Calls sent: {len(calls)}** (with an outcome so far: {int(ended.sum())})", ""]
        if ended.any():
            m = mult[ended]
            L += ["Of those with an outcome: " + ", ".join(f"{t}x+: {pct(int((m >= t).sum()), len(m))}"
                                                          for t in TIERS), ""]

    # 5. simple rules
    L += rules(X, keys, peak)
    if pulls is not None:
        L += pull_section(pulls)
    L += ["## Caveats", "",
          "- Only tokens active enough to be snapshotted are here; dead-on-arrival launches never enter the data.",
          "- One or two days is a small, single market regime: treat patterns as leads until they hold for a week+.",
          "- 'Separation' looks at one factor at a time; the learned model combines them; it is retrained only when asked (tools/retrain.py).",
          ""]
    return "\n".join(L)


def rules(X: np.ndarray, keys: list[str], peak: np.ndarray, target: float = 4.0) -> list[str]:
    """A shallow tree learned on the older 70% of tokens and checked on the newer 30%."""
    L = [f"## 5. Simple rules for {target:g}x+ (learned on older tokens, checked on newer ones)", ""]
    y = (peak >= target).astype(int)
    n = len(y)
    cut = int(n * 0.7)
    if n < 300 or y[:cut].sum() < 15 or y[cut:].sum() < 5:
        return L + [f"_Not enough data yet ({n} tokens, {int(y.sum())} reached {target:g}x). Needs a few more days._", ""]
    from sklearn.tree import DecisionTreeClassifier
    use = [j for j, k in enumerate(keys) if k not in ("prior", "prob") and np.mean(~np.isnan(X[:, j])) >= 0.3]
    Xs = np.nan_to_num(X[:, use], nan=-1.0)          # "unknown" sorts below every real value
    tree = DecisionTreeClassifier(max_depth=3, min_samples_leaf=max(30, n // 100), class_weight="balanced",
                                  random_state=0).fit(Xs[:cut], y[:cut])
    base_tr, base_te = y[:cut].mean(), y[cut:].mean()
    leaf_tr, leaf_te = tree.apply(Xs[:cut]), tree.apply(Xs[cut:])
    t = tree.tree_
    paths: dict[int, list[str]] = {}

    def walk(node: int, conds: list[str]) -> None:
        if t.children_left[node] == -1:
            paths[node] = conds
            return
        k, thr = keys[use[t.feature[node]]], t.threshold[node]
        walk(t.children_left[node], conds + [f"{label(k)} <= {fmt(float(thr))}"])
        walk(t.children_right[node], conds + [f"{label(k)} > {fmt(float(thr))}"])
    walk(0, [])
    body = []
    for leaf, conds in paths.items():
        tr, te = y[:cut][leaf_tr == leaf], y[cut:][leaf_te == leaf]
        if len(tr) == 0:
            continue
        lift = (te.mean() / base_te) if len(te) and base_te else float("nan")
        body.append((tr.mean(), [" AND ".join(conds) or "(all)", len(tr), pct(int(tr.sum()), len(tr)),
                                 len(te), pct(int(te.sum()), len(te)) if len(te) else "-",
                                 f"{lift:.1f}x" if lift == lift else "-"]))
    body.sort(key=lambda b: -b[0])
    L += [f"Base rate: {100 * base_tr:.1f}% (older) / {100 * base_te:.1f}% (newer). A rule is only worth "
          "trusting if its newer-token rate stays well above the base rate.", ""]
    L += table(["rule", "older tokens", f"{target:g}x+ rate", "newer tokens", f"{target:g}x+ rate", "lift (newer)"],
               [b[1] for b in body]) + [""]
    return L


def write_report(folder: Path, out: Path, days: int | None = None, checkpoint: int | None = None) -> str:
    snaps, calls, names = load(folder, days)
    text = report(per_token(snaps, checkpoint), calls, names, checkpoint, pulls=load_pulls(folder, days))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text


def default_out(cfg) -> Path:
    return cfg.data_dir / "research" / f"report-{time.strftime('%Y-%m-%d', time.gmtime())}.md"


def main() -> int:
    from radar.config import load_config
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Traits of 2x/4x/10x/100x runners")
    ap.add_argument("--dir", default=str(cfg.data_dir / "research" / "vps"))
    ap.add_argument("--days", type=int, default=None, help="only the newest N days")
    ap.add_argument("--checkpoint", type=int, default=None, help="use the snapshot at this many seconds (60/180/600/1800)")
    ap.add_argument("--out", default=None, help="report file (default var/research/report-<date>.md)")
    a = ap.parse_args()
    out = Path(a.out) if a.out else default_out(cfg)
    text = write_report(Path(a.dir), out, a.days, a.checkpoint)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    print(f"\n(saved to {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
