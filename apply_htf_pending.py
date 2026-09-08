#!/usr/bin/env python3
"""Apply HTF pending + freshness to signal_tracker.py (idempotent)."""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "signal_tracker.py")
src = path.read_text()

if "HTF_PENDING_SECONDS" in src and "_execute_pending_long" in src:
    print("Already applied.")
    sys.exit(0)


def req(old, new, label):
    global src
    if old not in src:
        print("FAILED: could not find block:", label)
        sys.exit(1)
    src = src.replace(old, new, 1)
    print("OK:", label)


req(
    'HTF_FILTER_ENABLED   = os.environ.get("HTF_FILTER_ENABLED", "false").lower() == "true"\n',
    'HTF_FILTER_ENABLED   = os.environ.get("HTF_FILTER_ENABLED", "false").lower() == "true"\n'
    'HTF_PENDING_SECONDS  = int(os.environ.get("HTF_PENDING_SECONDS", "43200"))\n'
    'HTF_STALE_SECONDS    = int(os.environ.get("HTF_STALE_SECONDS", "0"))\n',
    "env vars",
)
