"""The learner: turns labelled snapshots into a probability that a token 2x's from here.

Every retrain is a small honest experiment, split by time (never shuffled - tomorrow must be
predicted from yesterday):

    oldest 70% of tokens  -> fit          |  the challenger is scored on the newest 15%,
    next 15%              -> calibrate    |  which it has never seen, against (a) the hand
    newest 15%            -> test         |  scorecard and (b) the model currently in use.

It is promoted only if it clearly beats the scorecard (and chance) and is no worse than the
current model there.
The promoted version is then refitted on the first 85% and calibrated on the newest 15%, and
its call threshold is set where that newest slice shows the target hit rate.

Alongside the model, a plain "what separates winners" table is rebuilt (hit rate per value
range of every factor) - readable without trusting any model.
"""
from __future__ import annotations

import json
import logging
import math
import pickle
import sqlite3
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("model")
CATEGORICAL = ("chain", "launchpad")
HALF_LIFE_DAYS = 30.0


# ------------------------------------------------------------------------------- data
class Dataset:
    """Labelled snapshots as a float32 matrix. Built in two passes over the stored rows so that no
    per-row dictionaries are ever held: 250k snapshots take ~150 MB instead of several GB."""

    def __init__(self, db_path: Path, family: str, max_rows: int, min_share: float = 0.02):
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            self.layouts = {i: tuple(json.loads(k)) for i, k in con.execute("SELECT id, keys FROM feature_keys")}
            # a 2x is final the moment it happens (hit=1), a loss when the label is done
            rows = con.execute(
                "SELECT token_id, ts, fkeys, features, prior, hit FROM snapshots WHERE family=? AND tainted=0 "
                "AND (done=1 OR hit=1) ORDER BY token_created_ts DESC, id DESC LIMIT ?", (family, max_rows)).fetchall()
        finally:
            con.close()
        rows.reverse()
        counts: dict[str, int] = {}
        cats: dict[str, set] = {c: set() for c in CATEGORICAL}
        for r in rows:
            for k, v in self.items(r):
                if k in cats:
                    if v is not None:
                        cats[k].add(str(v))
                elif isinstance(v, (int, float)) and not k.startswith("_"):
                    counts[k] = counts.get(k, 0) + 1
        need = max(5, min_share * len(rows))
        self.names = sorted(k for k, n in counts.items() if n >= need)
        self.vocab = {c: {v: i for i, v in enumerate(sorted(s))} for c, s in cats.items()}
        self.tokens = [r[0] for r in rows]
        self.ts = np.array([r[1] for r in rows], dtype=np.float64)
        self.prior = np.array([r[4] or 0.0 for r in rows], dtype=np.float64)
        self.y = np.array([int(r[5] or 0) for r in rows], dtype=np.int8)
        self.rows = rows
        self.X = self.build(rows, self.names, self.vocab)

    def items(self, r):
        v = json.loads(r[3])
        return v.items() if isinstance(v, dict) else zip(self.layouts.get(r[2], ()), v)

    def build(self, rows, names: list[str], vocab: dict) -> np.ndarray:
        col = {k: j for j, k in enumerate(names)}
        cats = [c for c in CATEGORICAL if vocab.get(c)]
        cat_col = {c: len(names) + j for j, c in enumerate(cats)}
        X = np.full((len(rows), len(names) + len(cats)), np.nan, dtype=np.float32)
        for i, r in enumerate(rows):
            for k, v in self.items(r):
                j = col.get(k)
                if j is not None:
                    if isinstance(v, (int, float)):
                        X[i, j] = v
                elif k in cat_col:
                    code = vocab[k].get(str(v))
                    if code is not None:
                        X[i, cat_col[k]] = code
        return X


def matrix(feats: list[dict], names: list[str], vocab: dict) -> np.ndarray:
    cats = [c for c in CATEGORICAL if vocab.get(c)]
    X = np.full((len(feats), len(names) + len(cats)), np.nan, dtype=np.float64)
    for i, f in enumerate(feats):
        for j, k in enumerate(names):
            v = f.get(k)
            if v is not None and not isinstance(v, (str, list, dict)):
                X[i, j] = v
        for j, c in enumerate(cats):
            code = vocab[c].get(str(f.get(c)))
            if code is not None:
                X[i, len(names) + j] = code
    return X


# ------------------------------------------------------------------------------- metrics
def precision_at(y: np.ndarray, score: np.ndarray, share: float) -> float | None:
    k = max(1, int(round(len(y) * share)))
    if len(y) < 20:
        return None
    top = np.argsort(-score, kind="stable")[:k]
    return float(y[top].mean())


def evaluate(y: np.ndarray, score: np.ndarray) -> dict:
    from sklearn.metrics import average_precision_score, roc_auc_score
    if y.sum() == 0 or y.sum() == len(y):
        return {"ap": None, "auc": None}
    return {"ap": float(average_precision_score(y, score)), "auc": float(roc_auc_score(y, score)),
            "p_top1": precision_at(y, score, 0.01), "p_top2": precision_at(y, score, 0.02),
            "p_top5": precision_at(y, score, 0.05), "p_top10": precision_at(y, score, 0.10)}


