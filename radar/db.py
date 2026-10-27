"""SQLite store (WAL). One connection, used only from the event loop thread; the trainer
opens its own read-only connection. Writes are grouped and committed about once a second.

What is kept, and why (everything else is never written or is pruned, see Engine.maintain):
  snapshots   every factor at each checkpoint + its outcome  -> the training data
  tokens      one row per snapshotted token: how it ended + a thinned price path -> failure patterns
  calls       every alert and how it did                       -> the scoreboard
  wallets / creators / seen   running counters                -> reputation and uniqueness factors
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  id TEXT PRIMARY KEY,            -- "<chain>:<address>"
  family TEXT, chain TEXT, launchpad TEXT, address TEXT, pool TEXT,
  name TEXT, symbol TEXT, uri TEXT, creator TEXT, quote TEXT,
  created_ts REAL, created_slot INTEGER,
  meta TEXT,                      -- socials, image, flags (JSON)
  status TEXT DEFAULT 'live',     -- live / graduated / dead
  graduated_ts REAL, peak_mcap_usd REAL, last_mcap_usd REAL,
  liq_max_usd REAL,               -- most liquidity seen at a snapshot
  pull_state INTEGER,             -- DEX pools, looked up again later: NULL not yet, 0 still there, 1 liquidity gone
  pull_checks INTEGER DEFAULT 0, pull_ts REAL, liq_now_usd REAL,
  final TEXT,                     -- how it ended: death stats + price path (JSON)
  updated_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_tokens_created ON tokens(created_ts);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_id TEXT, family TEXT, cp INTEGER, ts REAL, token_created_ts REAL,
  price REAL, mcap_usd REAL,
  fkeys INTEGER,                  -- layout of `features` (feature_keys.id)
  features TEXT,                  -- every factor, as known at ts (JSON array in fkeys order)
  prior REAL, prob REAL, model_ver TEXT, gate TEXT, called INTEGER DEFAULT 0,
  done INTEGER DEFAULT 0, tainted INTEGER DEFAULT 0,
  hit INTEGER, stopped INTEGER, peak_mult REAL, min_mult REAL, end_mult REAL,
  t_hit REAL, t_stop REAL, t_peak REAL, end_reason TEXT, done_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_token ON snapshots(token_id);
CREATE INDEX IF NOT EXISTS ix_snap_done ON snapshots(family, done, ts);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER UNIQUE, token_id TEXT, ts REAL, price REAL, mcap_usd REAL,
  prior REAL, prob REAL, model_ver TEXT, reasons TEXT, tg_msg_id INTEGER,
  status TEXT DEFAULT 'open',     -- open / hit / stopped / expired
  peak_mult REAL, t_hit REAL, closed_ts REAL,
  tier INTEGER                    -- conviction: 0 standard, 1 high (top 5% of the day), 2 very high (top 1%)
);
CREATE INDEX IF NOT EXISTS ix_calls_ts ON calls(ts);

CREATE TABLE IF NOT EXISTS wallets (
  addr TEXT PRIMARY KEY, n_tok INTEGER DEFAULT 0, n_win INTEGER DEFAULT 0, n_dead INTEGER DEFAULT 0,
  n_slot0 INTEGER DEFAULT 0, n_dump INTEGER DEFAULT 0, pnl REAL DEFAULT 0, last_ts REAL
);
CREATE TABLE IF NOT EXISTS creators (
  addr TEXT PRIMARY KEY, n_created INTEGER DEFAULT 0, n_snap INTEGER DEFAULT 0, n_grad INTEGER DEFAULT 0,
  n_win INTEGER DEFAULT 0, n_dead INTEGER DEFAULT 0, best_mult REAL DEFAULT 0,
  day_ts REAL DEFAULT 0, day_n INTEGER DEFAULT 0, last_ts REAL
);
-- how often a name / symbol / metadata uri / image / social handle has been seen before
CREATE TABLE IF NOT EXISTS seen (
  kind TEXT, key TEXT, n INTEGER DEFAULT 0, first_ts REAL, last_ts REAL, win_ts REAL, win_n INTEGER DEFAULT 0,
  PRIMARY KEY (kind, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT, family TEXT, ver TEXT, ts REAL, n_rows INTEGER, n_pos INTEGER,
  metrics TEXT, path TEXT, active INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS feature_keys (id INTEGER PRIMARY KEY, keys TEXT UNIQUE);   -- factor names per layout
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS alerts (dedupe TEXT PRIMARY KEY, ts REAL, kind TEXT, text TEXT, msg_id INTEGER, status TEXT);
"""

