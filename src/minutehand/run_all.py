"""`minutehand run-all <dir>`: every scenario of a folder against one agent, in parallel, each in a run of its own.

Each scenario is played by `minutehand run` in a process of its own, so nothing one run holds (its proxy, its
receiver, the agent it started) is shared with another. What the agent itself must not share, a port it listens on
or a database it writes, the agent file and the command say with two placeholders, filled per scenario:

    {run.port}   a free port on this machine, for the agent to listen on
    {run.dir}    a folder of the scenario's own, for the agent's state (a copy of its database, say)

They are filled in the agent file's text and in each word of the command, and handed to the agent's command as
MINUTEHAND_RUN_PORT and MINUTEHAND_RUN_DIR too. An agent file naming either is written out once per scenario,
beside the original (so its relative paths read the same) under a hidden name, and removed after the run.

With `--samples N` each scenario is run N times, each under a seed of its own counted up from `--seed` (default the
scenario's own seed), so the people's moments differ from one sample to the next and any sample is played again by
`minutehand run --seed S`. Each scenario says the verdict it is written to reach (`expect_outcome`, default
`passed`): every sample must reach it, or, written as a rate (`{passed: ">= 0.9"}`), that share of its samples. The
command exits 1 when a scenario misses it, 2 when any run could not be performed, and 0 otherwise.
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
from minutehand.adapters.proxy.trust import authority
from minutehand.application.files import (
    FileKind,
    FileRefused,
    filled_as_run,
    kind_of,
    load_agent,
    load_scenario,
    read_yaml,
)
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import ExpectedOutcome, Model, OutcomeRate, Scenario, derived_seed
from minutehand.domain.templates import RUN_DIR, RUN_PORT
from minutehand.domain.world import Actor, MessageSnapshot, Operation

PORT = RUN_PORT
DIR = RUN_DIR
PORT_VARIABLE = "MINUTEHAND_RUN_PORT"
DIR_VARIABLE = "MINUTEHAND_RUN_DIR"
CA = "ca"
"""The proxy's CA folder under the state directory, as `session` names it."""
BATCHES = "run-all"
"""The folder under the state directory that holds each batch's per-scenario folders and logs."""
KEPT = "batch.json"
"""In a batch's folder: the `Batch` it came to, written once every run of it is over."""

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


class SamplePlayed(Model):
    """One run of a scenario, under one seed."""

    seed: int
    verdict: VerdictKind | None = Field(default=None, description="None: the run could not be performed")
    words: str = Field(description="The verdict's sentence, or why the run could not be performed")
    run_id: str | None = None
    timeline: list[Timeline] = []
    log: str = Field(description="Where the run's own output was written")


class ScenarioPlayed(Model):
    """One scenario of the folder, its samples, and how they came out against what it was written to reach."""

    file: str
    scenario: str
    expected: ExpectedOutcome | OutcomeRate
    samples: list[SamplePlayed]
    counts: dict[str, int] = Field(description="How many samples reached each verdict; `not_performed` for the rest")
    failing_seeds: list[int] = Field(description="The seeds whose sample did not reach the outcome expected")
    matched: bool

    @property
    def performed(self) -> bool:
        return all(s.verdict is not None for s in self.samples)


class Batch(Model):
    """What `run-all --json` prints, and what it keeps as `batch.json` in the batch's folder under the state
    directory, for the viewer's samples."""

    batch_id: str = Field(description="The batch's folder under the state directory's `run-all/`")
    folder: str
    samples: int = Field(ge=1, description="Runs of each scenario asked for")
    played: list[ScenarioPlayed]

    @property
    def exit_code(self) -> int:
        if any(not p.performed for p in self.played):
            return 2
        return 0 if all(p.matched for p in self.played) else 1


def judged(
    expected: ExpectedOutcome | OutcomeRate, samples: list[SamplePlayed]
) -> tuple[bool, list[int], dict[str, int]]:
    """Whether the samples reach what the scenario expects, the seeds of those that do not, and the count of each
    verdict. A plain outcome is expected of every sample; a rate, of that share of them."""
    outcome = expected if isinstance(expected, ExpectedOutcome) else expected.outcome
    wanted = _EXPECTED[outcome]
    failing = [s.seed for s in samples if s.verdict is not wanted]
    counts = {k.value: sum(1 for s in samples if s.verdict is k) for k in VerdictKind}
    counts["not_performed"] = sum(1 for s in samples if s.verdict is None)
    if isinstance(expected, ExpectedOutcome):
        return not failing, failing, counts
    share = (len(samples) - len(failing)) / len(samples) if samples else 0.0
    return expected.rate.met(share), failing, counts


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


_HANDED: set[int] = set()
PORTS = range(20000, 32768)
"""Where an agent's port is picked: below the ephemeral range, so a run's own proxy, which takes any free port the
system hands out, never takes the port an agent is about to listen on."""