def chance_ap(y: np.ndarray, rounds: int = 200) -> float:
    """What average precision luck alone reaches on this slice (99th percentile of shuffled rankings)."""
    from sklearn.metrics import average_precision_score
    rng = np.random.default_rng(7)
    return float(np.quantile([average_precision_score(y, rng.random(len(y))) for _ in range(rounds)], 0.99))


def pick_threshold(y: np.ndarray, prob: np.ndarray, target: float, floor: float, fallback_share: float) -> dict:
    """Lowest probability cut whose calls would have hit `target` of the time on this slice.
    Cuts are tried only at distinct probability values (calibration produces many ties), and a
    cut needs enough calls behind it that the hit rate is not a fluke."""
    min_calls = max(20, int(0.005 * len(y)))
    thr, how = None, "target"
    for v in np.unique(prob)[::-1]:
        sel = prob >= v
        n, hits = int(sel.sum()), float(y[sel].sum())
        if n < min_calls:
            continue
        p = hits / n
        lower = (p + 1 / (2 * n) - math.sqrt(p * (1 - p) / n + 1 / (4 * n * n))) / (1 + 1 / n)   # Wilson, z = 1
        if p >= target and lower >= 0.8 * target:
            thr = float(v)
        elif thr is not None:
            break
    if thr is None:
        # the target hit rate is not reachable yet: call the best slice instead
        how = "top-share"
        thr = float(np.quantile(prob, 1 - fallback_share))
    thr = max(thr, floor)
    sel = prob >= thr
    return {"thr": thr, "how": how, "calls_share": float(sel.mean()),
            "expected_precision": float(y[sel].mean()) if sel.any() else None, "n_calls": int(sel.sum())}


# ------------------------------------------------------------------------------- lift table
def lift_table(X: np.ndarray, y: np.ndarray, names: list[str], bins: int) -> list[dict]:
    """For each factor: hit rate in each value range, strongest separators first."""
    y = y.astype(float)
    base = float(y.mean()) if len(y) else 0.0
    out = []
    for j, k in enumerate(names):
        vals = X[:, j].astype(np.float64)
        have = ~np.isnan(vals)
        if have.sum() < 100:
            continue
        v, yy = vals[have], y[have]
        edges = np.unique(np.quantile(v, np.linspace(0, 1, bins + 1)))
        if len(edges) < 3:
            groups = [(float(u), float(u), v == u) for u in np.unique(v)[:bins]]
        else:
            groups = []
            for i in range(len(edges) - 1):
                hi_incl = i == len(edges) - 2
                mask = (v >= edges[i]) & ((v <= edges[i + 1]) if hi_incl else (v < edges[i + 1]))
                groups.append((float(edges[i]), float(edges[i + 1]), mask))
        cells = [{"lo": lo, "hi": hi, "n": int(m.sum()), "rate": float(yy[m].mean())}
                 for lo, hi, m in groups if m.sum() >= 30]
        if len(cells) < 2:
            continue
        rates = [c["rate"] for c in cells]
        out.append({"feature": k, "coverage": float(have.mean()), "cells": cells,
                    "spread": max(rates) - min(rates),
                    "best": max(cells, key=lambda c: c["rate"]), "worst": min(cells, key=lambda c: c["rate"]),
                    "missing_rate": float(y[~have].mean()) if (~have).sum() >= 30 else None})
    out.sort(key=lambda d: -d["spread"])
    return [{"base_rate": base, "n": len(y)}] + out


# ------------------------------------------------------------------------------- training
class Model:
    """A promoted model as used at run time."""

    def __init__(self, blob: dict):
        self.clf, self.iso = blob["clf"], blob["iso"]
        self.names, self.vocab = blob["names"], blob["vocab"]
        self.thr, self.ver, self.metrics = blob["thr"], blob["ver"], blob.get("metrics", {})

    def predict(self, f: dict) -> float:
        raw = self.clf.predict_proba(matrix([f], self.names, self.vocab))[:, 1]
        return float(self.iso.predict(raw)[0])

    @staticmethod
    def load(path: str | Path) -> "Model | None":
        try:
            with open(path, "rb") as fh:
                return Model(pickle.load(fh))
        except Exception as e:   # noqa: BLE001 - e.g. written by another scikit-learn version
            log.warning("could not load model %s (%s) - falling back to the scorecard", path, e)
            return None


def _fit(X: np.ndarray, y: np.ndarray, w: np.ndarray, n_cat: int):
    from sklearn.ensemble import HistGradientBoostingClassifier
    cat_mask = [False] * (X.shape[1] - n_cat) + [True] * n_cat
    clf = HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.05, max_leaf_nodes=24, min_samples_leaf=max(20, len(y) // 400),
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.15, n_iter_no_change=25,
        categorical_features=cat_mask if n_cat else None, random_state=7)
    clf.fit(X, y, sample_weight=w)
    return clf


