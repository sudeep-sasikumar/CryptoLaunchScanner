"""Research files: one gzip-compressed JSON-lines file per UTC day with every finished snapshot of
that day (all factors + what happened next), that day's calls, and how each token that ended that
day ended. Served by the dashboard (/api/research), copied to the PC by tools/sync_research.py,
studied by tools/analyze.py. Holds token addresses and market data only: no keys, no passwords."""
from __future__ import annotations

import calendar
import gzip
import io
import json
import re
import sqlite3
import time
from pathlib import Path

NAME = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl\.gz")


def day_bounds(day: str) -> tuple[float, float]:
    start = float(calendar.timegm(time.strptime(day, "%Y-%m-%d")))
    return start, start + 86400


def list_days(db_path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute("SELECT date(ts, 'unixepoch') d, COUNT(*) FROM snapshots WHERE tainted = 0 "
                           "AND (done = 1 OR hit = 1) GROUP BY d ORDER BY d").fetchall()
    finally:
        con.close()
    return [{"name": f"{d}.jsonl.gz", "snapshots": n} for d, n in rows if d]


def build_day(db_path: Path, day: str) -> bytes:
    """Runs in a worker thread with its own read-only connection."""
    start, end = day_bounds(day)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    layouts = {r["id"]: json.loads(r["keys"]) for r in con.execute("SELECT id, keys FROM feature_keys")}
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        def put(obj):
            gz.write((json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode())
        put({"kind": "meta", "day": day, "exported_at": time.time(), "format": 1})
        for r in con.execute(
                "SELECT s.id, s.token_id, s.family, s.cp, s.ts, s.token_created_ts, s.price, s.mcap_usd, s.fkeys, "
                "s.features, s.prior, s.prob, s.model_ver, s.gate, s.called, s.done, s.hit, s.stopped, "
                "s.peak_mult, s.min_mult, s.end_mult, s.t_hit, s.t_stop, s.t_peak, s.end_reason, "
                "t.launchpad, t.chain, t.symbol, t.name FROM snapshots s LEFT JOIN tokens t ON t.id = s.token_id "
                "WHERE s.ts >= ? AND s.ts < ? AND s.tainted = 0 AND (s.done = 1 OR s.hit = 1)", (start, end)):
            row = dict(r)
            vals = json.loads(row.pop("features") or "null")
            keys = layouts.get(row.pop("fkeys"))
            row["f"] = dict(zip(keys, vals)) if keys and isinstance(vals, list) else (vals or {})
            put({"kind": "snapshot", **row})
        for r in con.execute("SELECT * FROM calls WHERE ts >= ? AND ts < ?", (start, end)):
            put({"kind": "call", **dict(r)})
        for r in con.execute("SELECT id, family, chain, launchpad, symbol, created_ts, status, peak_mcap_usd, final "
                             "FROM tokens WHERE updated_ts >= ? AND updated_ts < ? AND final IS NOT NULL", (start, end)):
            row = dict(r)
            row["final"] = json.loads(row["final"] or "null")
            if isinstance(row["final"], dict):
                row["final"].pop("path", None)            # price paths are large; the ending stats are enough here
            put({"kind": "token", **row})
        for r in con.execute("SELECT id, launchpad, chain, creator, liq_max_usd, liq_now_usd, pull_state, pull_checks, "
                             "pull_ts FROM tokens WHERE created_ts >= ? AND created_ts < ? AND pull_checks > 0", (start, end)):
            put({"kind": "pull", **dict(r)})                 # looked up again later: was the liquidity still there?
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='paper'").fetchone():
            for r in con.execute("SELECT * FROM paper WHERE opened_ts >= ? AND opened_ts < ?", (start, end)):
                row = dict(r)
                row["steps"] = json.loads(row["steps"] or "[]")
                put({"kind": "paper", **row})
    con.close()
    return buf.getvalue()