def free_port() -> int:
    """A port nobody listens on now and no scenario of this batch was handed."""
    for _ in range(1000):
        port = secrets.choice(PORTS)
        if port in _HANDED:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        _HANDED.add(port)
        return port
    raise OSError(f"no free port between {PORTS.start} and {PORTS.stop - 1}")


def _filled(text: str, port: int, folder: Path) -> str:
    return text.replace(PORT, str(port)).replace(DIR, str(folder))


def checked_agent(agent: Path) -> None:
    """The agent file refused here, once, rather than once per scenario: read as a run reads it, with each
    placeholder filled, since a field that checks what it holds (an inbox's URL) would refuse the placeholder."""
    try:
        text = agent.read_text(encoding="utf-8")
    except OSError as e:
        raise FileRefused(f"{agent}: cannot be read: {e.strerror}") from e
    load_agent(agent, text=filled_as_run(text, agent))


ONE_RUN = "run"
"""The folder under the state directory's `run-all/` that holds the folders `{run.dir}` names for `minutehand run`."""


class FilledRun(Model):
    """An agent file and command as `minutehand run` starts them, each placeholder filled once for the invocation."""

    agent: AgentUnderTest
    command: list[str] | None
    environment: dict[str, str] = Field(description="Handed to the agent's command: empty when nothing was filled")


def filled_for_run(agent: Path, command: list[str] | None, *, state: Path) -> FilledRun:
    """`{run.port}` and `{run.dir}` in the agent file and the command, filled as `run-all` fills them for one
    scenario: a free port and a folder of the invocation's own under the state directory. A file that holds one with
    no command to start is refused (`load_agent`): nobody would listen on a port picked here."""
    try:
        text = agent.read_text(encoding="utf-8")
    except OSError as e:
        raise FileRefused(f"{agent}: cannot be read: {e.strerror}") from e
    uses = any(p in text or any(p in word for word in command or []) for p in (PORT, DIR))
    if not uses or not command:
        return FilledRun(agent=load_agent(agent), command=command, environment={})
    port = free_port()
    folder = state / BATCHES / ONE_RUN / secrets.token_hex(4)
    folder.mkdir(parents=True, exist_ok=True)
    return FilledRun(
        agent=load_agent(agent, text=_filled(text, port, folder)),
        command=[_filled(word, port, folder) for word in command],
        environment={PORT_VARIABLE: str(port), DIR_VARIABLE: str(folder)},
    )


async def play_all(
    folder: Path,
    agent: Path,
    *,
    state: Path,
    command: list[str] | None,
    jobs: int,
    judge: bool = False,
    samples: int = 1,
    seed: int | None = None,
) -> Batch:
    """Every scenario in `folder`, `samples` times each under seeds counted up from `seed`, at most `jobs` runs at
    a time."""
    if samples < 1:
        raise FileRefused(f"run-all needs at least one sample of each scenario, not {samples}")
    found = scenarios_in(folder)
    if not found:
        raise FileRefused(f"{folder}: holds no scenario file")
    authority(state / CA)  # made once here: runs started together would each make one, and trust another's
    batch_id = secrets.token_hex(4)
    batch = state / BATCHES / batch_id
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(path: Path, sample_seed: int | None, n: int) -> SamplePlayed:
        async with gate:
            return await _play(
                path, agent, state=state, batch=batch, command=command, judge=judge, seed=sample_seed, n=n
            )

    played: list[ScenarioPlayed] = []
    planned = [(p, load_scenario(p)) for p in found]
    runs = []
    for path, written in planned:
        base = seed if seed is not None else (written.seed if written.seed is not None else derived_seed(written.name))
        seeds = [None] if samples == 1 and seed is None else [base + n for n in range(samples)]
        runs.append([one(path, s, n) for n, s in enumerate(seeds)])
    results = await asyncio.gather(*(asyncio.gather(*r) for r in runs))
    for (path, written), sampled in zip(planned, results, strict=True):
        done = list(sampled)
        matched, failing, counts = judged(written.expect_outcome, done)
        played.append(
            ScenarioPlayed(
                file=str(path),
                scenario=written.name,
                expected=written.expect_outcome,
                samples=done,
                counts=counts,
                failing_seeds=failing,
                matched=matched,
            )
        )
    done = Batch(batch_id=batch_id, folder=str(folder), samples=samples, played=played)
    batch.mkdir(parents=True, exist_ok=True)
    (batch / KEPT).write_text(done.model_dump_json(indent=2), encoding="utf-8")
    return done


def batches(state: Path) -> list[Batch]:
    """Every batch `run-all` finished under `state`, oldest first by the time its file was written."""
    base = state / BATCHES
    found = sorted(base.glob(f"*/{KEPT}"), key=lambda p: p.stat().st_mtime) if base.is_dir() else []
    return [Batch.model_validate_json(p.read_text(encoding="utf-8")) for p in found]


