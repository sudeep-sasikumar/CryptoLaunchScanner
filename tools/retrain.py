r"""Ask the VPS to retrain the model, by hand (automatic retraining is off).

    .venv\Scripts\python.exe tools\retrain.py              train a candidate and report how it compares; nothing changes
    .venv\Scripts\python.exe tools\retrain.py --promote    ... and switch to it if it passes the usual tests
    .venv\Scripts\python.exe tools\retrain.py --status     what is in use and how the last retrain went

Signs in with DASHBOARD_PASSWORD from this PC's .env (never printed), like tools/sync_research.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar.config import load_config  # noqa: E402


def show(st: dict) -> None:
    last, use = st.get("last") or {}, st.get("in_use") or {}
    print(f"model in use: {use.get('ver', 'none')}"
          + (f" (since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(use['ts']))} UTC)" if use.get("ts") else ""))
    print(f"automatic retraining: {'ON' if st.get('auto_retrain') else 'off'}")
    if last:
        t = last.get("test") or {}
        print(f"last retrain {time.strftime('%Y-%m-%d %H:%M', time.gmtime(last.get('ts', 0)))} UTC: "
              f"{last.get('note') or last.get('verdict') or '-'}")
        if t:
            print(f"  newest unseen {t.get('n', 0):,} snapshots, base rate {t.get('base_rate', 0) * 100:.1f}%: "
                  f"top 2% picks hit {((t.get('model') or {}).get('p_top2') or 0) * 100:.0f}% (candidate) vs "
                  f"{((t.get('current_model') or {}).get('p_top2') or 0) * 100:.0f}% (in use) vs "
                  f"{((t.get('scorecard') or {}).get('p_top2') or 0) * 100:.0f}% (scorecard)")
        print(f"  passed the tests: {'yes' if last.get('would_promote') else 'no'}; switched to it: "
              f"{'yes' if last.get('promoted') else 'no'}")


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Manual model retrain on the VPS")
    ap.add_argument("--promote", action="store_true", help="switch to the new model if it passes the tests")
    ap.add_argument("--status", action="store_true", help="only show what is in use and the last retrain")
    ap.add_argument("--url", default=str(cfg.research.sync_url))
    a = ap.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password.strip():
        print("DASHBOARD_PASSWORD is not set in .env")
        return 2
    with httpx.Client(base_url=a.url.rstrip("/") + "/", timeout=60) as c:
        if c.post("api/login", json={"password": password}).status_code != 200:
            print("sign-in failed")
            return 3
        password = ""
        if not a.status:
            r = c.post("api/retrain", json={"promote": a.promote})
            if r.status_code != 200:
                print(f"could not start: HTTP {r.status_code} {r.text[:200]}")
                return 1
            print("retrain started" + (" (will switch if it passes)" if a.promote else " (check only)") + " ...")
            t0 = time.time()
            while time.time() - t0 < 2400:
                time.sleep(20)
                st = c.get("api/retrain").json()
                if not st.get("running"):
                    break
        show(c.get("api/retrain").json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
