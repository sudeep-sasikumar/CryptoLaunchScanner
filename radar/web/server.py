"""Dashboard: calls, scoreboard, what the learner found, how failures die, system health.

Bound to localhost: no login. Bound to anything else (the VPS): DASHBOARD_PASSWORD is required
and the dashboard refuses to start without it. Read-only - nothing here changes the scanner.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"
LOCAL = {"127.0.0.1", "localhost", "::1"}
COOKIE = "radar_session"


def _secret(data_dir: Path) -> bytes:
    p = data_dir / "session_secret"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
    return p.read_bytes()


def _token(secret: bytes, days: float) -> str:
    exp = str(int(time.time() + days * 86400))
    return f"{exp}.{hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()}"


def _valid(secret: bytes, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def call_state(status: str, following: bool) -> str:
    """What a call's outcome means, in plain words:
    live      - not yet 2x, not yet dumped: still undecided
    running   - reached 2x and is still being followed (it may keep going)
    ran       - reached 2x; following has ended (see the best multiple)
    dumped    - fell 50% below the call price before reaching 2x
    fizzled   - neither: went quiet or the 6-hour window ended"""
    if status == "hit":
        return "running" if following else "ran"
    if status == "stopped":
        return "dumped"
    if status == "open":
        return "live" if following else "fizzled"
    return "fizzled"


def build_app(engine) -> FastAPI:
    cfg, db = engine.cfg, engine.db
    d = cfg.dashboard
    need_login = d.bind_host not in LOCAL
    password = cfg.secret("DASHBOARD_PASSWORD")
    secret = _secret(cfg.data_dir)
    fails: dict[str, list[float]] = {}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if need_login and path.startswith("/api/") and path != "/api/login":
            if not _valid(secret, request.cookies.get(COOKIE)):
                return JSONResponse({"error": "login"}, status_code=401)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.post("/api/login")
    async def login(request: Request):
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 900]
        if len(recent) >= 8:
            raise HTTPException(429, "too many attempts - wait 15 minutes")
        try:
            given = str((await request.json()).get("password", ""))
        except (ValueError, AttributeError):
            given = ""
        if not (password and hmac.compare_digest(given.encode(), password.encode())):
            fails[ip] = recent + [now]
            raise HTTPException(401, "wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _token(secret, d.session_days), max_age=int(d.session_days * 86400), httponly=True,
                        samesite="strict", secure=bool(d.cookie_secure))
        return resp

    @app.get("/api/health")
    async def health():
        h = engine.health()
        h["login"] = need_login
        h["train"] = {f: {k: v for k, v in (engine.last_train.get(f) or {}).items() if k != "importance"}
                      for f in engine.models}
        return h

    @app.get("/api/calls")
    async def calls(limit: int = 100):
        rows = db.query(
            "SELECT c.id, c.ts, c.token_id, c.mcap_usd, c.prior, c.prob, c.model_ver, c.status, c.peak_mult, c.t_hit, c.tier, "
            "c.reasons, t.name, t.symbol, t.launchpad, t.chain, t.address, s.cp, s.end_mult, s.family "
            "FROM calls c JOIN snapshots s ON s.id=c.snapshot_id LEFT JOIN tokens t ON t.id=c.token_id "
            "ORDER BY c.ts DESC LIMIT ?", (min(limit, 500),))
        out = []
        for r in rows:
            x = dict(r)
            x["reasons"] = json.loads(x["reasons"] or "{}")
            live = engine.pump.get(x["address"]) or engine.dex.get(x["token_id"])
            lb = next((lb for lb in live.labels if lb.call_id == x["id"]), None) if live else None
            if lb:
                x["peak_mult"], x["now_mult"] = lb.peak, lb.last
                x["now_mcap_usd"] = engine.current_mcap(live)
            else:                                       # no longer followed: where it was when tracking ended
                x["now_mult"] = x.get("end_mult")
                x["now_mcap_usd"] = x["mcap_usd"] * x["end_mult"] if x.get("mcap_usd") and x.get("end_mult") else None
            x["following"] = lb is not None and not lb.done
            x["state"] = call_state(x["status"], following=lb is not None and not lb.done)
            out.append(x)
        return out

    @app.get("/api/candidates")
    async def candidates():
        """Best recent snapshots, called or not - what the scanner is looking at right now."""
        rows = db.query(
            "SELECT s.id, s.ts, s.token_id, s.cp, s.prior, s.prob, s.gate, s.called, s.mcap_usd, s.done, s.hit, "
            "s.peak_mult, s.family, t.name, t.symbol, t.launchpad, t.chain, t.address FROM snapshots s "
            "LEFT JOIN tokens t ON t.id=s.token_id WHERE s.ts > ? ORDER BY COALESCE(s.prob*100, s.prior) DESC LIMIT 60",
            (time.time() - 1800,))
        return [dict(r) for r in rows]

    @app.get("/api/scoreboard")
    async def scoreboard(days: int = 14):
        since = time.time() - min(days, 120) * 86400
        closed = "c.status IN ('hit','stopped','expired')"
        by_day = db.query(
            f"SELECT date(c.ts,'unixepoch') day, s.family, COUNT(*) n, SUM(c.status='hit') hit, "
            f"SUM(c.status='stopped') stopped, SUM(c.status='open') open, AVG(c.peak_mult) avg_peak "
            f"FROM calls c JOIN snapshots s ON s.id=c.snapshot_id WHERE c.ts>? GROUP BY day, s.family ORDER BY day DESC",
            (since,))
        by_pad = db.query(
            f"SELECT t.launchpad, COUNT(*) n, SUM(c.status='hit') hit, AVG(c.peak_mult) avg_peak FROM calls c "
            f"LEFT JOIN tokens t ON t.id=c.token_id WHERE c.ts>? AND {closed} GROUP BY t.launchpad ORDER BY n DESC",
            (since,))
        base = db.query(
            "SELECT family, cp, COUNT(*) n, AVG(hit) rate, AVG(peak_mult) avg_peak FROM snapshots "
            "WHERE (done=1 OR hit=1) AND tainted=0 AND ts>? GROUP BY family, cp ORDER BY family, cp", (since,))
        buckets = db.query(
            "SELECT family, CAST(prior/10 AS INT)*10 bucket, COUNT(*) n, AVG(hit) rate FROM snapshots "
            "WHERE (done=1 OR hit=1) AND tainted=0 AND ts>? GROUP BY family, bucket ORDER BY family, bucket", (since,))
        prob_buckets = db.query(
            "SELECT family, CAST(prob*10 AS INT)*10 bucket, COUNT(*) n, AVG(hit) rate FROM snapshots "
            "WHERE (done=1 OR hit=1) AND tainted=0 AND prob IS NOT NULL AND ts>? GROUP BY family, bucket ORDER BY family, bucket",
            (since,))
        total = db.one(f"SELECT COUNT(*) n, SUM(status='hit') hit, SUM(status='stopped') stopped, "
                       f"SUM(status='open') open, AVG(peak_mult) avg_peak FROM calls c WHERE ts>?", (since,))
        return {"total": dict(total), "by_day": [dict(r) for r in by_day], "by_launchpad": [dict(r) for r in by_pad],
                "base_rates": [dict(r) for r in base], "score_buckets": [dict(r) for r in buckets],
                "prob_buckets": [dict(r) for r in prob_buckets]}

    @app.get("/api/learning")
    async def learning():
        models = db.query("SELECT family, ver, ts, n_rows, n_pos, active, metrics FROM models ORDER BY id DESC LIMIT 30")
        out = {"models": [], "lift": {}, "last": engine.last_train, "wallets": {}}
        for r in models:
            x = dict(r)
            m = json.loads(x.pop("metrics") or "{}")
            x["test"], x["threshold"], x["importance"] = m.get("test"), m.get("threshold"), m.get("importance")
            out["models"].append(x)
        for fam in engine.models:
            out["lift"][fam] = (db.get(f"lift:{fam}") or [])[:41]
        w = db.one("SELECT COUNT(*) n, SUM(n_tok>=?) seasoned FROM wallets", (cfg.learn.wallets.min_tokens,))
        out["wallets"] = {"tracked": w["n"], "seasoned": w["seasoned"] or 0, "base": engine.rep.base,
                          "creators": db.one("SELECT COUNT(*) n FROM creators")["n"]}
        top = db.query("SELECT addr, n_tok, n_win, n_dead, n_slot0, n_dump, pnl FROM wallets WHERE n_tok>=? "
                       "ORDER BY (n_win*1.0+1)/(n_tok+8) DESC LIMIT 400", (cfg.learn.wallets.min_tokens,))
        smart = []
        for r in top:
            lift, is_smart, _, _ = engine.rep.judge(r)
            if is_smart:
                smart.append({**dict(r), "lift": round(lift, 2)})
        out["wallets"]["smart"] = smart[:25]
        return out

    @app.get("/api/failures")
    async def failures():
        """How launches end: what kills them, how fast."""
        rows = db.query("SELECT family, launchpad, final FROM tokens WHERE final IS NOT NULL ORDER BY updated_ts DESC "
                        "LIMIT 4000")
        kinds: dict[str, dict] = {}
        for r in rows:
            try:
                f = json.loads(r["final"])
            except ValueError:
                continue
            k = kinds.setdefault(f"{r['family']}|{f.get('kind')}", {"family": r["family"], "kind": f.get("kind"),
                                                                    "n": 0, "peak": [], "t_peak": [], "t_half": [],
                                                                    "dev_sold": [], "crash_sell": []})
            k["n"] += 1
            for key, src in (("peak", "peak_mult"), ("t_peak", "t_peak_s"), ("t_half", "t_half_s"),
                             ("dev_sold", "dev_sold_share"), ("crash_sell", "crash_sell_share")):
                if f.get(src) is not None:
                    k[key].append(f[src])
        out = []
        for k in kinds.values():
            for key in ("peak", "t_peak", "t_half", "dev_sold", "crash_sell"):
                v = sorted(k[key])
                k[key] = v[len(v) // 2] if v else None
            out.append(k)
        out.sort(key=lambda k: (k["family"], -k["n"]))
        return out

    @app.get("/api/research")
    async def research_list():
        """Days of research data on this server (tools/sync_research.py copies them to the PC)."""
        import asyncio
        from radar.export import list_days
        db.commit()
        return {"days": await asyncio.to_thread(list_days, db.path), "server_time": time.time()}

    @app.get("/api/research/file/{name}")
    async def research_file(name: str):
        """One day's finished snapshots, calls and token endings, built from the database on request."""
        import asyncio
        from radar.export import NAME, build_day
        m = NAME.fullmatch(name)
        if not m:
            raise HTTPException(404, "no such file")
        db.commit()
        data = await asyncio.to_thread(build_day, db.path, m.group(1))
        return Response(data, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.get("/api/token")
    async def token(id: str):
        t = db.one("SELECT * FROM tokens WHERE id=?", (id,))
        if not t:
            raise HTTPException(404, "unknown token")
        x = dict(t)
        x["meta"], x["final"] = json.loads(x["meta"] or "{}"), json.loads(x["final"] or "null")
        snaps = []
        for s in db.query("SELECT * FROM snapshots WHERE token_id=? ORDER BY ts", (id,)):
            y = dict(s)
            y["features"] = db.unpack(y.pop("fkeys"), y["features"])
            snaps.append(y)
        live = engine.pump.get(x["address"]) or engine.dex.get(id)
        if live and not x["final"]:
            x["final"] = live.final_stats(time.time())
        return {"token": x, "snapshots": snaps}

    return app


async def serve(engine) -> None:
    d = engine.cfg.dashboard
    if d.bind_host not in LOCAL and not engine.cfg.secret("DASHBOARD_PASSWORD"):
        log.error("dashboard NOT started: it would be reachable from the network and DASHBOARD_PASSWORD is not set")
        return
    config = uvicorn.Config(build_app(engine), host=d.bind_host, port=d.port, log_level="warning", lifespan="off",
                            proxy_headers=True, forwarded_allow_ips="*")
    log.info("dashboard on %s", d.base_url)
    await uvicorn.Server(config).serve()
