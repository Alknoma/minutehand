"""`minutehand run-all <dir>`: every scenario of a folder against one agent, in parallel, each in a run of its own.

Each scenario is played by `minutehand run` in a process of its own, so nothing one run holds (its proxy, its
receiver, the agent it started) is shared with another. What the agent itself must not share, a port it listens on
or a database it writes, the agent file and the command say with two placeholders, filled per scenario:

    {run.port}   a free port on this machine, for the agent to listen on
    {run.dir}    a folder of the scenario's own, for the agent's state (a copy of its database, say)

They are filled in the agent file's text and in each word of the command, and handed to the agent's command as
MINUTEHAND_RUN_PORT and MINUTEHAND_RUN_DIR too. An agent file naming either is written out once per scenario,
beside the original (so its relative paths read the same) under a hidden name, and removed after the run.

Each scenario says the verdict it is written to reach (`expect_outcome`, default `passed`); the command exits 1 when
any run's verdict differs, 2 when any could not be performed, and 0 otherwise.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
from datetime import timedelta
from pathlib import Path

from pydantic import Field, ValidationError

from minutehand import session
from minutehand.application.files import FileKind, FileRefused, kind_of, load_scenario, read_yaml
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import ExpectedOutcome, Model, Scenario
from minutehand.domain.world import Actor, MessageSnapshot, Operation

PORT = "{run.port}"
DIR = "{run.dir}"
PORT_VARIABLE = "MINUTEHAND_RUN_PORT"
DIR_VARIABLE = "MINUTEHAND_RUN_DIR"
BATCHES = "run-all"
"""The folder under the state directory that holds each batch's per-scenario folders and logs."""

_EXPECTED = {
    ExpectedOutcome.PASSED: VerdictKind.PASSED,
    ExpectedOutcome.FAILED: VerdictKind.FAILED,
    ExpectedOutcome.UNFINISHED: VerdictKind.UNFINISHED,
    ExpectedOutcome.NOT_JUDGED: VerdictKind.NOT_JUDGED,
}


class Moment(Model):
    """One thing that happened between the agent and a person."""

    after: timedelta = Field(description="From the scenario's start")
    what: str


class Timeline(Model):
    person: str = Field(description="Person.key")
    moments: list[Moment]


class ScenarioPlayed(Model):
    """One scenario of the folder, and how its run came out against what it was written to reach."""

    file: str
    scenario: str
    expected: ExpectedOutcome
    verdict: VerdictKind | None = Field(default=None, description="None: the run could not be performed")
    words: str = Field(description="The verdict's sentence, or why the run could not be performed")
    run_id: str | None = None
    matched: bool
    timeline: list[Timeline] = []
    log: str = Field(description="Where the run's own output was written")


class Batch(Model):
    """What `run-all --json` prints."""

    folder: str
    played: list[ScenarioPlayed]

    @property
    def exit_code(self) -> int:
        if any(p.verdict is None for p in self.played):
            return 2
        return 0 if all(p.matched for p in self.played) else 1


def scenarios_in(folder: Path) -> list[Path]:
    """Every scenario file directly in `folder`, by name; agent files and seeds beside them are left out."""
    found: list[Path] = []
    for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml"), *folder.glob("*.json")]):
        try:
            raw = read_yaml(path.read_text(encoding="utf-8"), str(path))
        except FileRefused:
            continue
        if kind_of(raw) is FileKind.SCENARIO:
            found.append(path)
    return found


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _filled(text: str, port: int, folder: Path) -> str:
    return text.replace(PORT, str(port)).replace(DIR, str(folder))


async def play_all(
    folder: Path, agent: Path, *, state: Path, command: list[str] | None, jobs: int, judge: bool = False
) -> Batch:
    """Every scenario in `folder`, at most `jobs` at a time."""
    found = scenarios_in(folder)
    if not found:
        raise FileRefused(f"{folder}: holds no scenario file")
    batch = state / BATCHES / secrets.token_hex(4)
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(path: Path) -> ScenarioPlayed:
        async with gate:
            return await _play(path, agent, state=state, batch=batch, command=command, judge=judge)

    return Batch(folder=str(folder), played=list(await asyncio.gather(*(one(p) for p in found))))


