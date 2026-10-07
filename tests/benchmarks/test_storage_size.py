"""Bytes on disk, write time and read-as-of time of three synthetic runs, through the real store and proxy.

Not in the default run: `uv run pytest -q -m benchmark tests/benchmarks -s` prints one table per run.

- A, a chatty agent: 5,000 calls to Slack through the proxy, 4,500 of them `users.list` answering the same
  listing of about 20 kB, and 500 small `chat.postMessage` writes.
- B, a document agent: 50 uploads of a 4 MiB file to Drive through the proxy, with 10 distinct contents, each
  read back 5 times (`alt=media`).
- C, a long run: 200 wakes, each ending in a checkpoint whose agent-state snapshot is taken by a real snapshot
  command copying a 30 MB directory of 300 files, 3 of which change every wake.

The numbers asserted on are facts of the record (calls kept, bodies read back), never a duration.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.google_workspace.provider import build as build_drive
from minutehand.adapters.providers.slack import state as slack_state
from minutehand.adapters.providers.slack.provider import build as build_slack
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import CHECKPOINT, Checkpoint, Restorable, write_checkpoint
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import take_snapshot
from minutehand.domain.agent import StateHooks
from minutehand.domain.scenario import Person, Scenario, SignIn
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation

pytestmark = [pytest.mark.benchmark, pytest.mark.timeout(3600)]

START = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
SLACK_TOKEN = "xoxb-benchmark-token"
REFRESH = "1//benchmark-refresh-token"
MiB = 1024 * 1024


@dataclass
class Measured:
    name: str
    tables: dict[str, int]
    files: dict[str, int]
    write_seconds: float
    read_seconds: float
    read_what: str
    wal: int

    def show(self) -> str:
        lines = [f"== run {self.name}"]
        lines += [f"  table {name:<24} {size:>14,}" for name, size in sorted(self.tables.items())]
        lines += [f"  file  {name:<24} {size:>14,}" for name, size in sorted(self.files.items())]
        lines.append(f"  on disk                        {sum(self.files.values()):>14,}")
        lines.append(f"  log before the store closed     {self.wal:>14,}")
        lines.append(f"  write                          {self.write_seconds:>14.2f} s")
        lines.append(f"  read as of mid-run             {self.read_seconds * 1000:>14.2f} ms ({self.read_what})")
        return "\n".join(lines)


def tables(world: Path) -> dict[str, int]:
    """Bytes of pages each table and index holds, read through SQLite's dbstat."""
    db = sqlite3.connect(f"{world.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {name: size for name, size in db.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
    finally:
        db.close()


def files(directory: Path) -> dict[str, int]:
    """Bytes on disk under `directory`, by its top-level entries: the world file, its log, snapshot directories."""
    found: dict[str, int] = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            key = "wake-*/" if entry.name.startswith("wake-") else entry.name + "/"
            found[key] = found.get(key, 0) + sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
        else:
            found[entry.name] = entry.stat().st_size
    return found


def close(store: SqliteStore, directory: Path) -> int:
    """Close the store, answering the size of its write-ahead log just before: what sits on disk while the run's
    process still holds the file."""
    log = directory / "world.db-wal"
    size = log.stat().st_size if log.exists() else 0
    store.close()
    return size


def as_of(store: SqliteStore, at_seq: int, entity: EntityRef, clock: RunClock) -> tuple[float, int]:
    """Seconds to read `entity` as of `at_seq` (through a fork, the store's own as-of), and the body's length."""
    probe = store.fork(f"probe-{at_seq}", at_seq=at_seq, clock=clock)
    began = time.perf_counter()
    found = probe.get(entity)
    seconds = time.perf_counter() - began
    assert found is not None
    probe.discard()
    probe.close()
    return seconds, len(found.body)


@asynccontextmanager
async def proxied(store: SqliteStore, clock: RunClock, scenario: Scenario, key: str, app: object, ca: Path):
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=ca) as proxy:
        proxy.mount(store, clock, {key: app}, scenario=scenario)  # type: ignore[dict-item]
        trust = ssl.create_default_context(cafile=str(proxy.ca_bundle))
        async with httpx.AsyncClient(proxy=proxy.url, verify=trust, trust_env=False, timeout=60) as client:
            yield client


# -- A -----------------------------------------------------------------------------------------------------------


def chatty_scenario() -> Scenario:
    people = [
        Person(key=f"person{n:03d}", name=f"Person Number {n:03d}", email=f"person{n:03d}@example.com")
        for n in range(52)
    ]
    return Scenario(name="chatty", goal="Keep the channel informed.", owner="person000", starts_at=START, people=people)


async def run_a(directory: Path) -> Measured:
    scenario = chatty_scenario()
    clock = RunClock(START)
    store = SqliteStore(directory / "world.db", "a", clock)
    provider = build_slack()
    provider.seed(scenario, store)
    general = slack_state.named_channel_id(slack_state.GENERAL)
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}"}
    listing = 0
    mid_seq = 0
    began = time.perf_counter()
    async with proxied(store, clock, scenario, "slack", provider.app(store, clock), directory.parent / "ca") as client:
        for n in range(5000):
            if n % 10 == 9:
                answer = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers=headers,
                    data={"channel": general, "text": f"Update {n}: the checklist moved on."},
                )
            else:
                answer = await client.post("https://slack.com/api/users.list", headers=headers, data={"limit": "1000"})
                listing = len(answer.content)
            assert answer.status_code == 200 and answer.json()["ok"] is True
            if n == 2500:
                mid_seq = store.head()
            clock.jump(clock.now() + timedelta(seconds=30))
    written = time.perf_counter() - began
    assert len(store.calls()) == 5000
    message = next(e.entity for e in store.events() if e.entity.kind is EntityKind.MESSAGE and e.seq <= mid_seq)
    seconds, _ = as_of(store, mid_seq, message, clock)
    wal = close(store, directory)
    return Measured(
        f"A (listing {listing:,} bytes)",
        tables(directory / "world.db"),
        files(directory),
        written,
        seconds,
        "a message",
        wal,
    )


