"""Paper trading: what the take-profit plan would have done with real money, on live prices, no money involved.

Each paper trade follows the call's own price feed (the same 15-second bar averages that grade the call),
so it needs no extra data. Kept realistic on purpose:
- bought at the price when the call goes out (not the snapshot's), plus `slippage_pct`
- every sell fills `slippage_pct` below its target; a stop that is gapped through fills at the price seen
- `fee_pct` charged on every buy and sell
- whatever is still held when the scanner stops following the coin (6 h at most) is sold at the last price
It also records the lowest point before the first take-profit and the highest point, so other stops and
targets can be checked exactly later."""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER UNIQUE, token_id TEXT, symbol TEXT, launchpad TEXT, tier INTEGER,
  opened_ts REAL, stake_usd REAL, factor REAL,       -- factor: call-label multiple -> this trade's multiple
  left REAL, cash REAL,                              -- share of the position still held; USD received so far
  state TEXT DEFAULT 'open', closed_ts REAL, reason TEXT, pnl_usd REAL,
  last REAL, peak REAL, low_pre REAL, t_first REAL,  -- multiples of this trade's entry
  steps TEXT                                         -- JSON list of what was sold, when, at what multiple
);
CREATE INDEX IF NOT EXISTS ix_paper_open ON paper(state);
"""


class Paper:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.db.con.executescript(SCHEMA)
        self.live: dict[int, dict] = {}                # call_id -> open position (with its Label)
        n = self.db.execute("UPDATE paper SET state='closed', reason='restart', closed_ts=?, "
                            "pnl_usd=cash + left*stake_usd*COALESCE(last,0)*(1-?/100.0)*(1-?/100.0) - stake_usd "
                            "WHERE state='open'", (time.time(), cfg.slippage_pct, cfg.fee_pct)).rowcount
        if n:
            log.info("paper: %d open trades closed at their last price (restart)", n)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def wants(self, tier: int, f: dict) -> bool:
        return self.enabled and tier >= self.cfg.min_tier

    def plan(self) -> list[tuple[float, float]]:
        return [(float(m), float(s)) for m, s in self.cfg.take_profit]

    # ------------------------------------------------------------------ opening
    def open(self, call_id: int, tok, lb, tier: int, price_now: float, now: float) -> None:
        """Buy at the price when the call goes out (+ slippage), however far it moved since the snapshot."""
        c = self.cfg
        entry = price_now * (1 + c.slippage_pct / 100.0)
        factor = lb.entry / entry if entry > 0 else 1.0       # label multiple x factor = this trade's multiple
        stake = float(c.stake_usd)
        pos = {"call_id": call_id, "lb": lb, "factor": factor, "stake": stake, "left": 1.0,
               "cash": -stake * c.fee_pct / 100.0,            # buy fee
               "peak": lb.peak * factor, "low": lb.last * factor, "last": lb.last * factor,
               "steps": [], "opened": now, "next": 0, "first_done": False, "t_first": None}
        self.live[call_id] = pos
        self.db.execute("INSERT OR IGNORE INTO paper(call_id,token_id,symbol,launchpad,tier,opened_ts,stake_usd,factor,"
                        "left,cash,last,peak,low_pre) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (call_id, tok.id, tok.symbol[:40], tok.launchpad, tier, now, stake, factor, 1.0,
                         pos["cash"], pos["last"], pos["peak"], pos["low"]))
        log.info("paper: bought $%.0f of %s (call %d, %.2fx the call price)", stake, tok.symbol, call_id, 1 / factor)

    # ------------------------------------------------------------------ following
    def step(self, now: float) -> list[tuple[int, str]]:
        """Check every open trade against its label's latest prices. Returns [(call_id, message)] for
        what happened (sells, stop, close), so the engine can report it if wanted."""
        out = []
        for cid, pos in list(self.live.items()):
            lb = pos["lb"]
            m_last, m_peak, m_low = lb.last * pos["factor"], lb.peak * pos["factor"], lb.low * pos["factor"]
            pos["last"] = m_last
            pos["peak"] = max(pos["peak"], m_peak)
            if not pos["first_done"]:
                pos["low"] = min(pos["low"], m_low)
            msgs = self._sells(pos, now)
            if pos["left"] > 1e-9 and lb.done:
                msgs.append(self._sell(pos, pos["left"], m_last, now, "end of tracking"))
            if pos["left"] <= 1e-9:
                msgs.append(self._close(pos, now, "sold out" if pos["steps"] and pos["steps"][-1]["why"].startswith("take")
                                        else pos["steps"][-1]["why"] if pos["steps"] else "closed"))
            elif msgs:
                self._save(pos)
            out += [(cid, m) for m in msgs if m]
        return out

    def _sells(self, pos: dict, now: float) -> list[str]:
        c, plan, msgs = self.cfg, self.plan(), []
        while pos["next"] < len(plan) and pos["left"] > 1e-9 and pos["peak"] >= plan[pos["next"]][0]:
            mult, share = plan[pos["next"]]
            msgs.append(self._sell(pos, min(share, pos["left"]), mult, now, f"take profit {mult:g}x"))
            pos["next"] += 1
            if not pos["first_done"]:
                pos["first_done"], pos["t_first"] = True, now - pos["opened"]
        stop = c.stop if not pos["first_done"] else c.get("stop_after_first")
        if stop is not None and pos["left"] > 1e-9 and pos["last"] <= stop:
            # gapped through the stop: sold where the price actually is, not at the stop
            msgs.append(self._sell(pos, pos["left"], min(stop, pos["last"]), now, f"stop {(stop - 1) * 100:+.0f}%"))
        return msgs

    def _sell(self, pos: dict, share: float, mult: float, now: float, why: str) -> str:
        c = self.cfg
        fill = mult * (1 - c.slippage_pct / 100.0)
        usd = share * pos["stake"] * fill * (1 - c.fee_pct / 100.0)
        pos["cash"] += usd
        pos["left"] -= share
        pos["steps"].append({"t": round(now - pos["opened"]), "share": round(share, 4), "mult": round(fill, 4),
                             "usd": round(usd, 2), "why": why})
        return f"sold {share * 100:.0f}% at {fill:.2f}x (${usd:.2f}) - {why}"

    def _close(self, pos: dict, now: float, reason: str) -> str:
        pnl = pos["cash"] - pos["stake"]
        self.live.pop(pos["call_id"], None)
        self._save(pos, closed=(now, reason, pnl))
        return f"closed: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f} on ${pos['stake']:.0f}"

    def _save(self, pos: dict, closed=None) -> None:
        self.db.execute(
            "UPDATE paper SET left=?, cash=?, last=?, peak=?, low_pre=?, t_first=?, steps=?" +
            (", state='closed', closed_ts=?, reason=?, pnl_usd=?" if closed else "") + " WHERE call_id=?",
            (max(pos["left"], 0.0), pos["cash"], pos["last"], pos["peak"], pos["low"], pos["t_first"],
             json.dumps(pos["steps"]), *(closed or ()), pos["call_id"]))

    def touch(self, now: float) -> None:
        """Keep open trades' last price on disk (a restart closes them at it)."""
        for pos in self.live.values():
            self.db.execute("UPDATE paper SET last=?, peak=?, low_pre=? WHERE call_id=?",
                            (pos["last"], pos["peak"], pos["low"], pos["call_id"]))

    # ------------------------------------------------------------------ reporting
    def stats(self, since: float = 0.0) -> dict:
        r = self.db.one("SELECT COUNT(*) n, SUM(state='open') open, SUM(CASE WHEN state='closed' THEN pnl_usd END) pnl, "
                        "SUM(CASE WHEN state='closed' THEN stake_usd END) staked, SUM(state='closed' AND pnl_usd>0) wins, "
                        "SUM(state='closed') closed FROM paper WHERE opened_ts > ?", (since,))
        return {k: r[k] or 0 for k in r.keys()}

    def summary_line(self, since: float) -> str | None:
        s = self.stats(since)
        if not s["n"]:
            return None
        roi = s["pnl"] / s["staked"] * 100 if s["staked"] else 0
        return (f"📝 Paper trading: {s['closed']} closed ({s['wins']} in profit) → "
                f"{'+' if s['pnl'] >= 0 else '-'}${abs(s['pnl']):.2f} ({roi:+.0f}% of ${s['staked']:.0f} staked)"
                f", {s['open']} open")