def train(db_path: Path, family: str, cfg, call_cfg, models_dir: Path, champion_path: str | None) -> dict:
    """Runs in a separate process. Returns a report; report['promoted'] says whether to switch."""
    from .config import Section
    cfg, call_cfg = Section(cfg), Section(call_cfg)
    t0 = time.time()
    ds = Dataset(db_path, family, cfg.max_rows)
    y, prior, X, names, vocab = ds.y, ds.prior, ds.X, ds.names, ds.vocab
    n, pos = len(y), int(y.sum())
    report: dict = {"family": family, "n_rows": n, "n_pos": pos, "base_rate": pos / n if n else None,
                    "promoted": False, "ts": t0}
    if n >= 200:
        report["lift"] = lift_table(X, y, names, cfg.lift_bins)
    if n < cfg.min_rows or pos < cfg.min_pos:
        report["note"] = f"collecting: {n}/{cfg.min_rows} labelled snapshots, {pos}/{cfg.min_pos} winners"
        return report

    # time split by token so one token's snapshots never straddle a boundary
    tokens = list(dict.fromkeys(ds.tokens))
    order = {t: i for i, t in enumerate(tokens)}
    ia, ib = int(len(tokens) * 0.70), int(len(tokens) * 0.85)
    rank = np.array([order[t] for t in ds.tokens])
    part = np.where(rank < ia, 0, np.where(rank < ib, 1, 2))
    age_days = (t0 - ds.ts) / 86400.0
    w = np.power(0.5, age_days / HALF_LIFE_DAYS)
    n_cat = sum(1 for c in CATEGORICAL if vocab.get(c))
    A, B, C = part == 0, part == 1, part == 2
    if y[A].sum() < 20 or y[C].sum() < 8 or y[B].sum() < 5:
        report["note"] = "not enough winners in each time slice yet"
        return report

    from sklearn.isotonic import IsotonicRegression
    challenger = _fit(X[A], y[A], w[A], n_cat)
    test_raw = challenger.predict_proba(X[C])[:, 1]
    m_new, m_prior = evaluate(y[C], test_raw), evaluate(y[C], prior[C])
    report["test"] = {"n": int(C.sum()), "pos": int(y[C].sum()), "base_rate": float(y[C].mean()),
                      "model": m_new, "scorecard": m_prior}
    m_old = None
    champ = Model.load(champion_path) if champion_path else None
    if champ is not None:
        try:
            test_rows = [r for r, c in zip(ds.rows, C) if c]
            old_raw = champ.clf.predict_proba(ds.build(test_rows, champ.names, champ.vocab))[:, 1]
            m_old = evaluate(y[C], old_raw)
            report["test"]["current_model"] = m_old
        except Exception as e:   # noqa: BLE001
            log.warning("could not score the current model: %s", e)
    # a real edge, not noise: clearly better than the scorecard, and better than 99% of random rankings
    chance = chance_ap(y[C])
    report["test"]["chance_ap_99"] = chance
    beats_card = (m_new["ap"] or 0) >= 1.10 * (m_prior["ap"] or 0) and (m_new["ap"] or 0) > chance
    holds_vs_old = m_old is None or (m_new["ap"] or 0) >= (m_old["ap"] or 0) - 0.005 + cfg.promote_margin
    report["verdict"] = ("beats the scorecard" if beats_card else "does NOT beat the scorecard") + (
        "" if m_old is None else (", holds against the current model" if holds_vs_old else ", worse than the current model"))
    if not (beats_card and holds_vs_old):
        report["note"] = "challenger rejected - keeping what is in use"
        report["seconds"] = round(time.time() - t0, 1)
        return report

    # promoted: refit on the first 85%, calibrate and set the threshold on the newest 15%
    AB = A | B
    clf = _fit(X[AB], y[AB], w[AB], n_cat)
    raw_c = clf.predict_proba(X[C])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw_c, y[C])
    thr = pick_threshold(y[C], iso.predict(raw_c), call_cfg.target_precision, call_cfg.min_prob,
                         call_cfg.top_pct / 100.0)
    report["threshold"] = thr
    try:
        from sklearn.inspection import permutation_importance
        idx = np.where(C)[0][-20000:]
        pi = permutation_importance(challenger, X[idx], y[idx], scoring="average_precision", n_repeats=3,
                                    random_state=7)
        cols = names + [c for c in CATEGORICAL if vocab.get(c)]
        top = sorted(zip(cols, pi.importances_mean), key=lambda t: -t[1])[:25]
        report["importance"] = [{"feature": k, "gain": round(float(v), 5)} for k, v in top if v > 0]
    except Exception as e:   # noqa: BLE001
        log.warning("importance skipped: %s", e)
    ver = time.strftime(f"{family}-%Y%m%d-%H%M", time.gmtime(t0))
    models_dir.mkdir(parents=True, exist_ok=True)
    path = models_dir / f"{ver}.pkl"
    with open(path, "wb") as fh:
        pickle.dump({"clf": clf, "iso": iso, "names": names, "vocab": vocab, "thr": thr["thr"], "ver": ver,
                     "metrics": report["test"]}, fh)
    report.update({"promoted": True, "ver": ver, "path": str(path), "n_features": X.shape[1],
                   "seconds": round(time.time() - t0, 1)})
    return report
