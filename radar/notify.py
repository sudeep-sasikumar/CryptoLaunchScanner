"""Telegram alerts (send-only). Same env names as Ignition & Coil: TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID. Every message has a dedupe key recorded in the database, so a restart never
repeats an alert. Without a token the text is printed to the console instead."""
from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

log = logging.getLogger("telegram")
API = "https://api.telegram.org"


class Telegram:
    def __init__(self, cfg, token: str, chat_id: str, db):
        self.token, self.chat_id, self.db = token, str(chat_id).strip(), db
        self.enabled = bool(cfg.enabled and token and self.chat_id)
        self.client = httpx.AsyncClient(timeout=20.0)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.sent = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, text: str, *, dedupe: str, kind: str, reply_to: int | None = None) -> int | None:
        if self.db.one("SELECT 1 FROM alerts WHERE dedupe=?", (dedupe,)):
            return None
        if not self.enabled:
            print(f"\n[telegram not configured - console only]\n{_plain(text)}\n", flush=True)
            msg_id, status = None, "console"
        else:
            msg_id = await self._post(text, reply_to)
            status = "sent" if msg_id else "failed"
        self.db.execute("INSERT OR REPLACE INTO alerts(dedupe,ts,kind,text,msg_id,status) VALUES(?,?,?,?,?,?)",
                        (dedupe, time.time(), kind, text, msg_id, status))
        self.sent += 1
        return msg_id

    async def _post(self, text: str, reply_to: int | None) -> int | None:
        payload: dict = {"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
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
    out = [f'<a href="https://dexscreener.com/{tok.chain}/{a}">DexScreener</a>']
    if tok.chain == "solana":
        out.append(f'<a href="https://gmgn.ai/sol/token/{a}">GMGN</a>')
        out.append(f'<a href="https://rugcheck.xyz/tokens/{a}">RugCheck</a>')
        if tok.family == "pump":
            out.append(f'<a href="https://pump.fun/coin/{a}">pump.fun</a>')
    else:
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
    lines = [head, score, f"Mcap {money(f.get('px_mcap_usd'))}"]
    if tok.family == "pump":
        lines[-1] += f" · curve {f.get('px_curve_pct', 0):.0f}% · {f.get('dist_holders', 0)} holders · {f.get('flow_buys', 0)} buys / {f.get('flow_sells', 0)} sells"
        lines.append(f"Bundle+snipers {f.get('launch_bundle_pct', 0):.1f}% · dev holds {f.get('launch_dev_hold_pct', 0):.1f}% "
                     f"(sold {f.get('launch_dev_sold_pct', 0):.0f}% of bag) · top10 {f.get('dist_top10_with_dev_pct', 0):.0f}%")
    else:
        lines[-1] += f" · liq {money(f.get('px_liq_usd'))} · h1 {f.get('flow_buys_h1', 0)} buys / {f.get('flow_sells_h1', 0)} sells"
        sec = []
        if f.get("sec_tax_max") is not None:
            sec.append(f"tax {f['sec_tax_max']:.0f}%")
        if f.get("sec_renounced") is not None:
            sec.append("renounced" if f["sec_renounced"] else "owner active")
        if f.get("sec_lp_locked_pct") is not None:
            sec.append(f"LP locked {f['sec_lp_locked_pct']:.0f}%")
        if f.get("sec_top10_pct") is not None:
            sec.append(f"top10 {f['sec_top10_pct']:.0f}%")
        if sec:
            lines.append("Contract: " + " · ".join(sec))
    wq = []
    if f.get("wq_smart_n"):
        wq.append(f"{f['wq_smart_n']} smart wallets in")
    if f.get("wq_toxic_n"):
        wq.append(f"{f['wq_toxic_n']} toxic")
    if f.get("id_name_prev_n"):
        wq.append(f"name used {f['id_name_prev_n']}x before")
    if f.get("llm_viral") is not None:
        wq.append(f"meme {f['llm_viral']}/10, originality {f.get('llm_original', '?')}/10")
    if wq:
        lines.append(" · ".join(wq))
    if good:
        lines.append("✅ " + ", ".join(good))
    if bad:
        lines.append("⚠️ " + ", ".join(bad))
    lines.append(f"<code>{esc(a)}</code>")
    lines.append(links(tok))
    return "\n".join(lines)
