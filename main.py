"""Launch Radar - self-learning scanner for new token launches. Signals only: it never trades.

    python main.py                 run continuously
    python main.py --minutes 5     run for 5 minutes, print what was seen, exit (smoke test)
    python main.py --retrain       retrain now on the stored history, print the report, exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys

from radar.config import load_config
from radar.engine import FAMILIES, Engine


def setup_logging(cfg) -> None:
    logdir = cfg.data_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(cfg.app.log_level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = logging.handlers.RotatingFileHandler(logdir / "radar.log", maxBytes=5_000_000, backupCount=3,
                                                encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def retrain(engine: Engine) -> None:
    for fam in FAMILIES:
        report = await engine.retrain(fam)
        report.pop("importance", None)
        print(json.dumps(report, indent=1, default=str))
    await engine.http.close()
    await engine.metadata.close()
    await engine.telegram.close()


def main() -> int:
    # Windows consoles default to cp1252, which cannot print the emoji in alert text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Launch Radar - new-launch scanner (signals only; never trades)")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--minutes", type=float, default=None, help="run this long, then print counters and exit")
    ap.add_argument("--retrain", action="store_true", help="retrain on stored history and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)
    print(f"Launch Radar - version {os.environ.get('APP_VERSION', 'dev')[:7]} - data in {cfg.data_dir}", flush=True)
    engine = Engine(cfg)
    try:
        if args.retrain:
            asyncio.run(retrain(engine))
        else:
            asyncio.run(engine.run(once_s=args.minutes * 60 if args.minutes else None))
            if args.minutes:
                print(json.dumps(engine.health(), indent=1, default=str))
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
