"""Telegram alerts. Same env names as Ignition & Coil: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
(comma-separated to post to several chats), plus the TELEGRAM_* settings in config.yaml.

Every message has a dedupe key recorded in the database, so a restart never repeats an alert.
Without a token the text is printed to the console instead. Commands (/status /calls /stats
/help) are answered only when enabled, and only in the configured chats.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("telegram")
API = "https://api.telegram.org"


class Telegram:
    def __init__(self, cfg, token: str, chat_id: str, db):
        self.cfg, self.token, self.db = cfg, token, db
        self.chats = [c.strip() for c in str(chat_id or "").split(",") if c.strip()]
        self.thread_id = cfg.get("thread_id")
        self.enabled = bool(cfg.enabled and token and self.chats)
        self.client = httpx.AsyncClient(timeout=40.0)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.sent = 0
        self.on_command: Callable[[str], Awaitable[str]] | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, text: str, *, dedupe: str, kind: str, reply_to: dict | None = None,
                   silent: bool = False) -> dict | None:
        """Post to every configured chat once per dedupe key. Returns {chat id: message id}."""
        if self.db.one("SELECT 1 FROM alerts WHERE dedupe=?", (dedupe,)):
            return None
        ids: dict = {}
        if not self.enabled:
            print(f"\n[telegram not configured - console only]\n{_plain(text)}\n", flush=True)
            status = "console"
        else:
            for chat in self.chats:
                mid = await self._post(chat, text, (reply_to or {}).get(chat), silent)
                if mid:
                    ids[chat] = mid
            status = "sent" if ids else "failed"
        self.db.execute("INSERT OR REPLACE INTO alerts(dedupe,ts,kind,text,msg_id,status) VALUES(?,?,?,?,?,?)",
                        (dedupe, time.time(), kind, text, json.dumps(ids) if ids else None, status))
        self.sent += 1
        return ids or None

    async def _post(self, chat: str, text: str, reply_to: int | None, silent: bool = False) -> int | None:
        payload: dict = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}, "disable_notification": silent}
        if self.thread_id:
            payload["message_thread_id"] = int(self.thread_id)
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        async with self._lock:
            for attempt in range(4):
                wait = 1.1 - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self.client.post(f"{API}/bot{self.token}/sendMessage", json=payload)
                    self._last = time.monotonic()
                    j = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("telegram send failed (%s), retry %d", type(e).__name__, attempt + 1)
                    await asyncio.sleep(2 ** attempt)
                    continue
                if j.get("ok"):
                    return int(j["result"]["message_id"])
                if j.get("error_code") == 429:
                    await asyncio.sleep(float(j.get("parameters", {}).get("retry_after", 5)))
                    continue
                log.error("telegram rejected message: %s", j.get("description"))
                return None
        return None

    async def poll_commands(self) -> None:
        """Answer /status /calls /stats /help, from the configured chats only."""
        if not (self.enabled and self.cfg.get("commands") and self.on_command):
            return
        offset = self.db.get("tg_update_offset", 0)
        while True:
            try:
                r = await self.client.get(f"{API}/bot{self.token}/getUpdates",
                                          params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'})
                j = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("telegram getUpdates failed: %s", type(e).__name__)
                await asyncio.sleep(5)
                continue
            if not j.get("ok"):
                if j.get("error_code") == 409:
                    log.error("telegram commands off: another app is already reading this bot's messages "
                              "(use a bot of its own for this scanner)")
                    return
                await asyncio.sleep(5)
                continue
            for u in j.get("result") or []:
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat in self.chats and text.startswith("/"):
                    try:
                        reply = await self.on_command(text.split()[0].split("@")[0].lower())
                    except Exception:   # noqa: BLE001
                        log.exception("command failed")
                        reply = "Something went wrong answering that."
                    await self._post(chat, reply, msg.get("message_id"))
            self.db.put("tg_update_offset", offset)
            self.db.commit()


def message_ids(stored) -> dict:
    """calls.tg_msg_id holds {chat: message id} as JSON (older rows: one integer)."""
    if not stored:
        return {}
    if isinstance(stored, int):
        return {}
    try:
        v = json.loads(stored)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _plain(text: str) -> str:
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def money(x: float | None) -> str:
    if x is None:
        return "?"
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:.0f}"


def links(tok) -> str:
    a = tok.address if hasattr(tok, "address") else tok.mint
    gm_chain = "sol" if tok.chain == "solana" else tok.chain
    out = [f'<a href="https://gmgn.ai/{gm_chain}/token/{a}">GMGN</a>',
           f'<a href="https://dexscreener.com/{tok.chain}/{a}">DexScreener</a>']
    if tok.chain == "solana":
        out.append(f'<a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a>')
        if tok.family == "pump":
            out.append(f'<a href="https://pump.fun/coin/{a}">pump.fun</a>')
    elif getattr(tok, "pool", ""):
        out.append(f'<a href="https://www.geckoterminal.com/{tok.chain}/pools/{tok.pool}">GeckoTerminal</a>')
    m = tok.meta or {}
    if m.get("x") and m.get("x_kind") == 1:
        out.append(f'<a href="https://x.com/{esc(m["x"])}">X</a>')
    if m.get("tg"):
        out.append(f'<a href="https://t.me/{esc(m["tg"])}">TG</a>')
    return " · ".join(out)


def call_text(tok, f: dict, prior: float, prob: float | None, model_ver: str | None, good: list[str],
              bad: list[str], expected: float | None, label_cfg) -> str:
    a = tok.address if hasattr(tok, "address") else tok.mint
    age = f.get("age_s", 0)
    age_txt = f"{age / 60:.0f} min" if age >= 90 else f"{age:.0f} s"
    head = f"🚀 <b>{esc(tok.symbol)}</b> — {esc(tok.name)}\n{esc(tok.launchpad)} · {esc(tok.chain)} · {age_txt} old"
    if prob is not None:
        score = f"Chance of {label_cfg.target:g}x before -{(1 - label_cfg.stop) * 100:.0f}%: <b>{prob * 100:.0f}%</b>  (model {esc(model_ver)}) · scorecard {prior:.0f}/100"
    else:
        score = f"Scorecard <b>{prior:.0f}/100</b>  (learning - no model yet)"
        if expected is not None:
            score += f" · calls like this have hit {expected * 100:.0f}% so far"
    # the same rows for every launchpad; a value the launchpad does not expose shows as "?"
    def pct(k, d=0):
        v = f.get(k)
        return "?" if v is None else f"{v:.{d}f}%"
    market = [f"Mcap {money(f.get('px_mcap_usd'))}", f"liq {money(f.get('px_liq_usd'))}"]
    if f.get("dist_holders") is not None:
        market.append(f"{f['dist_holders']:.0f} holders")
    market.append(f"{f.get('flow_buys_per_min', 0):.0f} buys/min, {pct('flow_buy_share_recent')} buying"
                  if f.get("flow_buy_share_recent") is None else
                  f"{f.get('flow_buys_per_min', 0):.0f} buys/min, {f['flow_buy_share_recent'] * 100:.0f}% buying")
    lines = [head, score, " · ".join(market)]
    # the GMGN-style panel: holders and who they are
    dev = pct("launch_dev_hold_pct", 1)
    if f.get("launch_dev_sold_pct") is not None:
        dev += f" (sold {f['launch_dev_sold_pct']:.0f}% of bag)"
    elif f.get("gm_dev_closed") is not None:
        dev += " (closed)" if f["gm_dev_closed"] else " (holding)"
    lines.append(f"Top10 {pct('dist_top10_with_dev_pct')} · Dev {dev} · Insiders {pct('gm_insider_pct')} · "
                 f"Phishing {pct('gm_phishing_pct')} · Bundler {pct('launch_slot0_pct')} · Snipers {pct('launch_sniper_pct')}")
    sec = []
    if f.get("sec_checked"):
        sec.append("no honeypot" if not f.get("sec_honeypot") else "HONEYPOT FLAG")
        sec.append(f"tax {f.get('sec_tax_max') or 0:.0f}%")
        sec.append("no mint/freeze/blacklist" if not (f.get("sec_mintable") or f.get("sec_blacklist"))
                   else "mint/freeze/blacklist ACTIVE")
        if f.get("sec_renounced") is not None:
            sec.append("renounced" if f["sec_renounced"] else "owner active")
        if f.get("sec_open_source") is not None and f.get("chain") != "solana":
            sec.append("verified" if f["sec_open_source"] else "NOT verified")
    if f.get("sec_lp_locked_pct") is not None:
        sec.append("liquidity in bonding curve" if f.get("px_on_curve") else f"LP burnt/locked {f['sec_lp_locked_pct']:.0f}%")
    lines.append("Contract: " + (" · ".join(sec) if sec else "not checked"))
    buzz = []
    smart = max(f.get("wq_smart_n") or 0, f.get("gm_smart_n") or 0)
    if smart:
        buzz.append(f"{smart:.0f} smart wallets")
    if f.get("gm_kol_n"):
        buzz.append(f"{f['gm_kol_n']:.0f} KOLs")
    if f.get("gm_x_followers"):
        buzz.append(f"X {f['gm_x_followers']:,.0f} followers")
    if f.get("gm_tg_calls"):
        buzz.append(f"{f['gm_tg_calls']:.0f} TG calls")
    if f.get("id_dex_paid"):
        buzz.append("Dex paid")
    if f.get("gm_cto"):
        buzz.append("CTO")
    if f.get("wq_toxic_n"):
        buzz.append(f"{f['wq_toxic_n']} toxic wallets")
    if f.get("id_img_prev_n"):
        buzz.append(f"image reused {f['id_img_prev_n']:.0f}x")
    if f.get("id_name_prev_n"):
        buzz.append(f"name used {f['id_name_prev_n']:.0f}x before")
    if f.get("llm_viral") is not None:
        buzz.append(f"meme {f['llm_viral']}/10, originality {f.get('llm_original', '?')}/10")
    if buzz:
        lines.append(" · ".join(buzz))
    made = f.get("gm_creator_created_n") or f.get("cr_prev_n")
    if made:
        opened = f.get("gm_creator_open_n")
        lines.append(f"Creator has made {made:,.0f} coin{'s' if made != 1 else ''}"
                     + (f", {opened:,.0f} graduated" if opened is not None else ""))
    if good:
        lines.append("✅ " + ", ".join(good))
    if bad:
        lines.append("⚠️ " + ", ".join(bad))
    lines.append(f"<code>{esc(a)}</code>")
    lines.append(links(tok))
    return "\n".join(lines)
