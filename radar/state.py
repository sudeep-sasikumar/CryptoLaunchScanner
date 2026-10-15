"""In-memory state of tokens being watched.

Label      - the outcome of one snapshot (2x before -50%?), fed with prices after the snapshot.
PumpToken  - a pump.fun coin rebuilt trade by trade: who holds what, who bought when, the bars.
DexToken   - any other launch, known only through periodic DexScreener snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SUPPLY = 1_000_000_000.0          # pump.fun coins: 1bn supply, 6 decimals
CURVE_TOKENS = 793_100_000.0      # sold through the bonding curve before graduation

# wallet record indexes (a list per wallet: this runs for every trade)
NET, BOUGHT, SOLD, BUY_Q, SELL_Q, FIRST_SLOT, FIRST_TS, NB, NS, FIRST_PRICE = range(10)
# bar indexes
B_IDX, B_O, B_H, B_L, B_C, B_BQ, B_SQ, B_NB, B_NS, B_TOK, B_NEW = range(11)


class Label:
    """Did price reach target x entry before falling to stop x entry?  All prices are in the
    token's own quote unit, so the multiple is exact whatever the quote is."""
    __slots__ = ("snap_id", "call_id", "t0", "entry", "target", "stop", "horizon_end", "peak", "t_peak",
                 "low", "last", "hit", "t_hit", "stopped", "t_stop", "done", "end_reason", "tainted", "told")

    def __init__(self, snap_id: int, t0: float, price: float, cfg):
        self.snap_id, self.call_id, self.t0 = snap_id, None, t0
        self.entry = price * (1 + cfg.entry_slippage_pct / 100.0)
        self.target, self.stop = cfg.target, cfg.stop
        self.horizon_end = t0 + cfg.horizon_s
        self.peak = self.low = self.last = price / self.entry
        self.t_peak = 0.0
        self.hit = self.stopped = self.done = self.tainted = False
        self.told = 0.0                       # highest multiple already announced to the user (runner updates)
        self.t_hit = self.t_stop = None
        self.end_reason = None

    def update(self, ts: float, price: float) -> str | None:
        """Feed one price (a closed bar's average, or a polled price). Returns 'hit' / 'stop' the
        first time either happens, else None."""
        if self.done or ts <= self.t0:
            return None
        m = price / self.entry
        self.last = m
        if m > self.peak:
            self.peak, self.t_peak = m, ts - self.t0
        if m < self.low:
            self.low = m
        event = None
        if not self.hit:
            if m >= self.target:
                self.hit, self.t_hit, event = True, ts - self.t0, "hit"
            elif m <= self.stop:
                self.stopped, self.t_stop, event = True, ts - self.t0, "stop"
                self.finish("stop")
        elif m <= self.stop:
            self.finish("faded")              # ran, then gave it all back: nothing more to learn
        return event

    def finish(self, reason: str) -> None:
        if not self.done:
            self.done, self.end_reason = True, reason

    def tick(self, now: float) -> None:
        if not self.done and now >= self.horizon_end:
            self.finish("horizon")

    def row(self, now: float) -> tuple:
        return (1, int(self.tainted), int(self.hit), int(self.stopped), self.peak, self.low, self.last,
                self.t_hit, self.t_stop, self.t_peak, self.end_reason, now, self.snap_id)


LABEL_SQL = ("UPDATE snapshots SET done=?, tainted=?, hit=?, stopped=?, peak_mult=?, min_mult=?, end_mult=?, "
             "t_hit=?, t_stop=?, t_peak=?, end_reason=?, done_ts=? WHERE id=?")


