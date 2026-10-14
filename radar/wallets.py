"""Wallet and creator reputation, learned from outcomes.

Nobody hands us a list of smart wallets, so the scanner builds its own: every finished token
credits (or blames) its early buyers and its creator. A wallet is "smart" when it is early in
winners far more often than the average early buyer and has realised a profit; "toxic" when it
keeps showing up in the creation slot of coins that die, or keeps dumping large bags.
Reputation only ever uses finished tokens, so a snapshot never sees its own future.
"""
from __future__ import annotations

import bisect

from .state import (B_H, B_IDX, BOUGHT, BUY_Q, FIRST_PRICE, FIRST_SLOT, FIRST_TS, SELL_Q, SOLD, SUPPLY, DexToken,
                    PumpToken)

SHRINK = 8.0                      # prior strength: a wallet starts at the average and earns its way out
BAD_ENDINGS = {"dev_dump", "bundle_dump", "whale_dump", "fast_fade"}


class Reputation:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.base = db.get("wallet_base", {"n": 0, "win": 0, "dead": 0})

    # ---------------------------------------------------------------- reading
    def rates(self) -> tuple[float, float]:
        n = self.base["n"]
        if n < 200:
            return 0.10, 0.60                     # rough starting guess until there is data
        return max(self.base["win"] / n, 1e-3), max(self.base["dead"] / n, 1e-3)

    def lookup(self, addrs: list[str]) -> dict:
        if not addrs:
            return {}
        marks = ",".join("?" * len(addrs))
        return {r["addr"]: r for r in self.db.query(f"SELECT * FROM wallets WHERE addr IN ({marks})", tuple(addrs))}

    def judge(self, r) -> tuple[float, bool, bool, bool]:
        """(win-rate lift vs. the average early buyer, smart?, toxic?, sniper bot?)"""
        win_rate, _ = self.rates()
        n = r["n_tok"]
        win_lift = (r["n_win"] + SHRINK * win_rate) / (n + SHRINK) / win_rate
        enough = n >= self.cfg.min_tokens
        smart = enough and win_lift >= self.cfg.smart_lift and r["pnl"] > 0
        # a bundler: mostly buys in the creation slot, and what it buys rarely goes anywhere
        bundler = enough and r["n_slot0"] >= 0.5 * n and win_lift <= self.cfg.toxic_lift
        toxic = bundler or r["n_dump"] >= 3
        return win_lift, smart, toxic, r["n_slot0"] >= 5

    def summarize(self, addrs: list[str]) -> dict:
        """Wallet-quality factors for a list of early buyers."""
        rows = self.lookup(addrs)
        n = len(addrs)
        out = {"wq_known_share": len(rows) / n if n else None, "wq_smart_n": 0, "wq_toxic_n": 0, "wq_bot_n": 0,
               "wq_lift_mean": None, "wq_pnl_sum": 0.0}
        lifts, smart = [], []
        for a, r in rows.items():
            lift, is_smart, is_toxic, is_bot = self.judge(r)
            lifts.append(lift)
            out["wq_smart_n"] += is_smart
            out["wq_toxic_n"] += is_toxic
            out["wq_bot_n"] += is_bot
            out["wq_pnl_sum"] += r["pnl"]
            if is_smart:
                smart.append(a)
        if lifts:
            out["wq_lift_mean"] = sum(lifts) / len(lifts)
        out["wq_toxic_share"] = out["wq_toxic_n"] / n if n else None
        out["_smart"] = smart
        return out

    def creator(self, addr: str):
        return self.db.one("SELECT * FROM creators WHERE addr=?", (addr,))

    # ---------------------------------------------------------------- writing
    def creator_launched(self, addr: str, ts: float) -> tuple[int, int]:
        """Count a launch. Returns (launches before this one, launches in the last 24 h before this one)."""
        r = self.creator(addr)
        if r is None:
            self.db.execute("INSERT INTO creators(addr,n_created,day_ts,day_n,last_ts) VALUES(?,?,?,?,?)",
                            (addr, 1, ts, 1, ts))
            return 0, 0
        day_n = r["day_n"] if ts - r["day_ts"] <= 86400 else 0
        day_ts = r["day_ts"] if day_n else ts
        self.db.execute("UPDATE creators SET n_created=n_created+1, day_ts=?, day_n=?, last_ts=? WHERE addr=?",
                        (day_ts, day_n + 1, ts, addr))
        return r["n_created"], day_n

    def credit(self, tok, final: dict, quote_usd: float | None) -> None:
        """Token finished: update its creator and early buyers."""
        if tok.tainted:
            return
        entries = self._entries(tok)
        peak, win_tok = final.get("peak_mult") or 0.0, (final.get("peak_mult") or 0.0) >= 4.0
        dead_tok = final.get("kind") in BAD_ENDINGS or final.get("kind") == "never_started"
        if isinstance(tok, PumpToken):
            self.db.execute(
                "UPDATE creators SET n_snap=n_snap+1, n_grad=n_grad+?, n_win=n_win+?, n_dead=n_dead+?, "
                "best_mult=MAX(best_mult,?) WHERE addr=?",
                (int(bool(tok.graduated_ts)), int(win_tok), int(dead_tok), peak, tok.user))
        usd = quote_usd or 0.0
        rows = []
        for wallet, win, dead, slot0, dump, pnl_q in entries:
            rows.append((wallet, int(win), int(dead), int(slot0), int(dump and final.get("kind") in BAD_ENDINGS),
                         pnl_q * usd, tok.last_trade_ts if isinstance(tok, PumpToken) else tok.created_ts))
            self.base["n"] += 1
            self.base["win"] += int(win)
            self.base["dead"] += int(dead)
        self.db.executemany(
            "INSERT INTO wallets(addr,n_tok,n_win,n_dead,n_slot0,n_dump,pnl,last_ts) VALUES(?,1,?,?,?,?,?,?) "
            "ON CONFLICT(addr) DO UPDATE SET n_tok=n_tok+1, n_win=n_win+excluded.n_win, n_dead=n_dead+excluded.n_dead, "
            "n_slot0=n_slot0+excluded.n_slot0, n_dump=n_dump+excluded.n_dump, pnl=pnl+excluded.pnl, "
            "last_ts=excluded.last_ts", rows)
        self.db.put("wallet_base", self.base)

    def _entries(self, tok) -> list[tuple]:
        """(wallet, won, lost, bought in creation slot, dumped a big bag, realised pnl in quote units)"""
        out = []
        if isinstance(tok, PumpToken):
            bars = tok.bars
            idxs = [b[B_IDX] for b in bars]
            suffix = [0.0] * (len(bars) + 1)
            for i in range(len(bars) - 1, -1, -1):
                suffix[i] = max(suffix[i + 1], bars[i][B_H])
            end = tok.price
            for w in tok.early_buyers(self.cfg.early_buyers):
                r = tok.wallets.get(w)
                if r is None or r[FIRST_PRICE] <= 0:
                    continue
                i = bisect.bisect_left(idxs, int(r[FIRST_TS] // tok.bar_s))
                best = max(suffix[i], end) / r[FIRST_PRICE]
                sold_part = min(1.0, r[SOLD] / r[BOUGHT]) if r[BOUGHT] > 0 else 0.0
                out.append((w, best >= 2.0, best < 1.2 and end < 0.5 * r[FIRST_PRICE],
                            r[FIRST_SLOT] <= tok.created_slot, r[SOLD] >= 0.015 * SUPPLY,
                            r[SELL_Q] - r[BUY_Q] * sold_part))
        elif isinstance(tok, DexToken) and tok.trades:
            for w, (ts, price, first_block) in tok.trades.get("entries", {}).items():
                if price <= 0:
                    continue
                best = (tok.max_price if tok.max_ts >= ts else tok.price) / price
                out.append((w, best >= 2.0, best < 1.2 and tok.price < 0.5 * price, first_block, False, 0.0))
        return out
