"""Screenshot a page of the viewer with headless Edge or Chrome, and exit.

    uv run python scripts/screenshot.py http://127.0.0.1:8977/#<run_id> shot.png [--dark] [--size 1280,1900]

`minutehand view` serves the page; `?open` before the `#` opens every collapsed section (what the model was asked
and answered, each outbound call), which a screenshot cannot click.

Headless Edge writes the screenshot and then does not exit when it runs on a profile of its own (a fresh
`--user-data-dir`, or the default one when no other Edge is running): measured here, on a page that is only
`data:text/html,<p>hi</p>` as much as on the viewer, so it is the browser's own background work on a new profile
and nothing the page holds open. The browser writes the file when its virtual time budget is spent, so this
waits for the file to appear and stop growing, then ends the browser's whole process group.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BROWSERS = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "microsoft-edge",
    "google-chrome",
    "chromium",
)
LIMIT = 90.0
"""Seconds to wait for the file before giving up."""


def browser() -> str:
    named = os.environ.get("SCREENSHOT_BROWSER")
    for candidate in ([named] if named else []) + list(BROWSERS):
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return found
    raise SystemExit("no Edge or Chrome found: set SCREENSHOT_BROWSER to one")


def shoot(url: str, out: Path, *, dark: bool, size: str, budget: int) -> None:
    out = out.resolve()
    out.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        argv = [
            browser(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={size}",
            f"--virtual-time-budget={budget}",
            # dark is forced; light is forced too, or a machine set to dark draws the page dark either way
            "--force-dark-mode" if dark else "--blink-settings=preferredColorScheme=1",
            f"--screenshot={out}",
            url,
        ]
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            give_up = time.monotonic() + LIMIT
            last = -1
            while time.monotonic() < give_up:
                if process.poll() is not None and not out.is_file():
                    raise SystemExit(f"the browser exited {process.returncode} without writing {out}")
                size_now = out.stat().st_size if out.is_file() else -1
                if size_now > 0 and size_now == last:
                    return
                last = size_now
                time.sleep(0.5)
            raise SystemExit(f"no screenshot at {out} after {LIMIT:.0f} s")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("out", type=Path)
    parser.add_argument("--dark", action="store_true", help="prefers-color-scheme: dark")
    parser.add_argument("--size", default="1280,1900", help="width,height of the window")
    parser.add_argument("--budget", type=int, default=9000, help="virtual milliseconds the page is given")
    args = parser.parse_args()
    shoot(args.url, args.out, dark=args.dark, size=args.size, budget=args.budget)
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
