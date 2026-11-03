"""Give back the sites that were written off while this Mac was offline.

A Mac dozing on battery wakes for a few seconds with no network, and every
site tried then failed its DNS lookup and was filed as `unreachable` - for
good, since a resume never revisits a row with a record. This finds those
rows, checks which names resolve now, and removes their record so the next
run (Continue batch, or a plain resume) tries them again.

    python requeue_offline_dns.py               # report only, changes nothing
    python requeue_offline_dns.py --apply       # remove the records

Refuses while a run is going: run.py rewrites state.json after every site,
so an edit made underneath it would simply be overwritten. A copy of
state.json is taken before anything is written.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "output" / "state.json"
LOCK = ROOT / "output" / "run.lock"
DNS_DETAIL = "domain does not resolve"
PROBE_HOSTS = ("www.google.com", "www.cloudflare.com", "www.apple.com")


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def run_in_progress() -> str:
    """Why it is not safe to edit state.json now, or "" if it is."""
    try:
        pid = int(LOCK.read_text().strip())
        os.kill(pid, 0)
        return f"output/run.lock names pid {pid}, which is still running"
    except (OSError, ValueError):
        pass
    # The lock is left behind on purpose when a run is killed for a restart,
    # so look for the process itself too.
    found = subprocess.run(["pgrep", "-f", "run.py --input"],
                           capture_output=True, text=True).stdout.split()
    if found:
        return f"run.py is running (pid {', '.join(found)})"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="remove the records (default: only report)")
    ap.add_argument("--sheet", default="",
                    help="only rows from a sheet whose file name contains this")
    args = ap.parse_args()

    busy = run_in_progress()
    if busy:
        print(f"Not safe yet: {busy}. Stop the run from the console first.")
        return 2
    socket.setdefaulttimeout(5)
    if not any(resolves(h) for h in PROBE_HOSTS):
        print("This Mac is offline right now - every lookup would fail. Try again when connected.")
        return 2

    data = json.loads(STATE.read_text(encoding="utf-8"))
    candidates = [
        url for url, e in data.items()
        if e.get("status") == "unreachable"
        and DNS_DETAIL in str(e.get("detail", ""))
        and args.sheet in str(e.get("sheet", ""))
        # A record with history is an earlier approach that later touches
        # built on - removing it would lose proof we had contacted them.
        and not e.get("touches")
    ]
    hosts = [urlparse(u if "://" in u else f"http://{u}").hostname or "" for u in candidates]
    with ThreadPoolExecutor(32) as ex:
        alive = list(ex.map(resolves, hosts))
    requeue = [u for u, ok in zip(candidates, alive) if ok]

    print(f"{len(candidates)} row(s) filed as 'domain does not resolve'; "
          f"{len(requeue)} resolve fine now and can be tried again.")
    for u in requeue[:15]:
        print(f"    {u}")
    if len(requeue) > 15:
        print(f"    and {len(requeue) - 15} more")

    if not args.apply:
        print("Report only - nothing changed. Re-run with --apply to re-queue them.")
        return 0
    if not requeue:
        return 0

    backup = STATE.with_name(f"state.json.bak.requeue_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(STATE, backup)
    for u in requeue:
        del data[u]
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(STATE)
    print(f"Removed {len(requeue)} record(s); backup at {backup.relative_to(ROOT)}. "
          f"Start the run again (Continue batch) and they will be tried.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
