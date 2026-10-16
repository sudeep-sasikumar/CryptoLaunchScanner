"""Paper trading: the take-profit plan on live prices, with fees and slippage, no money involved."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from radar.db import DB
from radar.paper import Paper
from radar.state import Label

from .helpers import cfg

C = cfg()
K = 0.97 * 0.99                                       # every sell: 3% slippage, 1% fee


def _setup(tmp_path, price_now=1.0, overrides=""):
    c = cfg(overrides) if overrides else C
    db = DB(tmp_path / "radar.db")
    paper = Paper(db, c.paper)
    lb = Label(1, 0.0, 1.0 / 1.03, c.label)          # label entry 1.0 (snapshot price + 3%)
    tok = SimpleNamespace(id="solana:Xpump", symbol="X", launchpad="pump.fun")
    paper.open(7, tok, lb, 2, price_now / 1.03, time.time())   # buys at price_now (with its own 3%)
    return db, paper, lb


def _feed(paper, lb, prices):
    t = 1.0
    for p in prices:
        lb.update(t, p)
        paper.step(time.time())
        t += 15


def test_full_ladder(tmp_path):
    db, paper, lb = _setup(tmp_path)
    _feed(paper, lb, [1.2, 2.1, 3.0, 4.2, 6.0, 8.5])
    r = db.one("SELECT * FROM paper WHERE call_id=7")
    assert r["state"] == "closed" and r["reason"] == "sold out"
    assert r["pnl_usd"] == pytest.approx(25 * K * (0.5 * 2 + 0.25 * 4 + 0.25 * 8) - 25 * 0.01 - 25)
    assert not paper.live


def test_stop_before_2x_fills_where_the_price_really_is(tmp_path):
    db, paper, lb = _setup(tmp_path)
    _feed(paper, lb, [1.3, 0.8, 0.31])                # gapped from 0.8 straight to 0.31
    r = db.one("SELECT * FROM paper WHERE call_id=7")
    assert r["reason"] == "stop -50%" and r["low_pre"] == pytest.approx(0.31)
    assert r["pnl_usd"] == pytest.approx(25 * 0.31 * K - 25 * 0.01 - 25)


def test_rest_sold_at_last_price_when_following_ends(tmp_path):
    db, paper, lb = _setup(tmp_path)
    _feed(paper, lb, [2.2, 3.1])
    lb.finish("horizon")
    paper.step(time.time())
    r = db.one("SELECT * FROM paper WHERE call_id=7")
    assert r["reason"] == "end of tracking"
    assert r["pnl_usd"] == pytest.approx(25 * K * (0.5 * 2 + 0.5 * 3.1) - 25 * 0.01 - 25)


def test_entry_is_the_price_when_the_call_goes_out(tmp_path):
    db, paper, lb = _setup(tmp_path, price_now=1.25)  # moved +25% between snapshot and call
    _feed(paper, lb, [2.1, 2.4])                       # 2.4 on the label = only 1.92x for this trade
    assert paper.live[7]["left"] == 1.0
    _feed(paper, lb, [2.6])                            # 2.6 / 1.25 = 2.08x: first sale
    assert paper.live[7]["left"] == pytest.approx(0.5)


def test_breakeven_stop_after_the_first_sale(tmp_path):
    db, paper, lb = _setup(tmp_path, overrides="paper.stop_after_first=1.0")
    _feed(paper, lb, [2.2, 1.5, 0.95])
    r = db.one("SELECT * FROM paper WHERE call_id=7")
    assert r["reason"] == "stop +0%"
    assert r["pnl_usd"] == pytest.approx(25 * K * (0.5 * 2 + 0.5 * 0.95) - 25 * 0.01 - 25)


def test_open_trades_are_closed_at_their_last_price_after_a_restart(tmp_path):
    db, paper, lb = _setup(tmp_path)
    _feed(paper, lb, [1.5])
    paper.touch(time.time())
    db.commit()
    again = Paper(db, C.paper)
    r = db.one("SELECT * FROM paper WHERE call_id=7")
    assert r["state"] == "closed" and r["reason"] == "restart" and not again.live
    assert r["pnl_usd"] == pytest.approx(25 * 1.5 * K - 25 * 0.01 - 25)
    assert "1 closed" in again.summary_line(0)


def test_only_the_chosen_tiers():
    p = Paper.__new__(Paper)
    p.cfg = C.paper
    assert p.wants(2, {}) and not p.wants(1, {}) and not p.wants(0, {})


def test_engine_follows_paper_trades_every_tick(tmp_path):
    import asyncio
    from radar.engine import Engine
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;paper.telegram=false"))
    lb = Label(1, 0.0, 1.0 / 1.03, e.cfg.label)
    e.paper.open(9, SimpleNamespace(id="solana:Ypump", symbol="Y", launchpad="pump.fun"), lb, 2, 1.0 / 1.03, time.time())
    lb.update(1.0, 0.4)
    e.last_tick = time.time()
    e._tick(time.time())
    assert e.db.one("SELECT reason FROM paper WHERE call_id=9")["reason"] == "stop -50%"
    assert "Paper trading: 1 closed" in e.summary_text()
    asyncio.run(e.http.close())
