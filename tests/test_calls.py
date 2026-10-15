"""What a call shows the user: the bar for calling, plain-language status, runner updates."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from radar.config import load_config
from radar.engine import Engine
from radar.sources.dexscreener import SYSTEM
from radar.state import Label
from radar.web.server import call_state

from .helpers import cfg


def test_call_bar_is_a_chance_of_2x_settable_in_percent(monkeypatch):
    assert load_config().call.min_prob == 0.5
    monkeypatch.setenv("CALL_MIN_CHANCE", "60")
    c = load_config()
    assert c.call.min_prob == 0.6 and c.call.target_precision == 0.6


def test_model_threshold_never_below_the_chosen_chance(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    e.models["all"] = SimpleNamespace(thr=0.27, ver="m1")
    assert e.threshold(time.time()) == ("prob", 0.5)
    e.models["all"] = SimpleNamespace(thr=0.62, ver="m2")
    assert e.threshold(time.time()) == ("prob", 0.62)
    asyncio.run(e.http.close())


def test_status_words():
    assert call_state("open", following=True) == "live"
    assert call_state("hit", following=True) == "running"
    assert call_state("hit", following=False) == "ran"
    assert call_state("stopped", following=False) == "dumped"
    assert call_state("expired", following=False) == "fizzled"
    assert call_state("open", following=False) == "fizzled"


async def test_runner_gets_one_update_per_new_milestone(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    e.quotes.usd[SYSTEM] = (100.0, time.time() + 1e6)
    sent = []

    async def fake(tok, lb, ev):
        sent.append(ev)
    e._outcome_msg = fake
    lb = Label(1, 0.0, 1.0, e.cfg.label)
    lb.call_id, lb.hit = 7, True
    tok = SimpleNamespace(labels=[lb], called=True)
    for peak in (2.4, 3.2, 3.9, 5.5, 12.0, 12.5):
        lb.peak = peak
        e._runner_updates(tok)
        await asyncio.sleep(0)
    assert sent == ["x3", "x5", "x10"]                  # 12x jumped past 5 and 10: one message, the highest
    await e.http.close()
