"""Telegram settings from the environment, several chats and topics, commands, and the engine's
GMGN path (with GMGN simulated: no network)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from radar.config import load_config
from radar.engine import Engine
from radar.notify import Telegram, message_ids
from radar.sources.dexscreener import SYSTEM
from radar.sources.gmgn import parse_token

from .helpers import Curve, addr, cfg
from .test_engine import _launch

FIX = json.loads((Path(__file__).parent / "fixtures" / "gmgn_tokens.json").read_text(encoding="utf-8"))


def test_telegram_settings_come_from_plain_environment_variables(monkeypatch):
    monkeypatch.setenv("TELEGRAM_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_DAILY_SUMMARY_UTC", "06:30")
    monkeypatch.setenv("TELEGRAM_OUTCOME_UPDATES", "false")
    monkeypatch.setenv("TELEGRAM_COMMANDS", "true")
    monkeypatch.setenv("CALLS_PER_HOUR", "4")
    c = load_config()
    assert c.telegram.thread_id == 42 and c.telegram.daily_summary_utc == "06:30"   # not YAML's 390
    assert c.telegram.outcome_updates is False and c.telegram.commands is True and c.call.max_per_hour == 4


class FakeDB:
    def __init__(self):
        self.rows = {}

    def one(self, sql, args=()):
        return self.rows.get(args[0]) if "FROM alerts" in sql else None

    def execute(self, sql, args=()):
        self.rows[args[0]] = args


def test_alerts_go_to_every_chat_in_the_topic_and_replies_land_under_each():
    tg = Telegram(cfg("telegram.thread_id=7").telegram, "token", "111, -100222", FakeDB())
    sent = []

    async def fake_post(chat, text, reply_to, silent=False):
        sent.append((chat, reply_to, silent))
        return 1000 + len(sent)
    tg._post = fake_post
    ids = asyncio.run(tg.send("call", dedupe="call:x", kind="call"))
    assert ids == {"111": 1001, "-100222": 1002} and tg.chats == ["111", "-100222"]
    stored = message_ids(json.dumps(ids))
    asyncio.run(tg.send("hit", dedupe="outcome:1:hit", kind="outcome", reply_to=stored, silent=True))
    assert sent[2:] == [("111", 1001, True), ("-100222", 1002, True)]
    assert asyncio.run(tg.send("call", dedupe="call:x", kind="call")) is None         # never twice


class FakeGmgn:
    """Answers like GMGN would, from the recorded fixture."""
    enabled = True
    calls = errors = limited = 0

    def __init__(self, fixture):
        self.fx = fixture
        self.feed = []

    def backlog_s(self):
        return 0.0

    async def token(self, chain, address, with_security=True):
        return parse_token(self.fx["info"], self.fx["security"] if with_security else None)

    async def trenches(self, chain, platforms, limit):
        return self.feed

    async def close(self):
        pass


def _engine(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;sources.metadata.enabled=false;"
                   "sources.rugcheck.enabled=false;sources.goplus.enabled=false;sources.gecko_info.enabled=false;sources.llm.enabled=false;"
                   "call.min_prior=40"))
    e.quotes.usd[SYSTEM] = (100.0, time.time() + 1e6)
    return e


async def test_pump_snapshot_gets_gmgn_numbers_and_safety_before_the_call_decision(tmp_path):
    e = _engine(tmp_path)
    e.gmgn = FakeGmgn(FIX["pump_sol"])
    now = time.time()
    _launch(e, Curve(ts=int(now - 61)))
    e._tick(now)
    await asyncio.sleep(0.1)                                    # GMGN answers, then the decision is made
    e.db.commit()
    snap = e.db.one("SELECT * FROM snapshots")
    f = e.db.unpack(snap["fkeys"], snap["features"])
    assert f["gm_ok"] == 1 and f["sec_checked"] == 1 and "gm_phishing_pct" in f and "gm_insider_pct" in f
    assert f["dist_holders"] == 40                              # our own trade-built count kept priority
    assert len(e.priors["all"]) == 1
    await e.http.close()


async def test_gmgn_feed_discovers_other_launchpads_and_tags_pump_coins_with_socials(tmp_path):
    e = _engine(tmp_path)
    e.gmgn = FakeGmgn(FIX["factor_robinhood"])
    now = time.time()
    _launch(e, Curve(ts=int(now - 30)))
    e.gmgn.feed = [{"address": addr(1), "x_user_follower": 1500, "tg_call_count": 3}]
    task = asyncio.create_task(e.gmgn_feed_loop("sol_pump"))
    await asyncio.sleep(0.05)
    task.cancel()
    assert e.pump[addr(1)].gm_feed["gm_x_followers"] == 1500 and not e.dex      # socials only, no duplicate
    e.gmgn.feed = [{"address": "0xabc123", "launchpad_platform": "pons_v2", "symbol": "NEW", "name": "New",
                    "created_timestamp": now - 20, "progress": 0.3, "x_user_follower": 828},
                   {"address": "0xold", "launchpad_platform": "pons_v2", "created_timestamp": now - 5000}]
    task = asyncio.create_task(e.gmgn_feed_loop("robinhood"))
    await asyncio.sleep(0.05)
    task.cancel()
    tok = e.dex["robinhood:0xabc123"]
    assert tok.launchpad == "pons" and tok.on_curve is True and tok.gm_feed["gm_x_followers"] == 828
    assert "robinhood:0xold" not in e.dex                       # too old to be a new launch
    assert "Last calls" in await e.command("/calls") or "No calls" in await e.command("/calls")
    assert "pump.fun stream" in await e.command("/status")
    await e.http.close()