async def _play(
    path: Path,
    agent: Path,
    *,
    state: Path,
    batch: Path,
    command: list[str] | None,
    judge: bool,
    seed: int | None,
    n: int,
) -> SamplePlayed:
    written = load_scenario(path)
    played_seed = (
        seed if seed is not None else (written.seed if written.seed is not None else derived_seed(written.name))
    )
    own = batch / written.name / str(n) if seed is not None else batch / written.name
    own.mkdir(parents=True, exist_ok=True)
    port = free_port()
    text = agent.read_text(encoding="utf-8")
    copy = agent.parent / f".{agent.stem}.{written.name}.{port}{agent.suffix}"
    uses = PORT in text or DIR in text
    if uses:
        copy.write_text(_filled(text, port, own), encoding="utf-8")
    argv = [sys.executable, "-m", "minutehand", "run", str(path), "--agent", str(copy if uses else agent)]
    argv += ["--state", str(state), "--json", *(["--judge"] if judge else [])]
    argv += ["--seed", str(seed)] if seed is not None else []
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
        return SamplePlayed(
            seed=played_seed,
            words=said[-1] if said else f"minutehand run exited {process.returncode} and said nothing",
            log=str(log),
        )
    verdict = outcome.result.verdict
    return SamplePlayed(
        seed=outcome.record.seed,
        verdict=verdict.kind,
        words=verdict.words,
        run_id=outcome.record.run_id,
        timeline=timeline(state, outcome.record.run_id),
        log=str(log),
    )


def timeline(state: Path, run_id: str) -> list[Timeline]:
    """What passed between the agent and each person, in order: each message the agent sent them, each of theirs. At
    one instant a person's reply comes before what the agent wrote on hearing it."""
    scenario: Scenario = session.scenario_of(state, run_id)
    by_email = {p.email: p.key for p in scenario.people}
    moments: dict[str, list[tuple[timedelta, int, Moment]]] = {p.key: [] for p in scenario.people}
    with session.reading(state, run_id) as world:
        for event in world.events():
            after = event.after
            if not isinstance(after, MessageSnapshot) or event.operation is not Operation.CREATE:
                continue
            if event.actor is not Actor.AGENT:
                continue
            at = event.sim_time - scenario.starts_at
            for email in after.recipient_emails:
                if email in by_email:
                    moments[by_email[email]].append((at, 1, Moment(after=at, what=f"agent: {_clip(after.text)}")))
        for reply in world.replies():
            if reply.person in moments:
                at = reply.at - scenario.starts_at
                moments[reply.person].append((at, 0, Moment(after=at, what=f"they: {_clip(reply.text)}")))
    return [
        Timeline(person=k, moments=[m for _, _, m in sorted(v, key=lambda t: (t[0], t[1]))])
        for k, v in moments.items()
        if v
    ]


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
    """The summary, then each scenario's samples and, for a scenario played once, its timeline."""
    width = max(len(p.scenario) for p in batch.played)
    lines = [f"{len(batch.played)} scenarios in {batch.folder}", ""]
    for p in batch.played:
        mark = "ok      " if p.matched else "DIFFERS "
        expected = (
            p.expected.value
            if isinstance(p.expected, ExpectedOutcome)
            else f"{p.expected.outcome.value} {p.expected.rate}"
        )
        if len(p.samples) == 1:
            one = p.samples[0]
            got = one.verdict.value if one.verdict is not None else "not performed"
            lines.append(
                f"  {mark}{p.scenario:<{width}}  {got:<13} expected {expected:<11} {one.run_id or ''} seed {one.seed}"
            )
            continue
        counted = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in p.counts.items() if n)
        lines.append(f"  {mark}{p.scenario:<{width}}  {counted}; expected {expected}")
        if p.failing_seeds:
            lines.append(f"  {'':8}{'':<{width}}  failing seeds: {', '.join(str(s) for s in p.failing_seeds)}")
    for p in batch.played:
        for one in p.samples:
            if len(p.samples) > 1 and one.verdict is not None and one.seed not in p.failing_seeds:
                continue
            lines += ["", f"{p.scenario} (seed {one.seed}): {one.words}"]
            if one.verdict is None:
                lines.append(f"  its output: {one.log}")
            for line in one.timeline:
                lines.append(f"  {line.person}")
                lines += [f"    {offset(m.after)}  {m.what}" for m in line.moments]
    differs = [p.scenario for p in batch.played if not p.matched]
    lines += ["", f"differs from what it expects: {', '.join(differs)}" if differs else "every scenario as expected"]
    lines += ["play a sample again with `minutehand run <scenario> --seed <seed>`"] if differs else []
    return "\n".join(lines)
