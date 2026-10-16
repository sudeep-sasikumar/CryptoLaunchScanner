"""Research data: the VPS serves one file per day, the PC copies it safely and studies it."""
from __future__ import annotations

import asyncio
import gzip
import json
import random

import httpx
import numpy as np
from fastapi.testclient import TestClient

from radar.engine import Engine
from radar.export import build_day, list_days
from radar.web.server import build_app
from tools import analyze, sync_research

from .helpers import cfg

DAY0 = 1791936000.0                       # 2026-10-14 00:00 UTC


def _engine(tmp_path, monkeypatch, n=30):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-for-tests")
    e = Engine(cfg(f"app.data_dir={tmp_path.as_posix()};dashboard.enabled=false;dashboard.bind_host=0.0.0.0"))
    for i in range(n):
        ts = DAY0 + i * 3600 * 1.5                    # spreads over two UTC days
        fk, packed = e.db.pack({"dist_holders": i, "launchpad": "pump.fun", "px_mcap_usd": 5000.0 + i})
        e.db.execute("INSERT INTO tokens(id,family,chain,launchpad,symbol,name,created_ts,updated_ts,status) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (f"t{i}", "pump", "solana", "pump.fun", f"S{i}", f"N{i}", ts, ts, "dead"))
        e.db.execute("INSERT INTO snapshots(token_id,family,cp,ts,token_created_ts,price,mcap_usd,fkeys,features,prior,"
                     "done,hit,tainted,peak_mult) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"t{i}", "pump", 60, ts, ts - 60, 1e-6, 5000.0, fk, packed, 50.0, int(i != 3), int(i % 5 == 0),
                      int(i == 7), 1.0 + i / 10))
    e.db.commit()
    return e


def test_day_files_hold_finished_untainted_snapshots_with_named_factors(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch)
    days = list_days(e.db.path)
    assert [d["name"] for d in days] == ["2026-10-14.jsonl.gz", "2026-10-15.jsonl.gz"]
    rows = [json.loads(x) for x in gzip.decompress(build_day(e.db.path, "2026-10-14")).splitlines()]
    snaps = [r for r in rows if r["kind"] == "snapshot"]
    ids = {r["token_id"] for r in snaps}
    assert "t7" not in ids and "t3" not in ids            # tainted, and not finished yet (no hit)
    assert "t0" in ids and len(snaps) == days[0]["snapshots"]
    s = next(r for r in snaps if r["token_id"] == "t5")
    assert s["f"]["dist_holders"] == 5 and s["launchpad"] == "pump.fun" and s["peak_mult"] == 1.5
    asyncio.run(e.http.close())


def test_research_files_need_the_login(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch)
    with TestClient(build_app(e)) as c:
        assert c.get("/api/research").status_code == 401
        assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
        assert c.post("/api/login", json={"password": "pw-for-tests"}).status_code == 200
        days = c.get("/api/research").json()["days"]
        r = c.get(f"/api/research/file/{days[0]['name']}")
        assert r.status_code == 200 and gzip.decompress(r.content).startswith(b'{"kind":"meta"')
        assert c.get("/api/research/file/..%2Fradar.db").status_code == 404
        assert c.get("/api/research/file/notes.txt").status_code == 404
    asyncio.run(e.http.close())


def test_sync_keeps_the_old_copy_when_a_download_is_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_research.time, "sleep", lambda s: None)
    good = gzip.compress(b'{"kind":"meta"}\n')
    dest = tmp_path / "2026-10-14.jsonl.gz"
    dest.write_bytes(good)
    broken = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=good[:-6])))
    assert sync_research.download(broken, dest.name, dest) is None
    assert dest.read_bytes() == good and not (tmp_path / (dest.name + ".part")).exists()
    newer = gzip.compress(b'{"kind":"meta","v":2}\n')
    ok = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(lambda req: httpx.Response(200, content=newer)))
    assert sync_research.download(ok, dest.name, dest) == len(newer) and dest.read_bytes() == newer


def test_analysis_finds_a_planted_trait(tmp_path):
    rnd = random.Random(1)
    lines = [{"kind": "meta", "day": "2026-10-14"}]
    for i in range(800):
        holders = rnd.uniform(0, 100)
        bundled = rnd.uniform(0, 40)
        good = holders > 70 and bundled < 10
        peak = rnd.choice([4.5, 12.0, 150.0]) if good and rnd.random() < 0.7 else rnd.uniform(0.3, 1.9)
        lines.append({"kind": "snapshot", "token_id": f"t{i}", "cp": 60, "ts": DAY0 + i * 60, "peak_mult": peak,
                      "prior": holders - bundled + rnd.uniform(-5, 5), "prob": None, "called": 0, "symbol": f"S{i}",
                      "launchpad": "pump.fun", "mcap_usd": 6000,
                      "f": {"dist_holders": holders, "launch_slot0_pct": bundled, "noise": rnd.random(),
                            "launchpad": rnd.choice(["pump.fun", "bonk"]), "chain": "solana", "source": "stream"}})
    (tmp_path / "2026-10-14.jsonl.gz").write_bytes(gzip.compress("\n".join(json.dumps(x) for x in lines).encode()))
    snaps, calls, days = analyze.load(tmp_path)
    rows = analyze.per_token(snaps, None)
    text = analyze.report(rows, calls, days, None)
    traits = text.split("### Ran 4x+")[1].split("###")[0]
    names = [ln.split(" | ")[0].strip("| ") for ln in traits.splitlines() if ln.startswith("| ") and "factor" not in ln]
    assert set(names[:3]) == {"holders", "bundled at launch %", "scorecard score"}   # the score mixes both
    assert names[-1] == "noise"
    assert "## 5. Simple rules" in text and "holders >" in text


def test_auc_basics():
    x = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    assert analyze.auc(x, (x > 5).astype(int)) == 1.0
    assert analyze.auc(-x, (x > 5).astype(int)) == 0.0
    assert analyze.auc(np.ones(10), (x > 5).astype(int)) == 0.5


def test_skipped_launchpads_and_mispriced_pools_are_set_aside_at_start(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, n=0)
    fk, packed = e.db.pack({"px_mcap_usd": 1.0})
    for tid, pad, mcap in (("solana:farm", "fluxbeam", 200000.0), ("solana:empty", "meteora-damm-v2", 1.0),
                           ("solana:fine", "meteora-damm-v2", 20000.0)):
        e.db.execute("INSERT INTO tokens(id,family,chain,launchpad) VALUES(?,?,?,?)", (tid, "dex", "solana", pad))
        e.db.execute("INSERT INTO snapshots(token_id,family,cp,ts,price,mcap_usd,fkeys,features,done,hit,tainted) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (tid, "dex", 60, DAY0, 1.0, mcap, fk, packed, 1, 1, 0))
    e.db.commit()
    asyncio.run(e.http.close())
    e2 = Engine(e.cfg)
    left = [r["token_id"] for r in e2.db.query("SELECT token_id FROM snapshots WHERE tainted=0")]
    assert left == ["solana:fine"]
    asyncio.run(e2.http.close())
