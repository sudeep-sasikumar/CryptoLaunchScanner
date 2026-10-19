"""Automatic retraining is off: the model in use only changes when a retrain is asked for."""
from __future__ import annotations

import asyncio
import concurrent.futures
import time

from fastapi.testclient import TestClient

import radar.engine as engine_mod
from radar.engine import Engine

from .helpers import cfg


class Inline(concurrent.futures.ThreadPoolExecutor):
    def __init__(self, max_workers=1, mp_context=None):
        super().__init__(max_workers=1)


def _fake_train(*args):
    return {"family": "all", "ts": time.time(), "promoted": True, "ver": "cand1", "n_rows": 5000, "n_pos": 400,
            "path": "nowhere.pkl", "test": {"n": 900, "base_rate": 0.1}, "threshold": {"thr": 0.55}}


def test_no_automatic_retraining_by_default(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    assert e.cfg.learn.auto_retrain is False
    asyncio.run(asyncio.wait_for(e.train_loop(), 2))          # returns at once instead of looping
    asyncio.run(e.http.close())


def test_a_check_only_retrain_reports_but_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "ProcessPoolExecutor", Inline)
    monkeypatch.setattr(engine_mod.learner, "train", _fake_train)
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    sent = []

    async def fake_send(*a, **k):
        sent.append(a)
    e.telegram.send = fake_send
    rep = asyncio.run(e.retrain("all", promote=False))
    assert rep["would_promote"] is True and rep["promoted"] is False
    assert e.models["all"] is None and not e.db.query("SELECT * FROM models") and not sent
    asyncio.run(e.http.close())


def test_retrain_api_needs_the_login_and_runs_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    from radar.web.server import build_app
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;dashboard.bind_host=0.0.0.0"))
    started = []

    async def fake_retrain(fam, promote=True):
        started.append(promote)
        await asyncio.sleep(0)
        e.last_train["all"] = {"note": "ok", "would_promote": False, "promoted": False}
        return e.last_train["all"]
    e.retrain = fake_retrain
    with TestClient(build_app(e)) as c:
        assert c.post("/api/retrain", json={}).status_code == 401
        c.post("/api/login", json={"password": "pw"})
        assert c.post("/api/retrain", json={"promote": False}).json() == {"started": True, "promote": False}
        st = c.get("/api/retrain").json()
        assert st["auto_retrain"] is False and st["in_use"] is None
    assert started == [False]
    asyncio.run(e.http.close())
