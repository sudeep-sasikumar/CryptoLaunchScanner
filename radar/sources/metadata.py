"""Token identity: socials, description and image from the metadata the creator published.

Answers "is there an X profile / Telegram / website", "has this image or these socials been
used by earlier tokens" and "how big is the Telegram group". Links come from token creators,
so every fetch is restricted to public internet addresses and small responses.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from ..features import social_key

log = logging.getLogger("meta")
IPFS_GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://gateway.pinata.cloud/ipfs/", "https://ipfs.io/ipfs/")
_IPFS = re.compile(r"(?:^ipfs://(?:ipfs/)?|/ipfs/)([A-Za-z0-9]{40,}(?:/[^?#]*)?)")
_MEMBERS = re.compile(r"([\d\s.,]+)\s+(?:members|subscribers)", re.I)


async def _public_host(host: str) -> bool:
    """True only if every address the name resolves to is a public one."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


class Metadata:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s, connect=4.0), follow_redirects=False,
                                        headers={"User-Agent": "Mozilla/5.0 (compatible; LaunchRadar/1.0)"})
        self.fetched = self.failed = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, max_bytes: int) -> bytes | None:
        """IPFS content is tried on several gateways (any one of them may be slow or blocked)."""
        m = _IPFS.search(url)
        if not m:
            return await self._get_one(url, max_bytes)
        for gw in IPFS_GATEWAYS:
            raw = await self._get_one(gw + m.group(1), max_bytes)
            if raw is not None:
                return raw
        return None

    async def _get_one(self, url: str, max_bytes: int) -> bytes | None:
        for _ in range(4):                                  # follow redirects by hand, checking each hop
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname or not await _public_host(u.hostname):
                return None
            try:
                async with self.client.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            return None
                    return bytes(buf)
            except (httpx.HTTPError, OSError, ValueError):
                return None
        return None

    async def from_uri(self, uri: str, ts: float) -> dict:
        """Metadata of a pump.fun-style token (JSON document at `uri`)."""
        async with self.sem:
            raw = await self._get(uri, 200_000) if uri else None
            try:
                doc = json.loads(raw) if raw else None
            except ValueError:
                doc = None
            if not isinstance(doc, dict):
                self.failed += 1
                return {"ok": False}
            self.fetched += 1
            ext = doc.get("extensions") if isinstance(doc.get("extensions"), dict) else {}
            links = {k: str(doc.get(k) or ext.get(k) or "") for k in ("twitter", "telegram", "website")}
            return await self._finish(links, str(doc.get("description") or ""), str(doc.get("image") or ""), ts)

    async def from_pair(self, pair: dict, ts: float) -> dict:
        """Metadata of any other token, from the profile DexScreener shows for it (may be empty)."""
        info = pair.get("info") or {}
        links = {"twitter": "", "telegram": "", "website": ""}
        for s in info.get("socials") or []:
            if s.get("type") in ("twitter", "telegram") and not links[s["type"]]:
                links[s["type"]] = s.get("url") or ""
        for wsite in info.get("websites") or []:
            links["website"] = links["website"] or wsite.get("url") or ""
        async with self.sem:
            return await self._finish(links, "", info.get("imageUrl") or "", ts)

    async def _finish(self, links: dict, desc: str, image: str, ts: float) -> dict:
        m: dict = {"ok": True, "desc": desc[:200], "image": image[:300]}
        x, x_kind = social_key(links["twitter"])
        tg, _ = social_key(links["telegram"])
        web, _ = social_key(links["website"])
        if web in ("x.com", "twitter.com", "t.me", "pump.fun"):       # a social link in the website field
            web = ""
        m.update({"x": x, "x_kind": x_kind, "tg": tg, "web": web})
        for kind, key in (("x", x), ("tg", tg), ("web", web)):
            if key:
                m[kind + "_prev"] = self.db.seen_bump(kind, key, ts)[0]
        if image and self.cfg.image_hash:
            h = await self._image_hash(image)
            if h:
                m["img_hash"] = h
                m["img_prev"] = self.db.seen_bump("img", h, ts)[0]
        if tg and self.cfg.telegram_members:
            m["tg_members"] = await self._tg_members(tg)
        return m

    async def _image_hash(self, url: str) -> str | None:
        raw = await self._get(url, 3_000_000)
        if not raw:
            return None
        try:
            return await asyncio.to_thread(_dhash, raw)
        except Exception:   # noqa: BLE001 - not an image, or a format Pillow cannot read
            return hashlib.sha1(raw).hexdigest()[:16]

    async def _tg_members(self, handle: str) -> int | None:
        raw = await self._get(f"https://t.me/{handle}", 300_000)
        if not raw:
            return None
        m = _MEMBERS.search(raw.decode("utf-8", "replace"))
        if not m:
            return None
        digits = re.sub(r"\D", "", m.group(1))
        return int(digits) if digits else None


def _dhash(raw: bytes) -> str:
    """Perceptual hash: the same picture re-encoded or resized gets the same 64 bits."""
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    img.seek(0)
    px = list(img.convert("L").resize((9, 8)).getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"
