"""Elysium (Kinetiq's Hyperliquid L2, chain 99901, HYPE gas, 74 ms blocks), read straight from the chain.

GMGN, GoPlus and Codex do not cover Elysium, and GeckoTerminal does not know which launchpad made a pool. So
new launches are found from the chain's own pool-creation events (Uniswap v4 `Initialize`), the launchpad is
read from the pool's PoolManager + hook, and the contract check is done by comparing the token's bytecode with
that launchpad's own token template.

Research 2026-11-08 (scratchpad ely_*.py), over ~43,000 pools created in 24 h and 7 days of liquidity events:
- Launchpad contracts from agoradex.app's launchpad table, matched to its token tags (2,563 tokens).
- No liquidity was ever removed from a pool of the launchpads marked LOCKED; pools without a launchpad hook
  had removals in 15% of cases.
- Docs: Montra "no function that takes it out", Mesa "owned by the hook, never removable", ElysPump "locked in
  the launchpad forever", Eleos "no code path removes it", Zyora "locked forever", Obol "locked liquidity".
- Token templates: one bytecode per launchpad (immutables masked); no owner / mint / pause / blacklist function
  in the templates of the LOCKED launchpads. RONS tokens have an owner and the Volta template a mint function
  (and Volta's docs say nothing about a lock): tracked, not called.

There is no official public RPC before launch; these community nodes answered chain id 99901 on 2026-11-08.
ELYSIUM_RPC_URL (env) is tried first when set.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

log = logging.getLogger("elysium")

CHAIN_ID = 99901
RPCS = ["https://rpc.elyscan.info", "https://rpc.elyspump.com", "https://rpc-live.elysp.com", "https://rpc.elyscan.live"]
HYPE = {"0x0000000000000000000000000000000000000000", "0xba876237b0f021a6c6d852d6c26e107997cc5e81"}   # native, WHYPE
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"   # v4 PoolManager.Initialize
SHARED_PM = "0xe2e105d3f7209a9da11f83ccf3e7398a753823f1"

# (PoolManager, hook) -> launchpad
LAUNCHPADS = {
    (SHARED_PM, "0x6b1216a40b65c83e9c9e18a562c446697b8cf8ec"): "montra",
    (SHARED_PM, "0xe928f2336051cbd5b9d4531c25eadd13f9a82aec"): "mesa",
    (SHARED_PM, "0xe0def4221b9840a610d4e7d55812a79a6431a8ec"): "ascend",
    (SHARED_PM, "0x3ab88ddc3cf2a012af88b60828edb4553697e5dc"): "volta",
    (SHARED_PM, "0xbacf71bf02b8cfe171853c5ec4a060ba2ac1e0cc"): "elyspump",
    (SHARED_PM, "0x4de07902f28d00736e29aa5739dea2d4ce98aacc"): "obol",
    (SHARED_PM, "0xd80e67b50e35d5be06572f0fb2d824cf8173e8ec"): "rons",
    ("0x5fb015a49f5b35073f9ce01c2adb287a75f89600", "0xe11cafa5901b0aab51eb78251488400f9e4320cc"): "elyspump",
    ("0x06c63714fbf1f5753017123e2fcfdede4540b2c4", "0xb3e25cdf4a2f42e7f37ab3ae43bfb90ee8052acc"): "boxa",
    ("0x06c63714fbf1f5753017123e2fcfdede4540b2c4", "0xbcc1f22d137c1d98dfa5548e4d4261d7aee06acc"): "boxa",
    ("0xb308b5df891c300a5f5a5b8fdbc30108ba842490", "0xd5068e7f4e2d70d5efbac6fd28a8407252e1eacc"): "kleos",
    ("0xb308b5df891c300a5f5a5b8fdbc30108ba842490", "0x0c835e6a51717a514144d95c7f0a1c640179e0cc"): "kleos",
    ("0x5c54a7293d96b11a29a0256ee8dfdbc3059637da", "0x8680965555dcea2a4c0ed3a29e59a8aa424e6acc"): "eleos",
    ("0x3564d7defdb26abe3f0953ee57d93953ec94fa25", "0xe20830b30f2c016194035128e3601d97d5b660cc"): "elyxa",
    ("0x9fc48776883a89b9611979069ce25be1e82a536d", "0xd44b726cdef2e95a37a3c881928fcef3c9b5aacc"): "zyora",
    ("0x9fc48776883a89b9611979069ce25be1e82a536d", "0x8e4a08262426dfb7fe3620952abf71b824342acc"): "zyora",
}
# launchpad -> token template fingerprints (bytecode with 20+ byte PUSH operands zeroed, sha256[:16]);
# Montra's tokens are clones of its token master 0x10b0292f...f55f (no owner / mint / pause / blacklist).
TEMPLATES = {
    "montra": {"1174e1b47a0cfb99"}, "mesa": {"ad3152929ef72a75"}, "ascend": {"a50179fbb7fc8a3c"},
    "elyspump": {"c0c23cbb971d2c4b"}, "boxa": {"ea0ca97c9db246c7"}, "kleos": {"50af5d60fac0e831", "c250c857340a646e"},
    "eleos": {"4e80b6fdb8a540ee"}, "elyxa": {"fc4ef5ae23478196"}, "obol": {"53504935fa47a0a0"}, "zyora": {"70e43fdba5f78761"},
}


def skeleton(code: bytes) -> str:
    """Fingerprint of a contract's code with immutables (PUSH operands of 20+ bytes) zeroed. A minimal proxy
    (clone, <= 64 bytes) is fingerprinted whole: its only content is the address of the code it runs."""
    if len(code) <= 64:
        return hashlib.sha256(code).hexdigest()[:16]
    out, i = bytearray(code), 0
    while i < len(code):
        op = code[i]
        if 0x60 <= op <= 0x7f:
            n = op - 0x5f
            if n >= 20:
                out[i + 1:i + 1 + n] = b"\0" * len(out[i + 1:i + 1 + n])
            i += 1 + n
        else:
            i += 1
    return hashlib.sha256(bytes(out)).hexdigest()[:16]


def parse_init(log_: dict) -> dict | None:
    """A v4 Initialize log -> {pool, manager, hook, launchpad, token} for a HYPE-quoted pool, else None."""
    try:
        t0, t1 = "0x" + log_["topics"][2][-40:], "0x" + log_["topics"][3][-40:]
        hook = "0x" + log_["data"][2:][64 * 2 + 24:64 * 3]
        manager = log_["address"].lower()
    except (KeyError, IndexError, TypeError):
        return None
    t0, t1, hook = t0.lower(), t1.lower(), hook.lower()
    if (t0 in HYPE) == (t1 in HYPE):
        return None                                     # not a coin against HYPE
    pad = LAUNCHPADS.get((manager, hook)) or ("elysium-v4" if int(hook, 16) == 0 else "elysium-hook")
    return {"pool": log_["topics"][1].lower(), "manager": manager, "hook": hook, "launchpad": pad,
            "token": t1 if t0 in HYPE else t0, "block": int(log_.get("blockNumber") or "0x0", 16)}


class Elysium:
    def __init__(self, http, cfg):
        self.http, self.cfg = http, cfg
        self.enabled = bool(cfg.get("enabled", True))
        env = os.environ.get("ELYSIUM_RPC_URL", "").strip()
        self.urls = ([env] if env else []) + [u for u in (cfg.get("rpcs") or RPCS) if u != env]
        self.lim = http.limiter("elysium-rpc", float(cfg.get("calls_per_min") or 300))
        self.i, self.ok_ids, self.fails = 0, set(), 0
        self.next_block: int | None = None
        self.checked: dict[str, dict] = {}              # "launchpad:token" -> contract check result
        self.found = self.calls = self.errors = self.switches = 0

    @property
    def url(self) -> str:
        return self.urls[self.i % len(self.urls)]

    async def rpc(self, method: str, params: list):
        """Call the current node; on an error or a wrong chain move to the next one (each tried once)."""
        for _ in range(len(self.urls)):
            url = self.url
            if url not in self.ok_ids:
                r = await self.http.get_json(url, method="POST", limiter=self.lim, timeout=10, json_body={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
                if isinstance(r, dict) and str(r.get("result")) == hex(CHAIN_ID):
                    self.ok_ids.add(url)
                else:
                    self._switch(f"{url} is not chain {CHAIN_ID}")
                    continue
            self.calls += 1
            r = await self.http.get_json(url, method="POST", limiter=self.lim, timeout=15, json_body={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if isinstance(r, dict) and "result" in r:
                self.fails = 0
                return r["result"]
            self.errors += 1
            self._switch(f"{method} failed on {url}: {(r or {}).get('error') if isinstance(r, dict) else r}")
        return None

    def _switch(self, why: str) -> None:
        self.i += 1
        self.switches += 1
        log.debug("Elysium RPC: %s -> %s", why, self.url)

    async def new_pools(self) -> list[dict]:
        """HYPE-quoted v4 pools created since the last call (the first call starts at the chain head)."""
        head = await self.rpc("eth_blockNumber", [])
        if not head:
            return []
        head = int(head, 16)
        if self.next_block is None or head - self.next_block > 40000:
            self.next_block = head - 200                # start (or restart after an outage) near the head
        out, lo = [], self.next_block
        while lo <= head:
            hi = min(lo + 4000, head)                   # every node here accepts 4,000+ blocks per query
            logs = await self.rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi), "topics": [INIT]}])
            if logs is None:
                break
            out += [p for p in (parse_init(x) for x in logs) if p]
            lo = hi + 1
        self.next_block = lo
        self.found += len(out)
        return out

    async def contract_check(self, launchpad: str, token: str) -> dict | None:
        """The token is the launchpad's own template (no owner, mint, pause or blacklist): a contract check.
        Anything else, or an unknown launchpad, gives no check (and so no call)."""
        key = f"{launchpad}:{token}"
        if key in self.checked:
            return self.checked[key]
        code = await self.rpc("eth_getCode", [token, "latest"])
        if not isinstance(code, str) or len(code) < 4:
            return None
        fp = skeleton(bytes.fromhex(code[2:]))
        ok = fp in TEMPLATES.get(launchpad, set())
        res = {"sec_checked": 1, "sec_honeypot": 0, "sec_mintable": 0, "sec_blacklist": 0, "sec_renounced": 1,
               "sec_pausable": 0, "_template": fp} if ok else {"_template": fp, "_template_ok": 0}
        self.checked[key] = res
        if len(self.checked) > 50000:
            self.checked.clear()
        return res

    def health(self) -> dict:
        return {"node": self.url.split("//")[-1], "calls": self.calls, "errors": self.errors, "switches": self.switches,
                "pools_found": self.found, "next_block": self.next_block, "checked": len(self.checked),
                "ts": time.time()}
