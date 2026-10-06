"""`minutehand serve` in a thread of this process, for a suite with no server already running."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from minutehand.serve import ServeOptions, serving

READY_SECONDS = 30.0


@contextmanager
def serve_in_background(state: Path, options: ServeOptions | None = None) -> Iterator[str]:
    """Start the server on its own event loop in a thread; yield the control API's URL; stop it, closing every
    world, when the block ends. Ports default to any free ones on loopback."""
    chosen = options or ServeOptions(proxy_port=0, control_port=0, telemetry_port=0)
    ready = threading.Event()
    found: list[str] = []
    failed: list[BaseException] = []
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()

    async def main() -> None:
        try:
            async with serving(state, chosen) as running:
                found.append(f"http://{_reached(chosen.host)}:{running.control_port}")
                ready.set()
                await stop.wait()
        except BaseException as e:
            failed.append(e)
            ready.set()

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),), name="minutehand-serve", daemon=True)
    thread.start()
    if not ready.wait(READY_SECONDS) or failed:
        raise RuntimeError(f"minutehand serve did not start: {failed[0] if failed else 'it took too long'}")
    try:
        yield found[0]
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(READY_SECONDS)
        loop.close()


def _reached(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
