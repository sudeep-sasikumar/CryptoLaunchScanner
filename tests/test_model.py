"""The learner: promotes a model only when it really beats the scorecard on unseen, newer data."""
from __future__ import annotations

import json
import random
import time

import numpy as np

from radar import model as learner
from radar.db import DB

from .helpers import cfg

C = cfg("learn.min_rows=600;learn.min_pos=30")


def _fill(db: DB, n: int, signal: bool, seed: int = 1) -> None:
    """n snapshots, oldest first. With `signal`, winners have low bundle % and many holders."""
    rng = random.Random(seed)
    now = time.time()
    rows = []
    for i in range(n):
        bundle, holders, noise = rng.uniform(0, 30), rng.randint(5, 200), rng.random()
        p = 0.02 + (0.5 if (bundle < 6 and holders > 90) else 0.0) if signal else 0.12
        hit = int(rng.random() < p)
        f = {"launch_bundle_pct": bundle, "dist_holders": holders, "noise": noise, "chain": "solana",
             "launchpad": rng.choice(["pump.fun", "pons"]), "sometimes": None if i % 3 else 1.0, "_private": "x"}
        ts = now - (n - i) * 60
        rows.append((f"tok{i}", "pump", 60, ts, ts - 60, 1.0, json.dumps(f), 50 + rng.uniform(-10, 10), 1, 0, hit,
                     2.5 if hit else 1.1))
    db.executemany("INSERT INTO snapshots(token_id,family,cp,ts,token_created_ts,price,features,prior,done,tainted,hit,"
                   "peak_mult) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()


def test_model_is_promoted_when_it_beats_the_scorecard(tmp_path):
    db = DB(tmp_path / "t.db")
    _fill(db, 3000, signal=True)
    r = learner.train(db.path, "pump", C.learn, C.call, tmp_path / "models", None)
    assert r["promoted"], r.get("note")
    t = r["test"]
    assert t["model"]["ap"] > 2 * t["scorecard"]["ap"]
    assert t["model"]["p_top5"] > 3 * t["base_rate"]
    assert r["threshold"]["expected_precision"] >= C.call.target_precision
    m = learner.Model.load(r["path"])
    good = m.predict({"launch_bundle_pct": 2, "dist_holders": 150, "noise": 0.5, "launchpad": "pump.fun"})
    bad = m.predict({"launch_bundle_pct": 25, "dist_holders": 20, "noise": 0.5, "launchpad": "pump.fun"})
    assert good > m.thr > bad and good > 0.3 and bad < 0.1
    assert m.predict({}) < m.thr                                   # all factors missing: no call
    assert "_private" not in m.names and "chain" not in m.names
    lift = r["lift"]
    assert lift[0]["n"] == 3000 and {lift[1]["feature"], lift[2]["feature"]} == {"launch_bundle_pct", "dist_holders"}


def test_model_is_rejected_when_there_is_nothing_to_learn(tmp_path):
    db = DB(tmp_path / "t.db")
    _fill(db, 2500, signal=False)
    r = learner.train(db.path, "pump", C.learn, C.call, tmp_path / "models", None)
    assert not r["promoted"] and "rejected" in r["note"]
    assert r["test"]["model"]["ap"] <= r["test"]["chance_ap_99"]     # no better than a lucky shuffle


def test_not_enough_history_means_no_model_but_a_lift_table(tmp_path):
    db = DB(tmp_path / "t.db")
    _fill(db, 300, signal=True)
    r = learner.train(db.path, "pump", C.learn, C.call, tmp_path / "models", None)
    assert not r["promoted"] and "collecting" in r["note"] and r["lift"][0]["n"] == 300


def test_tainted_and_unfinished_snapshots_are_never_trained_on(tmp_path):
    db = DB(tmp_path / "t.db")
    _fill(db, 50, signal=True)
    db.execute("UPDATE snapshots SET tainted=1 WHERE id<=10")
    db.execute("UPDATE snapshots SET done=0, hit=NULL WHERE id>40")     # still being followed, no outcome yet
    db.execute("UPDATE snapshots SET hit=1 WHERE id=50")                # still followed, but already 2x: final
    db.commit()
    ds = learner.Dataset(db.path, "pump", 1000)
    assert ds.X.shape[0] == 31 and ds.y[-1] == 1


def test_compact_rows_round_trip_and_train_like_plain_ones(tmp_path):
    db = DB(tmp_path / "t.db")
    f = {"a": 1.23456789, "b": None, "chain": "solana", "c": 3, "flag": True, "inf": float("inf")}
    kid, text = db.pack(f)
    assert db.unpack(kid, text) == {"a": 1.235, "b": None, "chain": "solana", "c": 3, "flag": 1, "inf": None}
    assert db.pack({"c": 0, "a": 2.0, "b": 1, "chain": "x", "flag": 0, "inf": 0})[0] == kid   # same layout, reused
    assert len(text) < len(json.dumps(f))


def test_threshold_is_the_lowest_cut_that_keeps_the_target_hit_rate():
    y = np.array([1] * 30 + [0] * 30 + [1] * 5 + [0] * 935)
    prob = np.concatenate([np.full(30, 0.9), np.full(30, 0.6), np.full(5, 0.3), np.full(935, 0.05)])
    t = learner.pick_threshold(y, prob, target=0.5, floor=0.25, fallback_share=0.02)
    assert t["how"] == "target" and t["thr"] == 0.3 and t["n_calls"] == 65       # never a cut inside the tied 0.05 block
    assert round(t["expected_precision"], 3) == 0.538
    t = learner.pick_threshold(y, prob, target=0.99, floor=0.25, fallback_share=0.02)
    assert t["thr"] == 0.9 and t["expected_precision"] == 1.0
    t = learner.pick_threshold(y, np.full(1000, 0.05), target=0.5, floor=0.25, fallback_share=0.02)
    assert t["how"] == "top-share" and t["thr"] == 0.25 and t["n_calls"] == 0     # nothing confident: no calls
