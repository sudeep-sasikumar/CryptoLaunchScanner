"""Pull-proof calls only, and learning which creators pull their liquidity."""
from __future__ import annotations

import asyncio
import time

from radar.engine import Engine
from radar.features import creator_features
from radar.scoring import gate, pull_proof

from .helpers import cfg

C = cfg()
PP = C.call.pull_proof
CLEAN = {"px_mcap_usd": 20000, "sec_checked": 1, "px_liq_usd": 20000}


def test_only_coins_whose_liquidity_cannot_be_pulled():
    assert pull_proof({"px_on_curve": 1, "launchpad": "meteora-dbc"}, PP)          # still on a curve
    assert pull_proof({"px_on_curve": 0, "launchpad": "pump.fun"}, PP)             # PumpSwap LP burned
    assert pull_proof({"px_on_curve": 0, "launchpad": "pons"}, PP)                 # v4 position in the locker
    assert not pull_proof({"px_on_curve": 0, "launchpad": "uniswap-v4-robinhood"}, PP)
    assert not pull_proof({"px_on_curve": 0, "launchpad": "meteora-dbc"}, PP)      # graduated: lock not guaranteed
    assert pull_proof({"px_on_curve": 0, "launchpad": "meteora-damm-v2", "sec_lp_locked_pct": 100}, PP)
    raw = {**CLEAN, "px_on_curve": 0, "launchpad": "uniswap-v4-robinhood"}
    assert gate(raw, C, learned=True).startswith("liquidity can be pulled")       # applies with a model too
    assert gate(raw, cfg("call.pull_proof.only=false"), learned=True) is None
    assert gate({**CLEAN, "px_on_curve": 1, "launchpad": "pons"}, C, learned=True) is None


def test_env_switch(monkeypatch):
    from radar.config import load_config
    monkeypatch.setenv("PULL_PROOF_ONLY", "false")
    assert load_config().call.pull_proof["only"] is False


def _tok(e, i, launchpad, age_h, creator="CR1", liq=5000.0):
    e.db.execute("INSERT INTO tokens(id,family,chain,launchpad,address,creator,created_ts,liq_max_usd) "
                 "VALUES(?,?,?,?,?,?,?,?)", (f"robinhood:0xT{i}", "dex", "robinhood", launchpad, f"0xT{i}", creator,
                                            time.time() - age_h * 3600, liq))


def test_pulled_liquidity_marks_the_token_and_blocks_its_creator(tmp_path):
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    _tok(e, 1, "uniswap-v4-robinhood", 7)                    # due (6 h check), will be found empty
    _tok(e, 2, "uniswap-v4-robinhood", 7, creator="CR2")     # due, still has liquidity
    _tok(e, 3, "uniswap-v4-robinhood", 3)                    # too young
    _tok(e, 4, "pons", 30)                                   # locks its LP: never looked up
    _tok(e, 5, "uniswap-v4-robinhood", 30, liq=200)          # never held real liquidity
    asked = []

    async def fake(chain, addrs):
        asked.extend(addrs)
        return {"0xt1": 3.0, "0xt2": 25000.0}
    e.gecko.reserves = fake
    assert asyncio.run(e.check_pulls(time.time())) == 1
    assert sorted(asked) == ["0xT1", "0xT2"]
    t1 = e.db.one("SELECT pull_state, pull_checks FROM tokens WHERE id='robinhood:0xT1'")
    t2 = e.db.one("SELECT pull_state, pull_checks FROM tokens WHERE id='robinhood:0xT2'")
    assert (t1["pull_state"], t1["pull_checks"], t2["pull_state"], t2["pull_checks"]) == (1, 1, 0, 1)
    assert e.pull_due(time.time()) == []                     # T2's next look is at 24 h, T1 is settled
    assert sorted(r["id"] for r in e.pull_due(time.time() + 18 * 3600)) == ["robinhood:0xT2", "robinhood:0xT3"]
    cr = e.rep.creator("CR1")
    assert cr["n_pulled"] == 1
    tok = type("T", (), {"seen": {"cr_prev": 1, "cr_day": 0}})()
    f = {**CLEAN, "px_on_curve": 1, **creator_features(tok, {"creator": cr})}
    assert gate(f, C, learned=True) == "creator's earlier coins lost their liquidity (1x)"
    assert e.pull_stats() == [{"launchpad": "uniswap-v4-robinhood", "checked": 2, "gone": 1}]
    asyncio.run(e.http.close())


def test_earlier_coins_are_backfilled_and_exported(tmp_path):
    import gzip
    import json
    from radar.export import build_day
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false"))
    now = time.time()
    e.db.execute("INSERT INTO tokens(id,family,chain,launchpad,address,creator,created_ts) VALUES(?,?,?,?,?,?,?)",
                 ("robinhood:0xOLD", "dex", "robinhood", "uniswap-v4-robinhood", "0xOLD", "CR9", now - 8 * 3600))
    for liq in (3000.0, 9000.0):
        fk, packed = e.db.pack({"px_liq_usd": liq})
        e.db.execute("INSERT INTO snapshots(token_id,family,cp,ts,price,fkeys,features,done,tainted) "
                     "VALUES(?,?,?,?,?,?,?,1,0)", ("robinhood:0xOLD", "dex", 60, now - 8 * 3600, 1.0, fk, packed))
    e.db.commit()
    asyncio.run(e.http.close())
    e2 = Engine(e.cfg)                                       # restart: backfill runs
    assert e2.db.one("SELECT liq_max_usd FROM tokens WHERE id='robinhood:0xOLD'")["liq_max_usd"] == 9000.0

    async def fake(chain, addrs):
        return {"0xold": 0.0}
    e2.gecko.reserves = fake
    assert asyncio.run(e2.check_pulls(now)) == 1
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 8 * 3600))
    rows = [json.loads(x) for x in gzip.decompress(build_day(e2.db.path, day)).splitlines()]
    pull = [r for r in rows if r["kind"] == "pull"]
    assert pull and pull[0]["pull_state"] == 1 and pull[0]["creator"] == "CR9"
    asyncio.run(e2.http.close())