# -- B -----------------------------------------------------------------------------------------------------------


def contents() -> list[bytes]:
    """Ten 4 MiB files of text-like bytes: words drawn from a fixed vocabulary, each file its own draw."""
    words = [hashlib.sha256(str(n).encode()).hexdigest()[: 3 + n % 9] for n in range(4000)]
    made: list[bytes] = []
    for n in range(10):
        draw = random.Random(n)
        text = " ".join(draw.choice(words) for _ in range(800_000)).encode()
        made.append(text[: 4 * MiB])
    return made


async def run_b(directory: Path) -> Measured:
    scenario = Scenario(
        name="documents",
        goal="Every report is filed.",
        owner="mara",
        starts_at=START,
        people=[Person(key="mara", name="Mara Lindqvist", email="mara@example.com")],
        sign_ins=[SignIn(provider="google_workspace", credential=REFRESH, person="mara")],
    )
    clock = RunClock(START)
    store = SqliteStore(directory / "world.db", "b", clock)
    provider = build_drive()
    provider.seed(scenario, store)
    files_made: list[str] = []
    mid_seq = 0
    mid_body = b""
    bodies = contents()
    began = time.perf_counter()
    async with proxied(
        store, clock, scenario, "google_workspace", provider.app(store, clock), directory.parent / "ca"
    ) as client:
        token = await client.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "refresh_token", "refresh_token": REFRESH},
        )
        assert token.status_code == 200, token.text
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        for n in range(50):
            made = await client.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=media",
                headers={**headers, "content-type": "text/plain"},
                content=bodies[n % 10],
            )
            assert made.status_code == 200, made.text
            files_made.append(made.json()["id"])
            for _ in range(5):
                read = await client.get(
                    f"https://www.googleapis.com/drive/v3/files/{made.json()['id']}?alt=media", headers=headers
                )
                assert read.status_code == 200 and read.content == bodies[n % 10]
            if n == 25:
                mid_seq, mid_body = store.head(), bodies[n % 10]
            clock.jump(clock.now() + timedelta(seconds=30))
    written = time.perf_counter() - began
    assert len([c for c in store.calls() if c.exchange.method == "GET"]) == 250
    blob = EntityRef(
        provider="google_workspace", kind=EntityKind.RECORD, external_id=hashlib.sha256(mid_body).hexdigest()
    )
    seconds, size = as_of(store, mid_seq, blob, clock)
    wal = close(store, directory)
    return Measured(
        "B",
        tables(directory / "world.db"),
        files(directory),
        written,
        seconds,
        f"a file's bytes, {size:,} as stored",
        wal,
    )


# -- C -----------------------------------------------------------------------------------------------------------


def agent_state(directory: Path) -> None:
    """300 files of 100 kB: what the agent keeps, which its snapshot command copies whole."""
    directory.mkdir(parents=True)
    for n in range(300):
        (directory / f"part-{n:03d}.bin").write_bytes(os.urandom(100_000))


async def run_c(directory: Path) -> Measured:
    clock = RunClock(START)
    store = SqliteStore(directory / "world.db", "c", clock)
    agent = directory.parent / "agent-state"
    agent_state(agent)
    hooks = StateHooks(snapshot=["sh", "-c", f'cp -R "{agent}/." "$MINUTEHAND_SNAPSHOT_DIR"'], restore=["true"])
    note = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id="note")
    mid_seq = 0
    began = time.perf_counter()
    for wake in range(1, 201):
        clock.begin_wake()
        clock.jump(clock.now() + timedelta(hours=1))
        store.apply(
            Change(
                entity=note,
                operation=Operation.CREATE if wake == 1 else Operation.UPDATE,
                actor=Actor.AGENT,
                body=json.dumps({"text": f"wake {wake}", "ts": wake}),
            )
        )
        for n in range(3):
            (agent / f"part-{(wake * 3 + n) % 300:03d}.bin").write_bytes(os.urandom(100_000))
        await take_snapshot(hooks, store, directory.parent, wake)
        seq = write_checkpoint(
            store,
            Checkpoint(
                wake=wake,
                now=clock.now(),
                replies=0,
                pending=[],
                agent=Restorable(snapshot_of=store.run_id, wake=wake, report=None),
            ),
        )
        if wake == 100:
            mid_seq = seq
    written = time.perf_counter() - began
    seconds, _ = as_of(store, mid_seq, CHECKPOINT, clock)
    began = time.perf_counter()
    store.materialise("c", 100, directory.parent / "restoring")
    restoring = time.perf_counter() - began
    wal = close(store, directory)
    what = f"the checkpoint; its 30 MB snapshot written back out in {restoring * 1000:.0f} ms"
    return Measured("C", tables(directory / "world.db"), files(directory), written, seconds, what, wal)


async def test_storage_of_three_runs(tmp_path: Path) -> None:
    measured: list[Measured] = []
    for name, run in RUNS:
        directory = tmp_path / name / name
        directory.mkdir(parents=True)
        measured.append(await run(directory))
    print()
    for one in measured:
        print(one.show())


RUNS = [
    (name, run) for name, run in (("a", run_a), ("b", run_b), ("c", run_c)) if name in os.environ.get("BENCH", "abc")
]
