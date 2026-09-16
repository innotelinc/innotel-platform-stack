#!/usr/bin/env python3
"""Read-only SQLite integrity check across /docker/appdata (migration audit)."""
import glob
import os
import sqlite3
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/docker/appdata"
SKIP = ("-wal", "-shm", "-journal")

paths = []
for pat in ("**/*.db", "**/*.sqlite3", "**/*.sqlite"):
    paths += glob.glob(os.path.join(ROOT, pat), recursive=True)
paths = sorted({p for p in paths if not p.endswith(SKIP)})

for p in paths:
    size = os.path.getsize(p)
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        row = con.execute("PRAGMA integrity_check").fetchone()
        result = (row[0] if row else "no result")
        con.close()
    except Exception as exc:  # noqa: BLE001 - diagnostic
        result = f"ERROR {type(exc).__name__}: {str(exc)[:80]}"
    flag = "OK  " if result == "ok" else "BAD "
    print(f"{flag}{size:>9}  {p}  {'' if result == 'ok' else result[:120]}")
