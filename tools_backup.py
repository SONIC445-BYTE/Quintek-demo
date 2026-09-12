#!/usr/bin/env python3
"""
Take a backup, check it, or put one back.

WHY THIS IS A COMMAND AND NOT A CRON LINE WITH `cp`
-----------------------------------------------------
In WAL mode the `.db` file is NOT the database. Recent transactions live in
the `-wal` sidecar until a checkpoint, so copying the file while anything is
writing produces a backup that opens cleanly, passes a smoke test, and is
missing the last writes. That is the worst kind: one that looks fine.

`backup` uses SQLite's own backup API, which takes a consistent snapshot
across a live connection.

    python3 tools_backup.py take    --db quintek.db --out backups/
    python3 tools_backup.py verify  --backup backups/quintek-2026-09-12.db
    python3 tools_backup.py restore --backup backups/... --to restored.db

`verify` is not optional politeness. A backup nobody has restored is a
hypothesis, and this project has already been bitten once by evidence that was
written and never read back -- the per-item outage records that nothing could
display. So `take` verifies what it just wrote, and says so, and exits
non-zero if the copy is not readable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from student.db import Database
from student.operations import backup, restore, verify

#: Tables whose absence means the backup is not of this application. Checked
#: by name so a backup of the WRONG database fails verification rather than
#: passing because it happens to be valid SQLite.
EXPECTED = ("users", "notebooks", "sources", "source_chunks", "questions", "attempts")


def _stamped(out_dir: Path, db_path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return out_dir / f"{db_path.stem}-{stamp}.db"


def take(args) -> int:
    db = Database(args.db)
    target = Path(args.out)
    if target.is_dir() or not target.suffix:
        target = _stamped(target, Path(args.db))
    result = backup(db, target)
    checked = verify(target, expect_tables=EXPECTED)
    result["verified"] = checked
    print(json.dumps(result, indent=2) if args.json else
          f"backup   {result['path']}\n"
          f"bytes    {result['bytes']:,}\n"
          f"taken    {result['taken_at']}\n"
          f"verified {'OK' if checked['ok'] else 'FAILED: ' + str(checked)}\n"
          f"rows     {checked.get('row_counts', {})}")
    if not checked["ok"]:
        print("the backup was written but could not be read back; treat it as absent",
              file=sys.stderr)
        return 1
    return 0


def check(args) -> int:
    checked = verify(args.backup, expect_tables=EXPECTED)
    print(json.dumps(checked, indent=2))
    return 0 if checked["ok"] else 1


def put_back(args) -> int:
    try:
        result = restore(args.backup, args.to)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("take", help="take a verified backup")
    t.add_argument("--db", default="quintek.db")
    t.add_argument("--out", default="backups")
    t.add_argument("--json", action="store_true")

    v = sub.add_parser("verify", help="open a backup and read it")
    v.add_argument("--backup", required=True)

    r = sub.add_parser("restore", help="put a backup back, never over a live file")
    r.add_argument("--backup", required=True)
    r.add_argument("--to", required=True)

    args = parser.parse_args(argv)
    return {"take": take, "verify": check, "restore": put_back}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
