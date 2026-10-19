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


def test_conviction_is_the_top_slice_of_the_last_day(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    now = time.time()
    assert e.conviction(now, "prob", 0.99) == 0                 # too few snapshots to rank against
    for i in range(1000):
        e._remember(now - 60, 50.0, i / 1000)                    # chances 0.000 .. 0.999
    e._remember(now - 90000, 50.0, 5.0)                          # older than 24 h: ignored
    assert e.conviction(now, "prob", 0.995) == 2                 # top 1%
    assert e.conviction(now, "prob", 0.97) == 1                  # top 5%
    assert e.conviction(now, "prob", 0.90) == 0
    for _ in range(100):
        e._remember(now - 30, 50.0, 1.0)                         # a block of identical "certain" chances
    assert e.conviction(now, "prob", 1.0) == 1                   # 100 of 1,100 tied: not all "top 1%"
    asyncio.run(e.http.close())


def test_call_message_names_the_conviction():
    from radar.notify import call_text
    tok = SimpleNamespace(symbol="ABC", name="Abc", launchpad="pump.fun", chain="solana", mint="M1", family="pump", meta=None, pool=None)
    c = cfg().label
    text = call_text(tok, {"age_s": 60}, 70.0, 0.8, "m1", [], [], None, c, tier=2,
                     tier_cfg={"very_high_top_pct": 1, "high_top_pct": 5})
    assert text.startswith("🔥🔥 <b>VERY HIGH CONVICTION</b> — top 1% of the last 24 h")
    assert "CONVICTION" not in call_text(tok, {"age_s": 60}, 70.0, 0.8, "m1", [], [], None, c)


async def test_only_high_and_very_high_conviction_calls_are_sent(tmp_path):
    from radar.state import Label
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;paper.telegram=false"))
    assert e.cfg.call.min_tier == 1
    sent = []

    async def fake_send(text, **k):
        sent.append(text)
        return [1]
    e.telegram.send = fake_send
    e.threshold = lambda now: ("prior", 0.0)
    f = {"px_mcap_usd": 20000, "sec_checked": 1, "px_on_curve": 1, "launchpad": "pump.fun", "chain": "solana",
         "rc_score": 1, "st_ok": 1, "cx_ok": 1, "gti_ok": 1, "age_s": 60}
    for i, tier in enumerate((0, 1, 2)):
        tok = SimpleNamespace(id=f"solana:T{i}pump", family="pump", address=f"T{i}pump", mint=f"T{i}pump", chain="solana",
                              meta={}, called=False, tainted=False, price=1.0, symbol=f"T{i}", name="t", launchpad="pump.fun",
                              pool=None)
        lb = Label(i + 1, time.time(), 1.0, e.cfg.label)
        e.conviction = lambda now, kind, value, t=tier: t
        await e._consider(tok, lb, i + 1, dict(f), time.time())
    assert len(sent) == 2 and "HIGH CONVICTION" in sent[0] and "VERY HIGH CONVICTION" in sent[1]
    assert [r["tier"] for r in e.db.query("SELECT tier FROM calls ORDER BY id")] == [1, 2]
    await e.http.close()
