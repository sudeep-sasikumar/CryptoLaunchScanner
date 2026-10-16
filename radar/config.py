"""config.yaml -> attribute access, plus .env secrets and RADAR_OVERRIDES."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class Section(dict):
    """A dict whose keys also read as attributes; nested dicts become Sections."""

    def __getattr__(self, k):
        try:
            v = self[k]
        except KeyError:
            raise AttributeError(k) from None
        return Section(v) if isinstance(v, dict) and not isinstance(v, Section) else v


def _coerce(text: str):
    text = text.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):       # a time of day; YAML would read "06:00" as the number 360
        return text
    return yaml.safe_load(text)


def apply_overrides(cfg: dict, spec: str) -> None:
    """ "a.b.c=1;x.y=foo" -> cfg[a][b][c] = 1 ..."""
    for part in filter(None, (p.strip() for p in spec.split(";"))):
        path, _, val = part.partition("=")
        node = cfg
        keys = path.strip().split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = _coerce(val)


class Config(Section):
    @property
    def data_dir(self) -> Path:
        p = Path(self["app"]["data_dir"])
        p = p if p.is_absolute() else ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def secret(self, name: str) -> str:
        return os.environ.get(name, "").strip()


def load_config(path: str | os.PathLike | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    apply_overrides(raw, os.environ.get("RADAR_OVERRIDES", ""))
    ws = os.environ.get("SOLANA_WS_URL", "").strip()
    if ws:
        raw["sources"]["pump"]["ws_url"] = ws
    rpc = os.environ.get("SOLANA_RPC_URL", "").strip()
    if rpc:
        raw["sources"]["pump"]["rpc_url"] = rpc
    if os.environ.get("COINGECKO_DEMO_API_KEY", "").strip():
        raw["sources"]["gecko"]["calls_per_min"] = max(25, raw["sources"]["gecko"]["calls_per_min"])
    port = os.environ.get("PORT", "").strip()
    if port:
        raw["dashboard"]["port"] = int(port)
    # plain environment variables for the settings people change from the Hostinger panel
    for env, path in ENV_SETTINGS.items():
        val = os.environ.get(env, "").strip()
        if val:
            apply_overrides(raw, f"{path}={val}")
    chance = os.environ.get("CALL_MIN_CHANCE", "").strip().rstrip("%")
    if chance:                                   # a percentage people type, e.g. 50 -> 0.5
        v = float(chance)
        raw["call"]["min_prob"] = raw["call"]["target_precision"] = v / 100 if v > 1 else v
    return Config(raw)


ENV_SETTINGS = {
    "TELEGRAM_ENABLED": "telegram.enabled",
    "TELEGRAM_THREAD_ID": "telegram.thread_id",
    "TELEGRAM_OUTCOME_UPDATES": "telegram.outcome_updates",
    "TELEGRAM_SILENT_OUTCOMES": "telegram.silent_outcomes",
    "TELEGRAM_DAILY_SUMMARY_UTC": "telegram.daily_summary_utc",
    "TELEGRAM_COMMANDS": "telegram.commands",
    "CALLS_PER_HOUR": "call.max_per_hour",
    "CALL_MIN_SCORE": "call.min_prior",
    "PULL_PROOF_ONLY": "call.pull_proof.only",
    "GMGN_WEIGHT_PER_SEC": "sources.gmgn.weight_per_sec",
}
