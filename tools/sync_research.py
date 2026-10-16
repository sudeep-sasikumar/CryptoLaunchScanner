"""Copy the VPS's research data to this PC (same idea as Ignition & Coil's tool of the same name).

    .venv\\Scripts\\python.exe tools\\sync_research.py      (daily by Task Scheduler once you have run
                                                            tools\\install_daily_sync.ps1)

Signs in to the dashboard (research.sync_url in config.yaml) with DASHBOARD_PASSWORD from this PC's
.env, then downloads every day file this PC does not have yet into var/research/vps/, and always
re-downloads the newest days, whose outcomes are still being decided (a snapshot is followed for up
to 6 hours). A file replaces the old copy only after it passes a full gzip check. Then it writes the
traits report (tools/analyze.py) to var/research/report-<date>.md. The password is never printed,
logged or passed on a command line. Log: var/logs/research_sync.log.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar.config import ROOT, load_config  # noqa: E402
from radar.export import NAME  # noqa: E402

log = logging.getLogger("research_sync")


def gzip_ok(p: Path) -> bool:
    try:
        with gzip.open(p, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def download(c: httpx.Client, name: str, dest: Path, tries: int = 3) -> int | None:
    """Fetch one file to a temporary name, verify it, then swap it in. Returns bytes, or None."""
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(1, tries + 1):
        try:
            with c.stream("GET", f"/api/research/file/{name}") as r:
                if r.status_code != 200:
                    log.warning("%s: HTTP %s", name, r.status_code)
                    return None
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            if gzip_ok(tmp):
                os.replace(tmp, dest)
                return dest.stat().st_size
            log.info("%s: incomplete copy, retry %d", name, attempt)
        except httpx.HTTPError as e:
            log.info("%s: %s, retry %d", name, type(e).__name__, attempt)
        time.sleep(5 * attempt)
    tmp.unlink(missing_ok=True)
    return None


def main() -> int:
    cfg = load_config()                              # also loads this PC's .env
    ap = argparse.ArgumentParser(description="Download the VPS's research data")
    ap.add_argument("--url", default=str(cfg.research.sync_url), help="dashboard address (default research.sync_url)")
    ap.add_argument("--dest", default=str(cfg.data_dir / "research" / "vps"))
    ap.add_argument("--refresh-days", type=int, default=3,
                    help="re-download the newest N UTC days (outcomes and 48-hour liquidity checks still landing)")
    ap.add_argument("--no-report", action="store_true", help="only download; skip writing the traits report")
    a = ap.parse_args()

    logs = cfg.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(logs / "research_sync.log", encoding="utf-8")]
    if sys.stdout is not None:                       # pythonw (Task Scheduler) has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password.strip():
        log.error("DASHBOARD_PASSWORD is not set in %s - add it there (the one you sign in to the dashboard with)",
                  ROOT / ".env")
        return 2
    today = datetime.now(timezone.utc).date()
    refresh = {(today - timedelta(days=k)).isoformat() for k in range(a.refresh_days)}
    got = kept = failed = 0
    with httpx.Client(base_url=a.url.rstrip("/") + "/", timeout=180, follow_redirects=False) as c:
        r = c.post("api/login", json={"password": password})
        password = ""                                # not needed any more
        if r.status_code != 200:
            log.error("sign-in to %s failed: HTTP %s", a.url, r.status_code)
            return 3
        days = c.get("api/research").json().get("days", [])
        for d in days:
            name = d["name"]
            m = NAME.fullmatch(name)
            if not m:
                continue
            local = dest / name
            if local.exists() and m.group(1) not in refresh:
                kept += 1
                continue
            existed = local.exists()
            size = download(c, name, local)
            if size is None:
                failed += 1
                log.warning("%s: not downloaded (kept the previous copy, if any)", name)
            else:
                got += 1
                log.info("%s: %s (%.2f MB, %d snapshots)", name, "refreshed" if existed else "downloaded",
                         size / 1e6, d.get("snapshots", 0))
    log.info("done: %d downloaded/refreshed, %d already up to date, %d failed, %d days on the VPS -> %s",
             got, kept, failed, len(days), dest)
    if not a.no_report:
        from tools.analyze import default_out, write_report
        out = default_out(cfg)
        try:
            write_report(dest, out)
            log.info("traits report: %s", out)
        except Exception:                            # a report problem must not hide a good download
            log.exception("traits report failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
