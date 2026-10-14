"""The scanner: watches launches, snapshots them at fixed ages, scores, calls, labels, learns.

    pump.fun stream ──► PumpToken (trade by trade) ─┐
    GeckoTerminal ───► DexToken (DexScreener ticks) ┼─► snapshot at each checkpoint age
                                                     │      features -> gate + scorecard (+ model)
                                                     │      -> call (Telegram)       [top slice only]
                                                     └─► label: 2x before -50%?  -> database
    every N hours: retrain on the labels, promote only if better on the newest unseen data
"""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor

from . import model as learner
from .db import DB
from .features import apply_gmgn, dex_features, norm_name, pump_features, trade_features
from .http import Http
from .llm import Narrator
from .notify import Telegram, call_text, esc, message_ids, money
from .scoring import gate, prior_score
from .sources.dexscreener import SYSTEM, DexScreener, QuoteBook
from .sources.gecko import Gecko, canonical_launchpad
from .sources.gmgn import OURS, Gmgn, from_trenches
from .sources.metadata import Metadata
from .sources.pump_stream import PumpStream
from .sources.security import Security
from .state import LABEL_SQL, DexToken, Label, PumpToken
from .wallets import Reputation

log = logging.getLogger("engine")
FAMILIES = ("pump", "dex")      # where the data comes from: every trade (pump.fun) or polled snapshots
ALL = "all"                      # one scorecard, one threshold and one model cover every launchpad


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = DB(cfg.data_dir / "radar.db")
        self.http = Http()
        src = cfg.sources
        self.dexs = DexScreener(self.http, src.dexscreener.calls_per_min)
        self.quotes = QuoteBook(self.http, self.dexs, src.pump.rpc_url, self.db)
        self.gecko = Gecko(self.http, src.gecko, cfg.secret("COINGECKO_DEMO_API_KEY"), list(src.launchpad_names))
        self.security = Security(self.http, src)
        self.metadata = Metadata(src.metadata, self.db)
        self.narrator = Narrator(src.llm, cfg.secret("ANTHROPIC_API_KEY"), self.db)
        self.gmgn = Gmgn(src.gmgn, cfg.secret("GMGN_API_KEY"))
        self.telegram = Telegram(cfg.telegram, cfg.secret("TELEGRAM_BOT_TOKEN"), cfg.secret("TELEGRAM_CHAT_ID"), self.db)
        self.rep = Reputation(self.db, cfg.learn.wallets)
        self.telegram.on_command = self.command
        self.stream = PumpStream(src.pump.ws_url, src.pump.stall_s, self.on_pump_event, self.on_gap)

        self.pump: dict[str, PumpToken] = {}
        self.dex: dict[str, DexToken] = {}
        self.models: dict[str, learner.Model | None] = {ALL: None}
        self.started = self.last_tick = time.time()
        self.counts = {k: 0 for k in ("creates", "trades", "graduations", "snapshots", "gated_out", "calls",
                                      "labels", "hits", "doa", "finalized", "gaps", "dex_found")}
        self.create_ts: deque[float] = deque(maxlen=4000)
        self.grad_ts: deque[float] = deque(maxlen=2000)
        self.call_ts: deque[float] = deque(maxlen=200)
        self.priors = {ALL: deque(maxlen=60000)}                          # (ts, prior) of recent snapshots
        self.recent = {ALL: deque(maxlen=3000)}                           # (ts, hit) of recent labels
        self.tasks: set[asyncio.Task] = set()
        self.last_train: dict[str, dict] = {}
        self._load_state()

    # ------------------------------------------------------------------ startup
    def _load_state(self) -> None:
        now = time.time()
        # a previous run's unfinished labels can no longer be followed
        n = self.db.execute("UPDATE snapshots SET done=1, tainted=1, end_reason='restart', done_ts=? WHERE done=0",
                            (now,)).rowcount
        self.db.execute("UPDATE calls SET status='expired', closed_ts=? WHERE status='open'", (now,))
        if n:
            log.info("%d unfinished snapshots from the previous run set aside", n)
        r = self.db.one("SELECT path, ver FROM models WHERE family=? AND active=1 ORDER BY id DESC LIMIT 1", (ALL,))
        if r:
            self.models[ALL] = learner.Model.load(r["path"])
        for row in self.db.query("SELECT ts, prior FROM snapshots WHERE ts>? ORDER BY ts", (now - 86400,)):
            self.priors[ALL].append((row["ts"], row["prior"] or 0.0))
        for row in self.db.query("SELECT done_ts, hit FROM snapshots WHERE done=1 AND tainted=0 AND done_ts>? "
                                 "ORDER BY done_ts", (now - 6 * 3600,)):
            self.recent[ALL].append((row["done_ts"], row["hit"] or 0))
        self.last_train[ALL] = self.db.get(f"learn:{ALL}", {})
        self.db.commit()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, t: asyncio.Task) -> None:
        self.tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("background task failed", exc_info=t.exception())

    async def run(self, once_s: float | None = None) -> None:
        loops = [self.tick_loop(), self.quote_loop(), self.dex_loop(), self.train_loop(), self.daily_loop(),
                 self.profile_loop(), self.maintain_loop()]
        if self.cfg.sources.pump.enabled:
            loops.append(self.stream.run())
        if self.cfg.sources.gecko.enabled:
            loops += [self.discover_loop(net) for net in self.cfg.sources.gecko.networks]
        if self.gmgn.enabled:
            loops += [self.gmgn_feed_loop(name) for name in self.cfg.sources.gmgn.chains]
        loops.append(self.telegram.poll_commands())
        if self.cfg.dashboard.enabled:
            from .web.server import serve
            loops.append(serve(self))
        tasks = [asyncio.create_task(self._guard(c)) for c in loops]
        try:
            if once_s:
                await asyncio.sleep(once_s)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks + list(self.tasks):
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.db.commit()
            await self.http.close()
            await self.metadata.close()
            await self.telegram.close()
            await self.gmgn.close()

    async def _guard(self, coro) -> None:
        """A loop that dies takes the scanner's eyes with it: log loudly instead of failing silently."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001
            log.exception("a main loop stopped")
            raise

    # ------------------------------------------------------------------ pump.fun events
    def on_pump_event(self, kind: str, e: dict, slot: int, sig: str) -> None:
        if kind == "trade":
            tok = self.pump.get(e["mint"])
            if tok is None:
                return
            self.counts["trades"] += 1
            self._label_events(tok, tok.on_trade(e, slot))
            if not tok.meta_requested and tok.n_buys >= 6 and self.cfg.sources.metadata.enabled:
                tok.meta_requested = True
                self.spawn(self._fetch_meta(tok))
                self.spawn(self._fetch_safety(tok))         # GoPlus: a second, independent contract check
        elif kind == "create":
            self._on_create(e, slot)
        elif kind == "complete":
            tok = self.pump.get(e["mint"])
            self.grad_ts.append(time.time())
            if tok is not None and not tok.graduated_ts:
                tok.graduated_ts = float(e.get("timestamp") or time.time())
                usd = self.quotes.price(tok.quote)
                tok.grad_usd_per_q = usd if tok.q_known else None
                self.counts["graduations"] += 1

    def _on_create(self, e: dict, slot: int) -> None:
        mint = e.get("mint")
        if not mint or mint in self.pump:
            return
        q = e.get("quote_mint") or SYSTEM
        qdec = self.quotes.decimals.get(q)
        if qdec is None:
            self.quotes.pending.add(q)
        tok = PumpToken(e, slot, 6 if qdec is None else qdec, self.cfg.pump)
        tok.q_known = qdec is not None
        ts = tok.created_ts
        self.quotes.touch(q, ts)
        tok.seen = self._seen(tok, ts)
        tok.seen["cr_prev"], tok.seen["cr_day"] = self.rep.creator_launched(tok.user, ts)
        self.pump[mint] = tok
        self.create_ts.append(ts)
        self.counts["creates"] += 1

    def _seen(self, tok, ts: float) -> dict:
        s: dict = {}
        name, sym = norm_name(tok.name), norm_name(tok.symbol)
        if name:
            s["name_n"], s["name_first"], s["name_win"] = self.db.seen_bump("name", name, ts)
        if sym:
            s["sym_n"] = self.db.seen_bump("sym", sym, ts)[0]
        uri = getattr(tok, "uri", "")
        if uri:
            s["uri_n"] = self.db.seen_bump("uri", uri[:300], ts)[0]
        return s

    def on_gap(self, seconds: float) -> None:
        """The stream was down: every live pump token missed trades, so none can be trusted."""
        self.counts["gaps"] += 1
        if seconds >= 3:
            self._set_aside(self.pump.values(), f"stream gap of {seconds:.0f}s")

    def _set_aside(self, tokens, why: str) -> None:
        """Mark tokens whose history now has a hole: no more snapshots or calls, labels kept out of training."""
        n = 0
        for tok in tokens:
            if tok.tainted:
                continue
            tok.tainted = True
            for lb in tok.labels:
                if not lb.done:
                    lb.tainted = not lb.hit                 # a 2x seen before the hole still counts
                    lb.finish("gap")
            n += 1
        if n:
            log.warning("%s: %d live tokens set aside (kept out of training)", why, n)

    async def _fetch_safety(self, tok: PumpToken) -> None:
        """Contract powers of a pump.fun coin (mint / freeze authority, transfer fee or hook), the
        same check every other launchpad's tokens get."""
        r = await self.security.goplus_solana(tok.mint)
        if r:
            tok.security = {**(tok.security or {}), **r}

    async def _fetch_meta(self, tok) -> None:
        if isinstance(tok, PumpToken):
            m = await self.metadata.from_uri(tok.uri, tok.created_ts)
        else:
            m = await self.metadata.from_pair(tok.ticks[-1][1], tok.created_ts)
        tok.meta = {**(tok.meta or {}), **m}

    # ------------------------------------------------------------------ context shared by snapshots
    def _ctx(self, now: float) -> dict:
        lpm = sum(1 for t in self.create_ts if now - t <= 300) / 5.0
        rec = [h for t, h in self.recent[ALL] if now - t <= 3 * 3600]
        return {"launches_per_min": lpm, "recent_grads": sum(1 for t in self.grad_ts if now - t <= 3600),
                "recent_hit_rate": sum(rec) / len(rec) if len(rec) >= 50 else None}

    def threshold(self, now: float) -> tuple[str, float]:
        """('prob', cut) once a model is in use, else ('prior', cut) = top slice of the last 24 h."""
        m = self.models[ALL]
        c = self.cfg.call
        if m is not None:
            return "prob", max(m.thr, c.min_prob)
        vals = sorted(p for t, p in self.priors[ALL] if now - t <= 86400)
        if len(vals) < 100:
            return "prior", c.min_prior + 13.0              # too few scores to know the top slice: be strict
        cut = vals[min(len(vals) - 1, int(len(vals) * (1 - c.top_pct / 100.0)))]
        return "prior", max(c.min_prior, cut)

    # ------------------------------------------------------------------ the one-second tick
    async def tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self._tick(time.time())
            except Exception:   # noqa: BLE001 - one bad token must not stop the clock
                log.exception("tick failed")
            self.db.commit()

    def _tick(self, now: float) -> None:
        cps = self.cfg.checkpoints
        late = self.cfg.checkpoint_late_s["stream"]
        pc, lab = self.cfg.pump, self.cfg.label
        # holes must be caught here, before a silent token is mistaken for a dead one:
        # the process was frozen (host paused / asleep), or the feed has stopped talking
        pause = now - self.last_tick
        self.last_tick = now
        s = self.stream
        if pause > 20:
            self._set_aside(list(self.pump.values()) + list(self.dex.values()),
                            f"process was paused for {pause:.0f}s (host asleep, suspended or overloaded)")
        elif self.cfg.sources.pump.enabled and s.last_msg and now - s.last_msg > self.cfg.sources.pump.stall_s:
            self._set_aside(self.pump.values(), "pump.fun stream silent")
        done_rows = []
        for mint, tok in list(self.pump.items()):
            self._label_events(tok, tok.flush(now))
            age = now - tok.created_ts
            idle = now - tok.last_trade_ts
            for lb in tok.labels:
                if not lb.done:
                    lb.tick(now)
                    if not lb.done and idle > lab.idle_s:
                        lb.finish("idle")
                if lb.done and lb.snap_id is not None:
                    done_rows.append(self._label_done(tok, lb, now))
            if tok.tainted:
                if not tok.open_labels():
                    del self.pump[mint]
                continue
            while tok.next_cp < len(cps) and age >= cps[tok.next_cp]:
                cp = cps[tok.next_cp]
                tok.next_cp += 1
                if age - cp < late:                     # a late start must not fake an early snapshot
                    self._pump_snapshot(tok, cp, now)
            if tok.snapshots == 0:
                if (age >= pc.doa_age_s and tok.n_buys < pc.doa_buys) or tok.next_cp >= len(cps) \
                        or (age >= pc.doa_age_s and idle > 300):
                    self.counts["doa"] += 1
                    del self.pump[mint]
            elif not tok.open_labels() and (tok.next_cp >= len(cps) or idle > lab.idle_s):
                self._finalize(tok, now)
                del self.pump[mint]
        for tid, tok in list(self.dex.items()):
            age = now - tok.created_ts
            idle = now - (tok.last_change_ts or tok.created_ts)
            for lb in tok.labels:
                if not lb.done:
                    lb.tick(now)
                    if not lb.done and idle > 2 * lab.idle_s:
                        lb.finish("idle")
                if lb.done and lb.snap_id is not None:
                    done_rows.append(self._label_done(tok, lb, now))
            last_cp = cps[-1]
            if tok.snapshots == 0 and (age > last_cp + 120 or tok.misses > 40):
                del self.dex[tid]
            elif tok.snapshots and not tok.open_labels() and age > last_cp + 120:
                self._finalize(tok, now)
                del self.dex[tid]
        if done_rows:
            self.db.executemany(LABEL_SQL, done_rows)

    def _label_done(self, tok, lb: Label, now: float) -> tuple:
        row = lb.row(now)
        if not lb.tainted and not lb.hit and not lb.stopped:   # hits and stops were counted when they happened
            self.recent[ALL].append((now, 0))
            self.counts["labels"] += 1
        if lb.call_id is not None:
            status = "hit" if lb.hit else "stopped" if lb.stopped else "expired"
            self.db.execute("UPDATE calls SET status=?, peak_mult=?, t_hit=?, closed_ts=? WHERE id=?",
                            (status, lb.peak, lb.t_hit, now, lb.call_id))
        lb.snap_id = None                                   # written once
        return row

    def _label_events(self, tok, events: list) -> None:
        """A label reached 2x or the stop. The outcome is final from this moment, so it is stored now:
        waiting until a winner stops being followed (hours later, while losers resolve in minutes)
        would make the newest data look worse than it is - and the newest data is what models are tested on."""
        now = time.time()
        for lb, ev in events:
            if not lb.tainted:
                self.recent[ALL].append((now, int(ev == "hit")))
                self.counts["labels"] += 1
                self.counts["hits"] += int(ev == "hit")
            if ev == "hit" and lb.snap_id is not None:
                self.db.execute("UPDATE snapshots SET hit=1, t_hit=?, peak_mult=? WHERE id=?",
                                (lb.t_hit, lb.peak, lb.snap_id))
            if lb.call_id is None:
                continue
            self.db.execute("UPDATE calls SET status=?, peak_mult=?, t_hit=? WHERE id=?",
                            ("hit" if ev == "hit" else "stopped", lb.peak, lb.t_hit, lb.call_id))
            if self.cfg.telegram.outcome_updates:
                self.spawn(self._outcome_msg(tok, lb, ev))

    async def _outcome_msg(self, tok, lb: Label, ev: str) -> None:
        r = self.db.one("SELECT tg_msg_id FROM calls WHERE id=?", (lb.call_id,))
        t = lb.t_hit if ev == "hit" else lb.t_stop
        mins = f"{t / 60:.0f} min" if t and t >= 90 else f"{t or 0:.0f} s"
        if ev == "hit":
            text = f"✅ <b>{esc(tok.symbol)}</b> hit {self.cfg.label.target:g}x in {mins} after the call"
        else:
            text = f"❌ <b>{esc(tok.symbol)}</b> fell {(1 - self.cfg.label.stop) * 100:.0f}% after the call ({mins}); best was {lb.peak:.2f}x"
        await self.telegram.send(text, dedupe=f"outcome:{lb.call_id}:{ev}", kind="outcome",
                                 reply_to=message_ids(r["tg_msg_id"]) if r else None,
                                 silent=bool(self.cfg.telegram.get("silent_outcomes")))

    # ------------------------------------------------------------------ snapshots
    def _pump_snapshot(self, tok: PumpToken, cp: int, now: float) -> None:
        g = self.cfg.activity_gate
        if (tok.n_buys < g["min_buys"] or len(tok.wallets) < g["min_buyers"]
                or now - tok.last_trade_ts > g["max_idle_s"] or tok.price < tok.p0 * g["min_mcap_mult"]):
            self.counts["gated_out"] += 1
            return
        ctx = self._ctx(now)
        ctx["quote_kind"] = self.quotes.kind(tok.quote)
        ctx["quote_usd"] = self.quotes.price(tok.quote) if tok.q_known else None
        ctx["wq"] = self.rep.summarize(tok.early_buyers(self.cfg.learn.wallets.early_buyers))
        ctx["creator"] = self.rep.creator(tok.user)
        name = norm_name(tok.name)
        if name and "name_n" in tok.seen:
            tok.seen["name_after"] = max(0, self.db.seen_count("name", name) - tok.seen["name_n"] - 1)
        self._paid_flags(tok)
        f = pump_features(tok, cp, now, ctx)
        self._record(tok, cp, now, f, tok.price)

    def _paid_flags(self, tok) -> None:
        key = tok.id
        if key in self.dexs.paid or key in self.dexs.boosted:
            tok.meta = {**(tok.meta or {}), "dex_paid": int(key in self.dexs.paid),
                        "dex_boost": int(key in self.dexs.boosted)}

    async def _dex_snapshot(self, tok: DexToken, cp: int) -> None:
        """Enrich (contract and holder checks, trade list, profile), then record. Runs as its own task."""
        solana = tok.chain == "solana"
        if self.gmgn.enabled:
            # GMGN: holders, insiders, bundlers, phishing, snipers, dev, smart money and contract safety in one go
            jobs = [self._gm_fetch(tok)]
        else:
            jobs = [self.security.goplus_solana(tok.address) if solana else self.security.goplus(tok.chain, tok.address)]
            if solana:
                jobs.append(self.security.rugcheck(tok.address))   # holders, insiders, creator - refreshed per snapshot
        n_sec = len(jobs)
        want_trades = tok.trades is None and not self.gecko.busy() and tok.pool
        if want_trades:
            jobs.append(self.gecko.trades(tok.chain, tok.pool))     # first buyers: bundles and wallet reputation
        res = await asyncio.gather(*jobs, return_exceptions=True)
        if not self.gmgn.enabled:
            for r in res[:n_sec]:
                if isinstance(r, dict):
                    tok.security = {**(tok.security or {}), **r}
        elif not (tok.gm or {}).get("gm_sec_ok") and not (tok.security or {}).get("sec_checked"):
            # GMGN busy or not indexed yet: fall back to GoPlus (+ RugCheck on Solana) for this snapshot
            fb = [self.security.goplus_solana(tok.address) if solana else self.security.goplus(tok.chain, tok.address)]
            if solana:
                fb.append(self.security.rugcheck(tok.address))
            for r in await asyncio.gather(*fb, return_exceptions=True):
                if isinstance(r, dict):
                    tok.security = {**(tok.security or {}), **r}
        if want_trades and isinstance(res[-1], list) and res[-1]:
            tok.trades = trade_features(res[-1], tok.address)
        sec = {**(tok.security or {}), "_creator": (tok.gm or {}).get("_gm_creator") or (tok.security or {}).get("_creator")}
        if tok.meta is None and self.cfg.sources.metadata.enabled:
            # Solana tokens publish a metadata document like pump.fun coins do; otherwise the DexScreener profile
            if sec.get("_uri"):
                tok.meta = await self.metadata.from_uri(sec["_uri"], tok.created_ts)
            if not (tok.meta or {}).get("ok"):
                await self._fetch_meta(tok)
        creator = sec.get("_creator")
        if creator and "cr_prev" not in tok.seen:
            tok.seen["creator"] = creator
            tok.seen["cr_prev"], tok.seen["cr_day"] = self.rep.creator_launched(creator, tok.created_ts)
        now = time.time()
        if not tok.ticks or tok.tainted:
            return
        ctx = self._ctx(now)
        entries = list((tok.trades or {}).get("entries", {}))
        ctx["wq"] = self.rep.summarize(entries) if entries else None
        ctx["creator"] = self.rep.creator(tok.seen["creator"]) if tok.seen.get("creator") else None
        progress = (tok.gm or {}).get("gm_progress_pct")
        ctx["on_curve"] = (progress < 99.9) if progress is not None else (
            tok.on_curve if tok.on_curve is not None else any(d in tok.dex for d in self.cfg.sources.gecko.curve_dexes))
        self._paid_flags(tok)
        f = dex_features(tok, cp, now, ctx)
        apply_gmgn(f, tok.gm, tok.gm_feed, own_trades=False)
        self._record(tok, cp, now, f, tok.price)

    def _record(self, tok, cp: int, now: float, f: dict, price: float) -> None:
        """Store the snapshot, start its label, and decide whether it is worth a call."""
        fam = tok.family
        if isinstance(tok, PumpToken) and tok.bar is not None:
            self._label_events(tok, tok._close_bar())       # the new label must only see trades from now on
        prior, good, bad = prior_score(f)
        m = self.models[ALL]
        prob = m.predict(f) if m is not None else None
        why_not = gate(f, self.cfg, learned=m is not None)
        fkeys, packed = self.db.pack(f)
        cur = self.db.execute(
            "INSERT INTO snapshots(token_id,family,cp,ts,token_created_ts,price,mcap_usd,fkeys,features,prior,prob,"
            "model_ver,gate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tok.id, fam, cp, now, tok.created_ts, price, f.get("px_mcap_usd"), fkeys, packed, prior, prob,
             m.ver if m else None, why_not))
        snap_id = cur.lastrowid
        lb = Label(snap_id, now, price, self.cfg.label)
        tok.labels.append(lb)
        tok.snapshots += 1
        self.counts["snapshots"] += 1
        self._save_token(tok, now)
        if isinstance(tok, PumpToken) and self.gmgn.enabled:
            # the trade-stream factors are frozen at this second; GMGN's view of the token is added
            # as soon as it answers (about a second), then the call decision is made
            self.spawn(self._enrich_pump(tok, lb, snap_id, f, now))
            return
        self.priors[ALL].append((now, prior))
        self._maybe_consider(tok, lb, snap_id, f, prior, prob, why_not, now)

    def _maybe_consider(self, tok, lb: Label, snap_id: int, f: dict, prior: float, prob, why_not, now: float) -> None:
        if tok.called or why_not:
            return
        kind, cut = self.threshold(now)
        value = prob if kind == "prob" else prior
        near = cut * 0.8 if kind == "prob" else cut - self.cfg.call.enrich_margin
        if value is not None and value >= near:
            self.spawn(self._consider(tok, lb, snap_id, f, now))

    async def _gm_fetch(self, tok) -> dict | None:
        """GMGN's numbers for a token; contract safety is fetched once, the rest refreshed each time."""
        if not self.gmgn.enabled:
            return None
        address = tok.mint if isinstance(tok, PumpToken) else tok.address
        try:
            g = await asyncio.wait_for(
                self.gmgn.token(tok.chain, address, with_security=not (tok.gm or {}).get("gm_sec_ok")), 10)
        except asyncio.TimeoutError:
            g = None
        if g:
            tok.gm = {**(tok.gm or {}), **g}
        return tok.gm

    async def _enrich_pump(self, tok: PumpToken, lb: Label, snap_id: int, f: dict, t0: float) -> None:
        g = await self._gm_fetch(tok)
        apply_gmgn(f, g, tok.gm_feed, own_trades=True)
        prior, _, _ = prior_score(f)
        m = self.models[ALL]
        prob = m.predict(f) if m is not None else None
        why_not = gate(f, self.cfg, learned=m is not None)
        fkeys, packed = self.db.pack(f)
        self.db.execute("UPDATE snapshots SET fkeys=?, features=?, prior=?, prob=?, gate=? WHERE id=?",
                        (fkeys, packed, prior, prob, why_not, snap_id))
        self.priors[ALL].append((t0, prior))
        if not tok.tainted and not lb.done:
            self._maybe_consider(tok, lb, snap_id, f, prior, prob, why_not, t0)

    async def _consider(self, tok, lb: Label, snap_id: int, f: dict, t0: float) -> None:
        """Slow pre-call checks on a near-miss or better, then the final decision."""
        fam = tok.family
        jobs = []
        if tok.chain == "solana" and "rc_score" not in f:
            jobs.append(self.security.rugcheck(tok.address if fam == "dex" else tok.mint))
        if not f.get("sec_checked"):                       # never call a coin whose contract was not checked
            jobs.append(self._safety_retry(tok, f))
        if self.narrator.enabled and "llm" not in (tok.meta or {}):
            jobs.append(self._rate(tok))
        if jobs:
            res = await asyncio.gather(*jobs, return_exceptions=True)
            for r in res:
                if isinstance(r, dict):
                    f.update(r)
        for k, v in ((tok.meta or {}).get("llm") or {}).items():
            f[("_llm" if k.startswith("_") else "llm_") + k] = v
        prior, good, bad = prior_score(f)
        m = self.models[ALL]
        prob = m.predict(f) if m is not None else None
        why_not = gate(f, self.cfg, learned=m is not None, final=True)
        now = time.time()
        kind, cut = self.threshold(now)
        value = prob if kind == "prob" else prior
        hour_calls = sum(1 for t in self.call_ts if now - t <= 3600)
        moved = abs(tok.price / lb.entry - 1) if lb.entry else 0
        ok = (not why_not and not tok.called and not tok.tainted and not lb.done and value is not None
              and value >= cut and hour_calls < self.cfg.call.max_per_hour and moved < 0.25 and now - t0 < 30)
        fkeys, packed = self.db.pack(f)
        self.db.execute("UPDATE snapshots SET fkeys=?, features=?, prior=?, prob=?, gate=?, called=? WHERE id=?",
                        (fkeys, packed, prior, prob, why_not, int(ok), snap_id))
        if not ok:
            return
        tok.called = True
        self.call_ts.append(now)
        self.counts["calls"] += 1
        cur = self.db.execute(
            "INSERT INTO calls(snapshot_id,token_id,ts,price,mcap_usd,prior,prob,model_ver,reasons) VALUES(?,?,?,?,?,?,?,?,?)",
            (snap_id, tok.id, now, lb.entry, f.get("px_mcap_usd"), prior, prob, m.ver if m else None,
             json.dumps({"good": good, "bad": bad})))
        lb.call_id = cur.lastrowid
        text = call_text(tok, f, prior, prob, m.ver if m else None, good, bad, self._call_hit_rate(), self.cfg.label)
        ids = await self.telegram.send(text, dedupe=f"call:{tok.id}", kind="call")
        if ids:
            self.db.execute("UPDATE calls SET tg_msg_id=? WHERE id=?", (json.dumps(ids), lb.call_id))
        log.info("CALL %s %s (%s) prior %.0f prob %s mcap %s", tok.launchpad, tok.symbol, tok.id, prior,
                 f"{prob:.2f}" if prob is not None else "-", money(f.get("px_mcap_usd")))

    async def _safety_retry(self, tok, f: dict) -> None:
        """One more try at the contract check before a call: GMGN first, then GoPlus."""
        if self.gmgn.enabled:
            apply_gmgn(f, await self._gm_fetch(tok), tok.gm_feed, own_trades=isinstance(tok, PumpToken))
        if not f.get("sec_checked"):
            if isinstance(tok, PumpToken):
                await self._fetch_safety(tok)
                f.update({k: v for k, v in (tok.security or {}).items() if not k.startswith("_")})
            else:
                r = await (self.security.goplus_solana(tok.address) if tok.chain == "solana"
                           else self.security.goplus(tok.chain, tok.address))
                if r:
                    f.update({k: v for k, v in r.items() if not k.startswith("_")})

    async def _rate(self, tok) -> dict | None:
        r = await self.narrator.rate(tok)
        if r:
            tok.meta = {**(tok.meta or {}), "llm": r}
        return None

    def _call_hit_rate(self) -> float | None:
        r = self.db.one("SELECT COUNT(*) n, SUM(status='hit') h FROM calls WHERE status IN ('hit','stopped','expired')")
        return r["h"] / r["n"] if r and r["n"] >= 15 else None

    # ------------------------------------------------------------------ persistence of tokens
    def _save_token(self, tok, now: float) -> None:
        if tok.saved:
            return
        tok.saved = True
        pump = isinstance(tok, PumpToken)
        self.db.execute(
            "INSERT OR REPLACE INTO tokens(id,family,chain,launchpad,address,pool,name,symbol,uri,creator,quote,"
            "created_ts,created_slot,meta,status,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tok.id, tok.family, tok.chain, tok.launchpad, tok.mint if pump else tok.address,
             "" if pump else tok.pool, tok.name[:80], tok.symbol[:40], tok.uri[:300] if pump else "",
             tok.user if pump else (tok.security or {}).get("_creator", ""), tok.quote,
             tok.created_ts, tok.created_slot if pump else None, _dump(tok.meta or {}), "live", now))

    def _finalize(self, tok, now: float) -> None:
        """All labels are in: store how it ended, credit wallets and creator."""
        final = tok.final_stats(now)
        usd = None
        if isinstance(tok, PumpToken):
            usd = self.quotes.price(tok.quote, max_age=3600) if tok.q_known else None
            status = "graduated" if tok.graduated_ts else "dead" if final["kind"] != "still_up" else "live"
            peak_usd = tok.max_price * 1e9 * usd if usd else None
            last_usd = tok.price * 1e9 * usd if usd else None
        else:
            status = "dead" if final["kind"] != "still_up" else "live"
            peak_usd = last_usd = None
        self.db.execute("UPDATE tokens SET status=?, graduated_ts=?, peak_mcap_usd=?, last_mcap_usd=?, final=?, meta=?, "
                        "updated_ts=? WHERE id=?",
                        (status, getattr(tok, "graduated_ts", None), peak_usd, last_usd, _dump(final),
                         _dump(tok.meta or {}), now, tok.id))
        self.rep.credit(tok, final, usd)
        self.counts["finalized"] += 1

    # ------------------------------------------------------------------ other launchpads
    async def gmgn_feed_loop(self, name: str) -> None:
        """New launches on every launchpad of one chain from GMGN, seconds after creation. For pump.fun
        (read from the chain itself) the feed only adds socials: X followers, TG calls, reused links."""
        g = self.cfg.sources.gmgn
        c = g.chains[name]
        chain = c.get("chain", name)
        ours = OURS.get(chain, chain)
        socials_only = bool(c.get("socials_only"))
        while True:
            try:
                items = await self.gmgn.trenches(chain, list(c.get("platforms") or []), g.feed_limit)
                now = time.time()
                for it in items:
                    addr = it["address"]
                    if not str(addr).isalnum():
                        continue                            # addresses end up in links: accept only plain ones
                    feed = from_trenches(it)
                    if ours == "solana" and addr in self.pump:
                        self.pump[addr].gm_feed = feed
                        continue
                    if socials_only:
                        continue
                    tid = f"{ours}:{addr}"
                    if tid in self.dex:
                        self.dex[tid].gm_feed = feed
                        continue
                    created = float(it.get("created_timestamp") or now)
                    if now - created > g.max_age_s or self.db.one("SELECT 1 FROM tokens WHERE id=?", (tid,)):
                        continue
                    progress = it.get("progress")
                    tok = DexToken(id=tid, chain=ours, address=addr, pool=str(it.get("pool_address") or ""),
                                   dex=str(it.get("exchange") or it.get("launchpad_platform") or ""),
                                   launchpad=self._pad(str(it.get("launchpad_platform") or it.get("launchpad") or "?")),
                                   name=str(it.get("name") or "")[:80], symbol=str(it.get("symbol") or "")[:40],
                                   created_ts=created)
                    tok.gm_feed = feed
                    tok.on_curve = None if progress is None else float(progress) < 0.999
                    tok.seen = self._seen(tok, created)
                    self.dex[tid] = tok
                    self.counts["dex_found"] += 1
            except Exception:   # noqa: BLE001
                log.exception("GMGN feed failed for %s", name)
            await asyncio.sleep(c["poll_s"])

    def _pad(self, raw: str) -> str:
        return canonical_launchpad(raw, list(self.cfg.sources.launchpad_names)) or raw[:40].lower()

    async def command(self, cmd: str) -> str:
        """Telegram commands (when enabled)."""
        if cmd in ("/status", "/start"):
            h = self.health()
            kind, cut = h["thresholds"][ALL]
            return (f"🛰 Launch Radar up {h['uptime_s'] / 3600:.1f} h · pump.fun stream "
                    f"{'live' if h['stream']['connected'] else 'DOWN'}\n"
                    f"Watching {h['live']['pump']} pump.fun + {h['live']['dex']} other launches · "
                    f"{h['counts']['snapshots']} snapshots, {h['counts']['calls']} calls since start\n"
                    f"Calls need {'a ' + format(cut * 100, '.0f') + '% model chance' if kind == 'prob' else 'scorecard ' + format(cut, '.0f')}"
                    f" · GMGN {'on' if self.gmgn.enabled else 'off'}")
        if cmd == "/calls":
            rows = self.db.query("SELECT c.ts, c.status, c.peak_mult, t.symbol, t.launchpad FROM calls c "
                                 "LEFT JOIN tokens t ON t.id=c.token_id ORDER BY c.ts DESC LIMIT 8")
            if not rows:
                return "No calls yet."
            icon = {"hit": "✅", "stopped": "❌", "open": "⏳", "expired": "⌛"}
            return "Last calls:\n" + "\n".join(
                f"{icon.get(r['status'], '•')} {esc(r['symbol'] or '?')} ({esc(r['launchpad'] or '?')}) "
                f"{(time.time() - r['ts']) / 3600:.1f} h ago, best {r['peak_mult'] or 0:.2f}x" for r in rows)
        if cmd == "/stats":
            return self.summary_text()
        return "/status - is it running\n/calls - the last calls and how they did\n/stats - the last 24 hours"

    async def discover_loop(self, network: str) -> None:
        net = self.cfg.sources.gecko.networks[network]
        poll = net.get("fast_poll_s", net["poll_s"]) if self.gecko.headers else net["poll_s"]
        while True:
            try:
                for p in await self.gecko.new_pools(network):
                    tid = f"{p['chain']}:{p['address']}"
                    if tid in self.dex or p["address"] in self.pump:
                        continue
                    if p["chain"] == "solana" and p["address"].endswith("pump"):
                        continue                            # a pump.fun coin getting another pool is not a launch
                    if self.db.one("SELECT 1 FROM tokens WHERE id=?", (tid,)):
                        continue
                    tok = DexToken(id=tid, chain=p["chain"], address=p["address"], pool=p["pool"], dex=p["dex"],
                                   launchpad=p["launchpad"], name=p["name"], symbol=p["symbol"],
                                   created_ts=p["created_ts"], quote=p["quote"])
                    tok.seen = self._seen(tok, p["created_ts"])
                    self.dex[tid] = tok
                    self.counts["dex_found"] += 1
            except Exception:   # noqa: BLE001
                log.exception("discovery failed for %s", network)
            await asyncio.sleep(poll)

    async def dex_loop(self) -> None:
        """Price / volume ticks for non-pump tokens and for graduated pump coins; their checkpoints.
        Young tokens are polled every `poll_s` so the 1- and 3-minute snapshots land on time."""
        cps, late = self.cfg.checkpoints, self.cfg.checkpoint_late_s["poll"]
        g = self.cfg.activity_gate
        ds = self.cfg.sources.dexscreener
        cycle = 0
        while True:
            await asyncio.sleep(ds.poll_s)
            cycle += 1
            every_old = max(1, round(ds.old_poll_s / ds.poll_s))
            try:
                now = time.time()
                by_chain: dict[str, list[DexToken]] = {}
                for tok in self.dex.values():
                    if now - tok.created_ts < 900 or cycle % every_old == 0:
                        by_chain.setdefault(tok.chain, []).append(tok)
                for chain, toks in by_chain.items():
                    pairs = await self.dexs.pairs(chain, [t.address for t in toks])
                    now = time.time()
                    for tok in toks:
                        p = pairs.get(tok.address)
                        if p is None:
                            tok.misses += 1
                            continue
                        born = (p.get("pairCreatedAt") or 0) / 1000
                        if not tok.ticks and born and tok.created_ts - born > 3600:
                            tok.misses = 999                # an old token with a new pool: not a launch
                            continue
                        self._label_events(tok, tok.on_pair(now, p))
                        age = now - tok.created_ts
                        while not tok.tainted and tok.next_cp < len(cps) and age >= cps[tok.next_cp]:
                            cp = cps[tok.next_cp]
                            tok.next_cp += 1
                            buys = ((p.get("txns") or {}).get("h1") or {}).get("buys", 0)
                            liq = (p.get("liquidity") or {}).get("usd")
                            on_curve = any(d in tok.dex for d in self.cfg.sources.gecko.curve_dexes)
                            # the same activity test as pump.fun coins, with what a snapshot shows
                            if (age - cp < late and buys >= g["min_buys"]
                                    and now - (tok.last_change_ts or tok.created_ts) <= g["max_idle_s"]
                                    and (on_curve or liq is None or liq >= g["min_liquidity_usd"])):
                                self.spawn(self._dex_snapshot(tok, cp))
                            else:
                                self.counts["gated_out"] += 1
                grads = [t for t in self.pump.values() if t.graduated_ts and t.grad_usd_per_q and t.open_labels()]
                if grads:
                    pairs = await self.dexs.pairs("solana", [t.mint for t in grads])
                    now = time.time()
                    for tok in grads:
                        p = pairs.get(tok.mint)
                        try:
                            usd = float(p["priceUsd"]) if p else 0.0
                        except (KeyError, TypeError, ValueError):
                            usd = 0.0
                        if usd > 0 and (p.get("dexId") != "pumpfun"):       # the curve's own pair is frozen
                            self._label_events(tok, tok.on_price(now, usd / tok.grad_usd_per_q))
            except Exception:   # noqa: BLE001
                log.exception("dex poll failed")

    async def quote_loop(self) -> None:
        while True:
            try:
                await self.quotes.refresh()
            except Exception:   # noqa: BLE001
                log.exception("quote refresh failed")
            await asyncio.sleep(30)

    async def profile_loop(self) -> None:
        while True:
            try:
                await self.dexs.poll_profiles()
            except Exception:   # noqa: BLE001
                log.exception("profile poll failed")
            await asyncio.sleep(60)

    # ------------------------------------------------------------------ learning
    async def train_loop(self) -> None:
        lc = self.cfg.learn
        await asyncio.sleep(120)
        while True:
            for fam in (ALL,):
                last = self.last_train.get(fam, {})
                collecting = "collecting" in (last.get("note") or "")
                if time.time() - last.get("ts", 0) < (1 if collecting else lc.retrain_hours) * 3600:
                    continue                                # while history is still building, look again hourly
                await self.retrain(fam)
            await asyncio.sleep(600)

    async def retrain(self, fam: str) -> dict:
        self.db.commit()
        r = self.db.one("SELECT path FROM models WHERE family=? AND active=1 ORDER BY id DESC LIMIT 1", (fam,))
        args = (self.db.path, fam, dict(self.cfg.learn), dict(self.cfg.call), self.cfg.data_dir / "models",
                r["path"] if r else None)
        # a separate process: training's memory is returned when it ends, and it never competes with
        # the live feed for the interpreter
        pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        try:
            report = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(pool, learner.train, *args),
                                            timeout=1800)
        except (asyncio.TimeoutError, Exception) as e:   # noqa: BLE001 - a failed retrain keeps what is in use
            for p in list(getattr(pool, "_processes", {}).values()):
                p.kill()
            log.error("retrain %s failed: %s", fam, e or type(e).__name__)
            report = {"family": fam, "ts": time.time(), "promoted": False,
                      "note": f"training failed ({type(e).__name__}) - keeping what is in use"}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        lift = report.pop("lift", None)
        if lift is not None:
            self.db.put(f"lift:{fam}", lift)
        self.db.put(f"learn:{fam}", report)
        self.last_train[fam] = report
        if report.get("promoted"):
            self.db.execute("UPDATE models SET active=0 WHERE family=?", (fam,))
            self.db.execute("INSERT INTO models(family,ver,ts,n_rows,n_pos,metrics,path,active) VALUES(?,?,?,?,?,?,?,1)",
                            (fam, report["ver"], report["ts"], report["n_rows"], report["n_pos"],
                             json.dumps(report), report["path"]))
            self.models[fam] = learner.Model.load(report["path"])
            t, thr = report["test"], report["threshold"]
            await self.telegram.send(
                f"🧠 <b>New model in use</b> ({esc(report['ver'])}) - all launchpads\n"
                f"Trained on {report['n_rows']:,} labelled snapshots ({report['n_pos']:,} winners).\n"
                f"On the newest unseen {t['n']:,}: top 2% picks hit {_pc(t['model'].get('p_top2'))} "
                f"vs scorecard {_pc(t['scorecard'].get('p_top2'))} (base rate {_pc(t['base_rate'])}).\n"
                f"Calls now need ≥ {thr['thr'] * 100:.0f}% — expected hit rate {_pc(thr.get('expected_precision'))}.",
                dedupe=f"model:{report['ver']}", kind="model")
        self.db.commit()
        log.info("retrain %s: %s", fam, report.get("note") or report.get("verdict") or "ok")
        return report

    # ------------------------------------------------------------------ housekeeping
    async def daily_loop(self) -> None:
        when = str(self.cfg.telegram.get("daily_summary_utc") or "off")
        if ":" not in when:
            return                                          # "off"
        hh, mm = (int(x) for x in when.split(":"))
        while True:
            now = time.time()
            t = time.gmtime(now)
            target = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) + hh * 3600 + mm * 60
            if target <= now:
                target += 86400
            await asyncio.sleep(target - now)
            try:
                await self.telegram.send(self.summary_text(), dedupe=time.strftime("daily:%Y-%m-%d", time.gmtime()),
                                         kind="daily")
            except Exception:   # noqa: BLE001
                log.exception("daily summary failed")

    async def maintain_loop(self) -> None:
        await asyncio.sleep(300)
        while True:
            try:
                await self.maintain()
            except Exception:   # noqa: BLE001
                log.exception("maintenance failed")
            await asyncio.sleep(3600)

    async def maintain(self, now: float | None = None) -> dict:
        """Hourly: drop what no longer helps the learner, then hold the disk budget. Deletes run in
        small batches with pauses between them, so the live feed is never held up."""
        now = now or time.time()
        d, keep = 86400.0, self.cfg.storage.retention_days
        rules = (
            ("snapshots", "called = 0 AND ts < ?", (now - keep["snapshots"] * d,)),
            ("snapshots", "called = 1 AND ts < ?", (now - keep["calls"] * d,)),   # kept as long as their call
            ("snapshots", "tainted = 1 AND called = 0 AND ts < ?", (now - 2 * d,)),   # never trained on
            ("tokens", "created_ts < ? AND id NOT IN (SELECT token_id FROM calls)", (now - keep["tokens"] * d,)),
            ("calls", "ts < ?", (now - keep["calls"] * d,)),
            ("wallets", "(n_tok < 4 AND last_ts < ?) OR last_ts < ?", (now - keep["wallets"] * d, now - keep["stale"] * d)),
            ("creators", "(n_snap = 0 AND last_ts < ?) OR last_ts < ?", (now - keep["seen"] * d, now - keep["stale"] * d)),
            ("seen", "last_ts < ?", (now - keep["seen"] * d,)),
            ("alerts", "ts < ?", (now - 30 * d,)),
        )
        out: dict = {}
        for table, where, args in rules:
            while True:
                n = self.db.delete_batch(table, where, args)
                out[table] = out.get(table, 0) + n
                if n < 2000:
                    break
                await asyncio.sleep(0.05)
        out["model_files"] = self._prune_model_files()
        # the hard cap: if the database still outgrows the budget, the oldest history goes first
        budget = self.cfg.storage.max_gb * 1e9
        while self._storage_bytes() > 0.9 * budget:
            oldest = self.db.one("SELECT MIN(ts) t FROM snapshots WHERE called = 0")["t"]
            if oldest is None:
                break
            cut = oldest + max(86400.0, (now - oldest) * 0.1)            # the oldest tenth (at least a day)
            n = 0
            while (k := self.db.delete_batch("snapshots", "called = 0 AND ts < ?", (cut,))) > 0:
                n += k
                await asyncio.sleep(0.05)
            while self.db.delete_batch("tokens", "created_ts < ? AND id NOT IN (SELECT token_id FROM calls)", (cut,)):
                await asyncio.sleep(0.05)
            self.db.reclaim(200000)
            out["over_budget_snapshots"] = out.get("over_budget_snapshots", 0) + n
            log.warning("storage over %.0f%% of the %s GB budget: dropped %d oldest snapshots",
                        90, self.cfg.storage.max_gb, n)
            if n == 0:
                break
        self.db.reclaim()
        out["db_gb"] = round(self.db.size_bytes() / 1e9, 3)
        log.info("maintenance: %s", out)
        return out

    def _storage_bytes(self) -> int:
        models = self.cfg.data_dir / "models"
        return self.db.size_bytes() + (sum(p.stat().st_size for p in models.glob("*.pkl")) if models.exists() else 0)

    def _prune_model_files(self, keep: int = 4) -> int:
        """Keep the models in use plus the newest few; older files are only history."""
        active = {r["path"] for r in self.db.query("SELECT path FROM models WHERE active=1")}
        files = sorted((self.cfg.data_dir / "models").glob("*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
        n = 0
        for p in files[keep:]:
            if str(p) not in active:
                p.unlink(missing_ok=True)
                n += 1
        return n

    def summary_text(self) -> str:
        since = time.time() - 86400
        c = self.db.one("SELECT COUNT(*) n, SUM(status='hit') h, SUM(status='stopped') s, SUM(status='open') o, "
                        "AVG(peak_mult) pk FROM calls WHERE ts>?", (since,))
        b = self.db.one("SELECT COUNT(*) n, AVG(hit) r FROM snapshots WHERE (done=1 OR hit=1) AND tainted=0 AND ts>?",
                        (since,))
        closed = (c["h"] or 0) + (c["s"] or 0)
        rate = f"{(c['h'] or 0) / closed * 100:.0f}%" if closed else "–"
        m = self.models[ALL]
        decider = f"model {m.ver}" if m else f"scorecard only ({self.last_train.get(ALL, {}).get('note') or 'collecting'})"
        lines = ["📊 <b>Launch Radar — last 24 h</b>",
                 f"{c['n']} calls → {c['h'] or 0} hit 2x, {c['s'] or 0} stopped, {c['o'] or 0} open "
                 f"(hit rate {rate}, avg best {c['pk'] or 0:.2f}x)",
                 f"All {b['n']:,} snapshots: {_pc(b['r'])} would have 2x'd — {decider}"]
        for pad in self.db.query("SELECT t.launchpad, COUNT(*) n, SUM(c.status='hit') h FROM calls c "
                                 "LEFT JOIN tokens t ON t.id=c.token_id WHERE c.ts>? GROUP BY 1 ORDER BY 2 DESC LIMIT 6",
                                 (since,)):
            lines.append(f"   {esc(pad['launchpad'] or '?')}: {pad['n']} calls, {pad['h'] or 0} hit")
        return "\n".join(lines)

    def health(self) -> dict:
        now = time.time()
        s = self.stream
        return {
            "uptime_s": now - self.started, "counts": self.counts,
            "live": {"pump": len(self.pump), "dex": len(self.dex),
                     "open_labels": sum(t.open_labels() for t in self.pump.values()) + sum(
                         t.open_labels() for t in self.dex.values())},
            "stream": {"connected": s.connected, "last_msg_age_s": now - s.last_msg if s.last_msg else None,
                       "msgs": s.msgs, "events": s.events, "reconnects": s.reconnects, "mb": round(s.bytes / 1e6, 1)},
            "apis": {**({"gmgn": {"calls": self.gmgn.calls, "errors": self.gmgn.errors, "rate_limited": self.gmgn.limited,
                                  "backlog_s": round(self.gmgn.backlog_s(), 1)}} if self.gmgn.enabled else {}),
                     **{k: {"calls": v.calls, "errors": v.errors, "rate_limited": v.limited,
                            "backlog_s": round(v.backlog_s(), 1)} for k, v in self.http.limiters.items()}},
            "gmgn": self.gmgn.enabled,
            "thresholds": {ALL: self.threshold(now)},
            "models": {ALL: (self.models[ALL].ver if self.models[ALL] else None)},
            "sol_usd": self.quotes.price(SYSTEM), "metadata": {"ok": self.metadata.fetched, "failed": self.metadata.failed},
            "llm": {"enabled": self.narrator.enabled, "used_today": self.narrator.used},
            "telegram": self.telegram.enabled,
        }


def _dump(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"), default=_json_default)


def _json_default(o):
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


def _pc(x) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"