class PumpToken:
    family = "pump"

    def __init__(self, e: dict, slot: int, qdec: int, cfg):
        self.mint = e["mint"]
        self.id = "solana:" + self.mint
        self.chain, self.launchpad = "solana", "pump.fun"
        self.name, self.symbol, self.uri = e.get("name", ""), e.get("symbol", ""), e.get("uri", "")
        self.user, self.creator = e["user"], e.get("creator") or e["user"]
        self.devs = {self.user, self.creator}
        self.created_ts, self.created_slot = float(e["timestamp"]), slot
        self.quote, self.qdec = e.get("quote_mint", ""), qdec
        self.q_known = True                   # False while the quote token's decimals are still a guess
        self.flags = {"mayhem": int(bool(e.get("is_mayhem_mode"))), "cashback": int(bool(e.get("is_cashback_enabled"))),
                      "holder_reward": int(bool(e.get("is_holder_reward"))), "creator_fee_bps": e.get("creator_fee_bps", 0),
                      "depth": e.get("depth", 0), "user_is_creator": int(self.user == self.creator)}
        vq = e.get("virtual_quote_reserves") or e.get("virtual_sol_reserves") or 0
        vt = e.get("virtual_token_reserves") or 1
        self.scale = 10.0 ** (6 - qdec)                       # raw reserve ratio -> quote units per token
        self.p0 = vq / vt * self.scale                        # launch price
        self.price = self.max_price = self.p0
        self.max_ts = self.created_ts
        self.low_after_max = self.p0
        self.curve_left = CURVE_TOKENS
        self.real_q = 0.0                     # quote actually held by the curve (its liquidity)
        self.bar_s = cfg.bar_s
        self.max_early, self.max_wallets = cfg.early_trades, cfg.max_wallets
        self.sniper_slots = cfg.sniper_slots

        self.wallets: dict[str, list] = {}
        self.early: list[tuple] = []          # (ts, slot, wallet, is_buy, tokens, quote)
        self.bars: list[list] = []            # closed bars
        self.bar: list | None = None          # bar being built
        self.n_buys = self.n_sells = 0
        self.buy_q = self.sell_q = 0.0
        self.max_buy_tok = self.max_sell_tok = 0.0
        self.max_sell = None                  # (ts, role, share of supply) of the largest single sell
        self.role_sold = {"dev": 0.0, "slot0": 0.0, "sniper": 0.0, "other": 0.0}
        self.phantom_sold = 0.0               # sold by wallets that never bought here (received by transfer)
        self.first_buy_ts: float | None = None
        self.last_trade_ts = self.created_ts
        self.graduated_ts: float | None = None
        self.grad_usd_per_q: float | None = None

        self.labels: list[Label] = []
        self.next_cp = 0                      # index into the checkpoint list
        self.snapshots = 0
        self.tainted = False                  # the stream dropped while this token was live
        self.meta: dict | None = None         # filled by the metadata fetch
        self.security: dict | None = None     # filled by the contract check (GoPlus, when GMGN is off)
        self.gm: dict | None = None           # GMGN's token page as factors (latest)
        self.gm_feed: dict | None = None      # what GMGN's launch feed said (socials, reuse)
        self.cx: tuple | None = None          # (time, Codex holder picture) from the batch refresh
        self.gti = None                       # GeckoTerminal token info (unused for pump.fun coins)
        self.meta_requested = False
        self.seen: dict = {}                  # name / symbol / uri counts at creation
        self.called = False
        self.saved = False                    # row exists in tokens table

    # ---------------------------------------------------------------- feeding
    def role(self, wallet: str, rec: list | None) -> str:
        if wallet in self.devs:
            return "dev"
        if rec is None:
            return "other"
        d = rec[FIRST_SLOT] - self.created_slot
        return "slot0" if d <= 0 else "sniper" if d <= self.sniper_slots else "other"

    def on_trade(self, e: dict, slot: int) -> list[tuple[Label, str]]:
        """Apply one trade. Returns label events [(label, 'hit'|'stop')] from any bar it closed."""
        ts = float(e["timestamp"])
        tok = e["token_amount"] / 1e6
        q_raw = e.get("quote_amount")
        if q_raw is None:
            q_raw = e.get("sol_amount", 0)
        q = q_raw / 10.0 ** self.qdec
        vq = e.get("virtual_quote_reserves") or e.get("virtual_sol_reserves") or 0
        vt = e.get("virtual_token_reserves") or 0
        if vq and vt:
            self.price = vq / vt * self.scale
        self.curve_left = e.get("real_token_reserves", 0) / 1e6
        rq = e.get("real_quote_reserves")
        self.real_q = (rq if rq is not None else e.get("real_sol_reserves", 0)) / 10.0 ** self.qdec
        wallet, is_buy = e["user"], bool(e["is_buy"])
        events = self._bar_trade(ts, is_buy, tok, q)

        rec = self.wallets.get(wallet)
        if is_buy:
            self.n_buys += 1
            self.buy_q += q
            if tok > self.max_buy_tok:
                self.max_buy_tok = tok
            if rec is None:
                if len(self.wallets) < self.max_wallets:
                    rec = self.wallets[wallet] = [0.0, 0.0, 0.0, 0.0, 0.0, slot, ts, 0, 0, self.price]
                    self.bar[B_NEW] += 1
            if rec is not None:
                rec[NET] += tok
                rec[BOUGHT] += tok
                rec[BUY_Q] += q
                rec[NB] += 1
            if self.first_buy_ts is None and wallet not in self.devs:
                self.first_buy_ts = ts
        else:
            self.n_sells += 1
            self.sell_q += q
            role = self.role(wallet, rec)
            self.role_sold[role] += tok
            if tok > self.max_sell_tok:
                self.max_sell_tok = tok
                self.max_sell = (ts, role, tok / SUPPLY)
            if rec is None:
                self.phantom_sold += tok
            else:
                excess = tok - max(rec[NET], 0.0)
                if excess > 1.0:
                    self.phantom_sold += excess
                rec[NET] -= tok
                rec[SOLD] += tok
                rec[SELL_Q] += q
                rec[NS] += 1
        if len(self.early) < self.max_early:
            self.early.append((ts, slot, wallet, is_buy, tok, q))

        if self.price > self.max_price:
            self.max_price, self.max_ts, self.low_after_max = self.price, ts, self.price
        elif self.price < self.low_after_max:
            self.low_after_max = self.price
        self.last_trade_ts = ts
        return events

    def on_price(self, ts: float, price_q: float, volume_q: float = 0.0) -> list[tuple[Label, str]]:
        """A polled price after graduation (no trade detail), already converted to quote units."""
        self.price = price_q
        if price_q > self.max_price:
            self.max_price, self.max_ts, self.low_after_max = price_q, ts, price_q
        elif price_q < self.low_after_max:
            self.low_after_max = price_q
        self.last_trade_ts = ts
        events = self.flush(ts)
        self.bars.append([int(ts // self.bar_s), price_q, price_q, price_q, price_q, volume_q, 0.0, 0, 0, 0.0, 0])
        for lb in self.labels:
            ev = lb.update(ts, price_q)
            if ev:
                events.append((lb, ev))
        return events

    def _bar_trade(self, ts: float, is_buy: bool, tok: float, q: float) -> list[tuple[Label, str]]:
        idx = int(ts // self.bar_s)
        events: list = []
        b = self.bar
        if b is None or idx != b[B_IDX]:
            if b is not None:
                events = self._close_bar()
            b = self.bar = [idx, self.price, self.price, self.price, self.price, 0.0, 0.0, 0, 0, 0.0, 0]
        p = self.price
        if p > b[B_H]:
            b[B_H] = p
        if p < b[B_L]:
            b[B_L] = p
        b[B_C] = p
        if is_buy:
            b[B_BQ] += q
            b[B_NB] += 1
        else:
            b[B_SQ] += q
            b[B_NS] += 1
        b[B_TOK] += tok
        return events

    def _close_bar(self) -> list[tuple[Label, str]]:
        b, self.bar = self.bar, None
        if len(self.bars) < 1500:
            self.bars.append(b)
        vol_q = b[B_BQ] + b[B_SQ]
        avg = vol_q / b[B_TOK] if b[B_TOK] > 0 and vol_q > 0 else b[B_C]   # volume-weighted price paid in the bar
        ts = (b[B_IDX] + 1) * self.bar_s
        events = []
        for lb in self.labels:
            ev = lb.update(ts, avg)
            if ev:
                events.append((lb, ev))
        return events

    def flush(self, now: float) -> list[tuple[Label, str]]:
        """Close the open bar once its time has passed (a quiet token gets no next trade to do it)."""
        if self.bar is not None and now >= (self.bar[B_IDX] + 1) * self.bar_s:
            return self._close_bar()
        return []

    # ---------------------------------------------------------------- views
    def mcap_q(self) -> float:
        return self.price * SUPPLY

    def open_labels(self) -> int:
        return sum(1 for lb in self.labels if not lb.done)

    def early_buyers(self, n: int) -> list[str]:
        """First n distinct non-dev buyers, in order."""
        out, seen = [], set()
        for _, _, w, is_buy, _, _ in self.early:
            if is_buy and w not in seen and w not in self.devs:
                seen.add(w)
                out.append(w)
                if len(out) >= n:
                    break
        return out

    def final_stats(self, now: float) -> dict:
        """How it ended - kept for every snapshotted token, winners and failures alike."""
        bars = self.bars
        peak_i = max(range(len(bars)), key=lambda i: bars[i][B_H]) if bars else 0
        peak = self.max_price
        t_half = None
        crash_sell_q = crash_q = 0.0
        for b in bars[peak_i:]:
            if b[B_L] <= peak * 0.5:
                t_half = (b[B_IDX] - bars[peak_i][B_IDX]) * self.bar_s
                crash_sell_q, crash_q = b[B_SQ], b[B_SQ] + b[B_BQ]
                break
        dev_bag = sum(self.wallets[d][BOUGHT] for d in self.devs if d in self.wallets)
        early_bag = sum(r[BOUGHT] for w, r in self.wallets.items()
                        if w not in self.devs and r[FIRST_SLOT] - self.created_slot <= self.sniper_slots)
        dev_out = self.role_sold["dev"] / dev_bag if dev_bag > 0 else 0.0
        early_out = (self.role_sold["slot0"] + self.role_sold["sniper"]) / early_bag if early_bag > 0 else 0.0
        peak_mult = peak / self.p0 if self.p0 else 0.0
        if self.graduated_ts:
            kind = "graduated"
        elif peak_mult < 1.5:
            kind = "never_started"
        elif t_half is None:
            kind = "still_up"
        elif dev_out >= 0.5:
            kind = "dev_dump"
        elif early_out >= 0.5 and early_bag / SUPPLY >= 0.03:
            kind = "bundle_dump"
        elif self.max_sell and self.max_sell[2] >= 0.03 and self.max_sell[0] >= self.max_ts:
            kind = "whale_dump"
        elif t_half > 300:
            kind = "slow_bleed"
        else:
            kind = "fast_fade"
        # the price path, thinned: every bar of the first 20 minutes, then at most 60 more plus the top
        keep = list(range(min(len(bars), 80)))
        rest = range(80, len(bars))
        if rest:
            step = max(1, len(rest) // 60)
            keep += list(rest)[::step]
            if peak_i >= 80:
                keep.append(peak_i)
        path = [[max(0, int((bars[i][B_IDX] + 1) * self.bar_s - self.created_ts)), float(f"{bars[i][B_C] / self.p0:.4g}"),
                 float(f"{bars[i][B_BQ] + bars[i][B_SQ]:.3g}"), bars[i][B_NB], bars[i][B_NS]] for i in sorted(set(keep))]
        return {
            "kind": kind, "peak_mult": round(peak_mult, 3), "t_peak_s": round(self.max_ts - self.created_ts, 1),
            "t_half_s": t_half, "end_mult": round(self.price / self.p0, 3) if self.p0 else None,
            "life_s": round(self.last_trade_ts - self.created_ts, 1),
            "crash_sell_share": round(crash_sell_q / crash_q, 3) if crash_q else None,
            "max_sell": {"t_s": round(self.max_sell[0] - self.created_ts, 1), "role": self.max_sell[1],
                         "pct": round(self.max_sell[2] * 100, 3)} if self.max_sell else None,
            "dev_sold_share": round(dev_out, 3), "early_sold_share": round(early_out, 3),
            "phantom_sold_pct": round(self.phantom_sold / SUPPLY * 100, 3),
            "buys": self.n_buys, "sells": self.n_sells, "wallets": len(self.wallets),
            "path": path,                      # [seconds since launch, price multiple, volume, buys, sells]
        }


TICK_KEYS = ("priceUsd", "txns", "volume", "priceChange", "liquidity", "marketCap", "fdv", "boosts", "info", "dexId")


@dataclass
class DexToken:
    """A launch outside pump.fun: discovered through GeckoTerminal, tracked through DexScreener."""
    family = "dex"
    id: str
    chain: str
    address: str
    pool: str
    dex: str
    launchpad: str
    name: str
    symbol: str
    created_ts: float
    quote: str = ""
    ticks: list = field(default_factory=list)       # (ts, pair dict from DexScreener)
    price: float = 0.0                              # USD
    p0: float = 0.0
    max_price: float = 0.0
    max_ts: float = 0.0
    last_change_ts: float = 0.0
    labels: list = field(default_factory=list)
    next_cp: int = 0
    snapshots: int = 0
    tainted: bool = False
    called: bool = False
    saved: bool = False
    meta: dict | None = None
    seen: dict = field(default_factory=dict)
    security: dict | None = None
    trades: dict | None = None
    misses: int = 0
    path: list = field(default_factory=list)        # [seconds since launch, price multiple, 5-min volume, buys, sells]
    t_half: float | None = None                     # seconds from the top to half of it
    gm: dict | None = None                          # GMGN's token page as factors (latest)
    gm_feed: dict | None = None                     # what GMGN's launch feed said (socials, reuse)
    on_curve: bool | None = None                    # still on a bonding curve (from the launch feed)
    cx: tuple | None = None                         # (time, Codex holder picture) from the batch refresh
    gti: dict | None = None                         # GeckoTerminal token info

    def on_pair(self, ts: float, pair: dict) -> list[tuple[Label, str]]:
        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return []
        if not self.p0:
            self.p0 = price
        if price != self.price:
            self.last_change_ts = ts
        self.price = price
        if price > self.max_price:
            self.max_price, self.max_ts, self.t_half = price, ts, None
        elif self.t_half is None and price <= self.max_price * 0.5:
            self.t_half = round(ts - self.max_ts, 1)
        if self.ticks:
            self.ticks[-1][1].pop("info", None)             # the profile is only needed from the newest tick
        self.ticks.append((ts, {k: pair.get(k) for k in TICK_KEYS}))
        if len(self.ticks) > 60:
            del self.ticks[:20]
        m5 = (pair.get("txns") or {}).get("m5") or {}
        self.path.append([int(ts - self.created_ts), float(f"{price / self.p0:.4g}"),
                          (pair.get("volume") or {}).get("m5", 0), m5.get("buys", 0), m5.get("sells", 0)])
        if len(self.path) > 140:
            self.path = self.path[:40] + self.path[40::2]   # thin out: keep the whole life at lower detail
        events = []
        for lb in self.labels:
            ev = lb.update(ts, price)
            if ev:
                events.append((lb, ev))
        return events

    def open_labels(self) -> int:
        return sum(1 for lb in self.labels if not lb.done)

    def final_stats(self, now: float) -> dict:
        peak_mult = self.max_price / self.p0 if self.p0 else 0.0
        end_mult = self.price / self.p0 if self.p0 else 0.0
        kind = ("never_started" if peak_mult < 1.5 else "still_up" if self.t_half is None
                else "slow_bleed" if self.t_half > 300 else "fast_fade")
        return {"kind": kind, "peak_mult": round(peak_mult, 3), "t_peak_s": round(self.max_ts - self.created_ts, 1),
                "t_half_s": self.t_half, "end_mult": round(end_mult, 3), "path": self.path}