async def _play(
    path: Path, agent: Path, *, state: Path, batch: Path, command: list[str] | None, judge: bool
) -> ScenarioPlayed:
    written = load_scenario(path)
    own = batch / written.name
    own.mkdir(parents=True, exist_ok=True)
    port = free_port()
    text = agent.read_text(encoding="utf-8")
    copy = agent.parent / f".{agent.stem}.{written.name}.{port}{agent.suffix}"
    uses = PORT in text or DIR in text
    if uses:
        copy.write_text(_filled(text, port, own), encoding="utf-8")
    argv = [sys.executable, "-m", "minutehand", "run", str(path), "--agent", str(copy if uses else agent)]
    argv += ["--state", str(state), "--json", *(["--judge"] if judge else [])]
    if command:
        argv += ["--", *(_filled(word, port, own) for word in command)]
    log = own / "run.log"
    env = {**os.environ, PORT_VARIABLE: str(port), DIR_VARIABLE: str(own)}
    try:
        with log.open("wb") as err:
            process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=err, env=env)
            out, _ = await process.communicate()
    finally:
        if uses:
            copy.unlink(missing_ok=True)
    try:
        outcome = session.Played.model_validate_json(out).outcomes[0]
    except (ValidationError, IndexError):
        said = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        return ScenarioPlayed(
            file=str(path),
            scenario=written.name,
            expected=written.expect_outcome,
            words=said[-1] if said else f"minutehand run exited {process.returncode} and said nothing",
            matched=False,
            log=str(log),
        )
    verdict = outcome.result.verdict
    return ScenarioPlayed(
        file=str(path),
        scenario=written.name,
        expected=written.expect_outcome,
        verdict=verdict.kind,
        words=verdict.words,
        run_id=outcome.record.run_id,
        matched=verdict.kind is _EXPECTED[written.expect_outcome],
        timeline=timeline(state, outcome.record.run_id),
        log=str(log),
    )


def timeline(state: Path, run_id: str) -> list[Timeline]:
    """What passed between the agent and each person, in order: each message the agent sent them, each of theirs."""
    scenario: Scenario = session.scenario_of(state, run_id)
    by_email = {p.email: p.key for p in scenario.people}
    moments: dict[str, list[Moment]] = {p.key: [] for p in scenario.people}
    with session.reading(state, run_id) as world:
        for event in world.events():
            after = event.after
            if not isinstance(after, MessageSnapshot) or event.operation is not Operation.CREATE:
                continue
            at = event.sim_time - scenario.starts_at
            if event.actor is Actor.AGENT:
                for email in after.recipient_emails:
                    if email in by_email:
                        moments[by_email[email]].append(Moment(after=at, what=f"agent: {_clip(after.text)}"))
        for reply in world.replies():
            if reply.person in moments:
                moments[reply.person].append(
                    Moment(after=reply.at - scenario.starts_at, what=f"they: {_clip(reply.text)}")
                )
    return [Timeline(person=k, moments=sorted(v, key=lambda m: m.after)) for k, v in moments.items() if v]


def _clip(text: str, most: int = 60) -> str:
    line = " ".join(text.split())
    return line if len(line) <= most else line[: most - 1] + "…"


def offset(delta: timedelta) -> str:
    """`+1d02h05m` from the start."""
    minutes = round(delta.total_seconds() / 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return f"+{days}d{hours:02d}h{minutes:02d}m"


def described(batch: Batch) -> str:
    """The summary, then each scenario's timeline."""
    width = max(len(p.scenario) for p in batch.played)
    lines = [f"{len(batch.played)} scenarios in {batch.folder}", ""]
    for p in batch.played:
        got = p.verdict.value if p.verdict is not None else "not performed"
        mark = "ok      " if p.matched else "DIFFERS "
        lines.append(f"  {mark}{p.scenario:<{width}}  {got:<13} expected {p.expected.value:<11} {p.run_id or ''}")
    for p in batch.played:
        lines += ["", f"{p.scenario}: {p.words}"]
        if p.verdict is None:
            lines.append(f"  its output: {p.log}")
        for line in p.timeline:
            lines.append(f"  {line.person}")
            lines += [f"    {offset(m.after)}  {m.what}" for m in line.moments]
    differs = [p.scenario for p in batch.played if not p.matched]
    lines += ["", f"differs from what it expects: {', '.join(differs)}" if differs else "every scenario as expected"]
    return "\n".join(lines)
