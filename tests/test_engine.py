"""The whole loop without the network: events in -> snapshot -> call -> label -> wallet credit."""
from __future__ import annotations

import asyncio
import json
import time

from radar.engine import Engine
from radar.sources.dexscreener import SYSTEM

from .helpers import Curve, addr, cfg


def _engine(tmp_path, extra: str = "") -> Engine:
    c = cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;sources.metadata.enabled=false;"
            f"sources.rugcheck.enabled=false;sources.llm.enabled=false;call.min_prior=40;{extra}")
    e = Engine(c)
    e.quotes.usd[SYSTEM] = (100.0, time.time() + 1e6)           # SOL = $100, never stale
    return e


def _launch(e: Engine, curve: Curve, buyers: int = 40) -> None:
    e.on_pump_event("create", curve.create_event(name="Fresh Idea", symbol="FRESH"), curve.slot, "sig")
    e.on_pump_event("trade", *curve.buy(100, 0.5), "sig")
    for i in range(buyers):
        ev, slot = curve.buy(200 + i, 0.3 + 0.01 * i, dt=3 + i, dslot=10 + i * 2)
        e.on_pump_event("trade", ev, slot, "sig")


async def test_clean_launch_is_snapshotted_called_labelled_and_credited(tmp_path, capsys):
    e = _engine(tmp_path)
    now = time.time()
    curve = Curve(ts=int(now - 61))
    _launch(e, curve)
    tok = e.pump[addr(1)]
    assert e.counts["creates"] == 1 and tok.n_buys == 41

    e._tick(now)                                                 # 60 s checkpoint is due
    assert e.counts["snapshots"] == 1 and len(tok.labels) == 1
    await asyncio.sleep(0.05)                                    # the pre-call checks run as a task
    e.db.commit()
    call = e.db.one("SELECT * FROM calls")
    assert call is not None and call["status"] == "open" and tok.called
    assert "FRESH" in capsys.readouterr().out                    # no Telegram configured: printed instead
    snap = e.db.one("SELECT * FROM snapshots")
    f = e.db.unpack(snap["fkeys"], snap["features"])
    assert snap["called"] == 1 and snap["gate"] is None and f["dist_holders"] == 40 and f["id_name_prev_n"] == 0

    for i in range(40):                                          # it runs ...
        ev, slot = curve.buy(600 + i, 1.5, dt=80 + i * 5, dslot=300 + i)
        e.on_pump_event("trade", ev, slot, "sig")
    e.last_tick = now + 299
    e._tick(now + 300)
    await asyncio.sleep(0.05)
    assert tok.labels[0].hit and e.db.one("SELECT status FROM calls")["status"] == "hit"

    for j, (w, r) in enumerate(list(tok.wallets.items())):       # ... then everyone leaves
        if r[0] > 0:
            ev, slot = curve.sell(0, r[0], dt=400 + j, dslot=900 + j)
            ev["user"] = w
            e.on_pump_event("trade", ev, slot, "sig")
    e.last_tick = now + 2499
    e._tick(now + 2500)                                          # past the last checkpoint: wrap up
    e.db.commit()
    snap = e.db.one("SELECT * FROM snapshots")
    assert snap["done"] == 1 and snap["hit"] == 1 and snap["tainted"] == 0 and snap["peak_mult"] > 2
    assert addr(1) not in e.pump and e.counts["finalized"] == 1
    final = json.loads(e.db.one("SELECT final FROM tokens")["final"])
    assert final["peak_mult"] > 3 and final["path"]
    assert e.db.one("SELECT n_win FROM wallets WHERE addr=?", (addr(200),))["n_win"] == 1
    assert e.db.one("SELECT n_created, n_snap FROM creators")["n_snap"] == 1
    assert "1 calls" in e.summary_text() and "1 hit 2x" in e.summary_text()

    # the dashboard reads all of this back without errors
    import httpx
    from radar.web.server import build_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://localhost") as web:
        for path in ("health", "calls", "candidates", "scoreboard", "learning", "failures"):
            r = await web.get(f"/api/{path}")
            assert r.status_code == 200, (path, r.text)
        calls = (await web.get("/api/calls")).json()
        assert calls[0]["symbol"] == "FRESH" and calls[0]["status"] == "hit"
        board = (await web.get("/api/scoreboard")).json()
        assert board["total"]["hit"] == 1 and board["by_launchpad"][0]["launchpad"] == "pump.fun"
        t = (await web.get("/api/token", params={"id": "solana:" + addr(1)})).json()
        assert t["token"]["final"]["kind"] and len(t["snapshots"]) == 1
        assert (await web.get("/")).status_code == 200
    await e.http.close()