SEEN_WINDOW_S = 600


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        if not self.con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            self.con.execute("PRAGMA auto_vacuum=INCREMENTAL")   # only takes effect on a new, empty file
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript(SCHEMA)
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(snapshots)")}
        if "fkeys" not in cols:
            self.con.execute("ALTER TABLE snapshots ADD COLUMN fkeys INTEGER")
        if "tier" not in {r["name"] for r in self.con.execute("PRAGMA table_info(calls)")}:
            self.con.execute("ALTER TABLE calls ADD COLUMN tier INTEGER")      # conviction: 0 / 1 high / 2 very high
        self._add_columns("tokens", {"liq_max_usd": "REAL", "pull_state": "INTEGER", "pull_checks": "INTEGER DEFAULT 0",
                                     "pull_ts": "REAL", "liq_now_usd": "REAL"})
        self._add_columns("creators", {"n_pulled": "INTEGER DEFAULT 0", "n_kept": "INTEGER DEFAULT 0"})
        self._add_columns("snapshots", {"tx": "TEXT"})          # how tradeable the move was (Label.tradeable)
        self._in_tx = False
        self._layouts: dict[tuple, int] = {}
        self._keys: dict[int, tuple] = {}

    def _add_columns(self, table: str, cols: dict) -> None:
        have = {r["name"] for r in self.con.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols.items():
            if name not in have:
                self.con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    # -- transactions: begin lazily, commit from the engine tick
    def _begin(self) -> None:
        if not self._in_tx:
            self.con.execute("BEGIN")
            self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self.con.execute("COMMIT")
            self._in_tx = False

    def execute(self, sql: str, args: tuple | dict = ()) -> sqlite3.Cursor:
        self._begin()
        return self.con.execute(sql, args)

    def executemany(self, sql: str, rows) -> None:
        self._begin()
        self.con.executemany(sql, rows)

    def query(self, sql: str, args: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | dict = ()):
        return self.con.execute(sql, args).fetchone()

    def close(self) -> None:
        self.commit()
        self.con.close()

    # -- key/value state
    def get(self, k: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def put(self, k: str, v) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # -- "seen before" counters
    def seen_bump(self, kind: str, key: str, ts: float) -> tuple[int, float, int]:
        """Count one more sighting. Returns (times seen BEFORE this one, first seen ts, sightings
        in the current 10-minute window before this one)."""
        r = self.one("SELECT n, first_ts, win_ts, win_n FROM seen WHERE kind=? AND key=?", (kind, key))
        if r is None:
            self.execute("INSERT INTO seen(kind,key,n,first_ts,last_ts,win_ts,win_n) VALUES(?,?,?,?,?,?,?)",
                         (kind, key, 1, ts, ts, ts, 1))
            return 0, ts, 0
        win_n = r["win_n"] if ts - r["win_ts"] <= SEEN_WINDOW_S else 0
        win_ts = r["win_ts"] if win_n else ts
        self.execute("UPDATE seen SET n=n+1, last_ts=?, win_ts=?, win_n=? WHERE kind=? AND key=?",
                     (ts, win_ts, win_n + 1, kind, key))
        return r["n"], r["first_ts"], win_n

    def seen_count(self, kind: str, key: str) -> int:
        r = self.one("SELECT n FROM seen WHERE kind=? AND key=?", (kind, key))
        return r["n"] if r else 0

    # -- compact feature storage
    def pack(self, f: dict) -> tuple[int, str]:
        """(layout id, JSON array of values). Factor names are stored once per layout instead of in
        every row, floats keep 4 significant digits: ~4x smaller than a plain JSON object."""
        keys = tuple(sorted(f))
        kid = self._layouts.get(keys)
        if kid is None:
            text = json.dumps(keys)
            r = self.one("SELECT id FROM feature_keys WHERE keys=?", (text,))
            kid = r["id"] if r else self.execute("INSERT INTO feature_keys(keys) VALUES(?)", (text,)).lastrowid
            self._layouts[keys] = kid
            self._keys[kid] = keys
        return kid, json.dumps([_compact(f[k]) for k in keys], separators=(",", ":"), default=str)

    def unpack(self, kid: int | None, text: str | None) -> dict:
        if not text:
            return {}
        vals = json.loads(text)
        if kid is None:                                      # rows written before layouts existed
            return vals
        keys = self._keys.get(kid)
        if keys is None:
            r = self.one("SELECT keys FROM feature_keys WHERE id=?", (kid,))
            keys = self._keys[kid] = tuple(json.loads(r["keys"])) if r else ()
        return dict(zip(keys, vals))

    # -- maintenance (called in small batches so the event loop never stalls)
    def delete_batch(self, table: str, where: str, args: tuple, limit: int = 2000) -> int:
        key, cols = ("(kind, key)", "kind, key") if table == "seen" else ("rowid", "rowid")   # `seen` has no rowid
        n = self.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT {cols} FROM {table} WHERE {where} LIMIT {limit})",
                         args).rowcount
        self.commit()
        return n

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in (self.path, Path(f"{self.path}-wal")) if p.exists())

    def reclaim(self, pages: int = 20000) -> None:
        """Give freed pages back to the disk (auto_vacuum=INCREMENTAL) and trim the WAL."""
        self.commit()
        self.con.execute(f"PRAGMA incremental_vacuum({pages})").fetchall()
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def _compact(v):
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return float(f"{v:.4g}")
    if isinstance(v, bool):
        return int(v)
    return v
