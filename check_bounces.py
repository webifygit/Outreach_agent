#!/usr/bin/env python3
"""Reclassify accepted-but-undelivered email as failed.

    python check_bounces.py            # report only, changes nothing
    python check_bounces.py --apply    # write the reclassification

SMTP "sent" means the provider took the message, not that anyone received it.
A bounce arrives minutes later in the sending mailbox, which the run never
reads, so a dead address stays recorded as contacted - and the duplicate check
then blocks that business for good. Marking the row `failed` puts it back in
reach, by a form or a different address.

Reads the mailboxes strictly read-only, and only ever looks at addresses this
agent actually wrote to.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.bounces import check_all                      # noqa: E402
from agent.config import Config, default_config_path, load_env_file   # noqa: E402

REACHED = ("sent",)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find emails that bounced and mark them failed")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (without this it only reports)")
    args = ap.parse_args(argv)

    load_env_file(ROOT)
    cfg = Config.load(default_config_path(ROOT))
    state_path = cfg.resolve(cfg.path("paths", "state_path", default="output/state.json"))

    lock = cfg.resolve(cfg.path("paths", "lock_path", default="output/run.lock"))
    if lock.exists():
        print("a run is in progress - it is writing the history. Try again when it finishes.")
        return 1

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"could not read {state_path}: {exc}")
        return 1

    by_address: dict[str, list[str]] = {}
    for url, row in state.items():
        if row.get("status") in REACHED and row.get("email_used"):
            by_address.setdefault(str(row["email_used"]).lower(), []).append(url)

    if not by_address:
        print("no sent email to check.")
        return 0
    print(f"checking {len(by_address)} address(es) against the sending mailboxes...")

    bounced, problems = check_all(cfg, set(by_address))
    for p in problems:
        print(f"  warning: {p}")

    if not bounced:
        print("no bounces found - everything we sent was accepted and stayed accepted.")
        return 0

    print(f"\nundeliverable: {len(bounced)} of {len(by_address)}")
    for addr, why in sorted(bounced.items()):
        for url in by_address[addr]:
            print(f"  {addr:<38} {url}")
            print(f"    {why[:88]}")

    if not args.apply:
        print("\nreport only - run again with --apply to mark these failed.")
        return 0

    backup = state_path.with_name(f"state-backup-bounces-{datetime.now():%Y%m%d_%H%M%S}.json")
    backup.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    changed = 0
    for addr, why in bounced.items():
        for url in by_address[addr]:
            row = state[url]
            row["status"] = "failed"
            row["detail"] = (f"email to {addr} bounced - undeliverable ({why[:60]}). "
                             + str(row.get("detail", "")))[:600]
            row["bounced_at"] = datetime.now().isoformat(timespec="seconds")
            changed += 1

    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(state_path)
    print(f"\nmarked {changed} row(s) failed. Backup: {backup.name}")
    print("those businesses are no longer treated as contacted, so they can be approached again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