async def test_dashboard_beyond_localhost_requires_the_password(tmp_path, monkeypatch):
    import httpx
    from radar.web.server import build_app
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse battery")
    e = _engine(tmp_path, "dashboard.bind_host=0.0.0.0")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://radar.test") as web:
        assert (await web.get("/api/calls")).status_code == 401
        assert (await web.post("/api/login", json={"password": "guess"})).status_code == 401
        assert (await web.get("/api/calls")).status_code == 401
        assert (await web.post("/api/login", json={"password": "correct horse battery"})).status_code == 200
        assert (await web.get("/api/calls")).status_code == 200
        for _ in range(8):
            await web.post("/api/login", json={"password": "guess"})
        assert (await web.post("/api/login", json={"password": "correct horse battery"})).status_code == 429
    await e.http.close()


async def test_a_2x_is_stored_the_moment_it_happens_and_survives_a_later_gap(tmp_path):
    e = _engine(tmp_path, "call.min_prior=95")                  # no call: this is about the label only
    now = time.time()
    curve = Curve(ts=int(now - 61))
    _launch(e, curve)
    tok = e.pump[addr(1)]
    e._tick(now)
    for i in range(40):
        ev, slot = curve.buy(600 + i, 1.5, dt=80 + i * 5, dslot=300 + i)
        e.on_pump_event("trade", ev, slot, "sig")
    e.last_tick = now + 299
    e._tick(now + 300)
    e.db.commit()
    snap = e.db.one("SELECT * FROM snapshots")
    assert snap["hit"] == 1 and snap["done"] == 0                # final already, though still being followed
    from radar import model as learner
    assert learner.Dataset(e.db.path, "pump", 100).y.tolist() == [1]  # so training sees it right away
    e.on_gap(60)                                                 # the feed drops afterwards
    assert tok.tainted and tok.labels[0].done and not tok.labels[0].tainted
    e._tick(now + 301)
    e.db.commit()
    snap = e.db.one("SELECT * FROM snapshots")
    assert snap["hit"] == 1 and snap["tainted"] == 0 and snap["end_reason"] == "gap"
    report = await e.retrain("pump")                             # runs in a separate process
    assert "collecting" in report["note"] and report["n_pos"] == 1
    await e.http.close()


async def test_maintenance_keeps_what_learning_needs_and_holds_the_budget(tmp_path):
    e = _engine(tmp_path, "storage.max_gb=0.000001")             # an absurdly small budget forces the cap
    now = time.time()
    day = 86400
    rows = [("old", "pump", 60, now - 200 * day, 0, 0), ("called_old", "pump", 60, now - 200 * day, 1, 0),
            ("tainted", "pump", 60, now - 3 * day, 0, 1), ("fresh", "pump", 60, now - day, 0, 0)]
    for tid, fam, cp, ts, called, tainted in rows:
        e.db.execute("INSERT INTO snapshots(token_id,family,cp,ts,features,called,tainted,done) VALUES(?,?,?,?,'{}',?,?,1)",
                     (tid, fam, cp, ts, called, tainted))
    e.db.execute("INSERT INTO calls(snapshot_id,token_id,ts) VALUES(2,'called_old',?)", (now - 200 * day,))
    e.db.executemany("INSERT INTO wallets(addr,n_tok,last_ts) VALUES(?,?,?)",
                     [("once", 1, now - 50 * day), ("regular", 9, now - 50 * day), ("gone", 9, now - 130 * day)])
    e.db.commit()
    out = await e.maintain(now)
    left = {r["token_id"] for r in e.db.query("SELECT token_id FROM snapshots")}
    assert "old" not in left and "tainted" not in left and "called_old" in left   # calls keep their snapshot
    assert {r["addr"] for r in e.db.query("SELECT addr FROM wallets")} == {"regular"}
    assert out["over_budget_snapshots"] >= 1 and "fresh" not in left           # over budget: oldest history went
    await e.http.close()


async def test_a_frozen_process_sets_live_tokens_aside(tmp_path):
    e = _engine(tmp_path)
    now = time.time()
    curve = Curve(ts=int(now - 61))
    _launch(e, curve)
    e._tick(now)
    await asyncio.sleep(0.05)
    e._tick(now + 600)                                           # ten minutes vanished (host asleep / paused)
    e.db.commit()
    snap = e.db.one("SELECT * FROM snapshots")
    assert snap["done"] == 1 and snap["tainted"] == 1 and snap["end_reason"] == "gap"
    assert not e.pump                                            # dropped, and never used for training or reputation
    assert e.db.one("SELECT COUNT(*) n FROM wallets")["n"] == 0
    await e.http.close()


async def test_quiet_launch_gets_no_snapshot_and_is_dropped(tmp_path):
    e = _engine(tmp_path)
    now = time.time()
    curve = Curve(ts=int(now - 61))
    _launch(e, curve, buyers=3)                                  # below the activity gate
    e._tick(now)
    assert e.counts["snapshots"] == 0 and e.counts["gated_out"] == 1
    e.last_tick = now + 199
    e._tick(now + 200)
    assert not e.pump and e.counts["doa"] == 1
    await e.http.close()
