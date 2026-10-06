"""The test agent's StateHooks: `hooks.py snapshot|restore <state dir>` copies state.json to or from $MINUTEHAND_SNAPSHOT_DIR."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

state = Path(sys.argv[2]) / "state.json"
snapshot = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"]) / "state.json"

if sys.argv[1] == "snapshot":
    if state.exists():
        shutil.copyfile(state, snapshot)
    else:
        snapshot.unlink(missing_ok=True)
elif sys.argv[1] == "restore":
    if snapshot.exists():
        shutil.copyfile(snapshot, state)
    else:
        state.unlink(missing_ok=True)
else:
    sys.exit(f"unknown hook {sys.argv[1]}")
