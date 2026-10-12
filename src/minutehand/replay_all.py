"""`minutehand replay-all <cases.yaml>`: every case of a file (a finished run, and the checkpoint to play on from)
replayed against the agent as it is now, each by a `minutehand replay` in a process of its own, and one line per
case saying whether the record still answers it whole.

A case is a run that was recorded and a checkpoint in it, the moment before whatever a team wants to keep an eye
on. Frozen (`--frozen`), nothing is sent to a model: a case the record answers whole is `same`, one where the agent
now asks its model something the record does not hold is `parted` at that call, with what was sent beside what the
record held next in the child's `replayed.json`. So a change to the agent's prompt or code is told by which cases it
touched and at which call, in minutes and at no model cost, before any case is played live. Without `--frozen`
each case goes live from the call it parts at, as `replay` does, and its verdict is read against its parent's.

A replay plays the agent file its run kept, so cases of one run listen where that run's agent did: they are played
one at a time unless `--jobs` says otherwise. The command exits 0 when every case is `same`, 1 when any parted or
ended with another verdict than its parent, and 2 when any could not be performed.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import Field, ValidationError

from minutehand import session
from minutehand.adapters.proxy.trust import authority
from minutehand.domain.experiment import ReplayCase, ReplayCases
from minutehand.domain.run import ReplayRecord, VerdictKind
from minutehand.domain.scenario import Model
from minutehand.run_all import CA, DIR_VARIABLE, PORT_VARIABLE, filled, free_port

SWEEPS = "replay-all"
"""The folder under the state directory that holds each sweep's per-case folders and logs."""
KEPT = "sweep.json"
"""In a sweep's folder: the `Sweep` it came to, written once every case of it is over."""


class CaseOutcome(StrEnum):
    SAME = "same"  # every model call answered from the record, and the verdict its parent reached
    PARTED = "parted"  # the record did not hold a call the agent made: frozen, it stopped there; else it went live
    DIFFERS = "differs"  # answered from the record whole, yet another verdict than its parent's
    NOT_PERFORMED = "not_performed"  # the replay could not be performed: its output says why


class CasePlayed(Model):
    run: str
    at: int | None
    outcome: CaseOutcome
    run_id: str | None = Field(default=None, description="The child run; None when not performed")
    verdict: VerdictKind | None = None
    parent_verdict: VerdictKind | None = None
    replayed: ReplayRecord | None = Field(default=None, description="The child's `replayed.json`")
    words: str
    log: str


class Sweep(Model):
    sweep_id: str
    cases_file: str
    frozen: bool
    played: list[CasePlayed]

    @property
    def exit_code(self) -> int:
        outcomes = {p.outcome for p in self.played}
        if CaseOutcome.NOT_PERFORMED in outcomes:
            return 2
        return 1 if outcomes & {CaseOutcome.PARTED, CaseOutcome.DIFFERS} else 0


async def play_all(
    cases: ReplayCases,
    *,
    cases_file: Path,
    state: Path,
    command: list[str] | None,
    jobs: int,
    frozen: bool,
    judge: bool = False,
    passed_on: list[str] | None = None,
) -> Sweep:
    """Every case, at most `jobs` at a time; `passed_on`, flags each `minutehand replay` takes as they are."""
    authority(state / CA)  # made once here: replays started together would each make one, and trust another's
    sweep_id = secrets.token_hex(4)
    folder = state / SWEEPS / sweep_id
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(case: ReplayCase) -> CasePlayed:
        async with gate:
            return await _play(
                case, state=state, folder=folder, command=command, frozen=frozen, judge=judge, passed_on=passed_on or []
            )

    played = list(await asyncio.gather(*(one(case) for case in cases.cases)))
    done = Sweep(sweep_id=sweep_id, cases_file=str(cases_file), frozen=frozen, played=played)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / KEPT).write_text(done.model_dump_json(indent=2), encoding="utf-8")
    return done


def sweeps(state: Path) -> list[Sweep]:
    """Every sweep `replay-all` finished under `state`, oldest first by the time its file was written."""
    base = state / SWEEPS
    found = sorted(base.glob(f"*/{KEPT}"), key=lambda p: p.stat().st_mtime) if base.is_dir() else []
    return [Sweep.model_validate_json(p.read_text(encoding="utf-8")) for p in found]


async def _play(
    case: ReplayCase,
    *,
    state: Path,
    folder: Path,
    command: list[str] | None,
    frozen: bool,
    judge: bool,
    passed_on: list[str],
) -> CasePlayed:
    own = folder / (case.run if case.at is None else f"{case.run}-{case.at}")
    own.mkdir(parents=True, exist_ok=True)
    port = free_port()
    argv = replay_argv(
        case,
        state=state,
        frozen=frozen,
        judge=judge,
        passed_on=passed_on,
        command=[filled(word, port, own) for word in command] if command else None,
    )
    log = own / "replay.log"
    env = {**os.environ, PORT_VARIABLE: str(port), DIR_VARIABLE: str(own)}
    with log.open("wb") as err:
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=err, env=env)
        out, _ = await process.communicate()
    try:
        outcome = session.Played.model_validate_json(out).outcomes[0]
    except (ValidationError, IndexError):
        said = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        return CasePlayed(
            run=case.run,
            at=case.at,
            outcome=CaseOutcome.NOT_PERFORMED,
            words=said[-1] if said else f"minutehand replay exited {process.returncode} and said nothing",
            log=str(log),
        )
    child = outcome.record.run_id
    replayed = session.replayed(state, child)
    verdict = outcome.result.verdict.kind
    parent_verdict = session.load(state, case.run).result.verdict.kind
    if replayed is not None and replayed.parted is not None:
        kind, words = CaseOutcome.PARTED, _parted(replayed, frozen)
    elif verdict is not parent_verdict:
        kind = CaseOutcome.DIFFERS
        words = f"answered from the record whole, yet {verdict.value} where its parent {parent_verdict.value}"
    else:
        kind = CaseOutcome.SAME
        calls = replayed.model_calls_replayed if replayed is not None else 0
        words = f"{calls} model call(s) from the record, {verdict.value} as its parent"
    return CasePlayed(
        run=case.run,
        at=case.at,
        outcome=kind,
        run_id=child,
        verdict=verdict,
        parent_verdict=parent_verdict,
        replayed=replayed,
        words=words,
        log=str(log),
    )


def _parted(replayed: ReplayRecord, frozen: bool) -> str:
    where = replayed.parted or ""
    after = f"after {replayed.model_calls_replayed} call(s) from the record"
    if frozen:
        return f"parted {after}: {where}; stopped there, what was sent beside what the record held in replayed.json"
    return f"parted {after}: {where}; live from there"


def replay_argv(
    case: ReplayCase,
    *,
    state: Path,
    frozen: bool,
    judge: bool,
    passed_on: list[str],
    command: list[str] | None,
) -> list[str]:
    """The `minutehand replay` one case is played by."""
    argv = [sys.executable, "-m", "minutehand", "replay", case.run, "--state", str(state), "--json"]
    argv += ["--at", str(case.at)] if case.at is not None else []
    argv += ["--frozen"] if frozen else []
    argv += ["--judge"] if judge else []
    argv += passed_on
    return argv + (["--", *command] if command else [])


def described(sweep: Sweep) -> str:
    """The summary, then one line per case."""
    marks = {
        CaseOutcome.SAME: "same         ",
        CaseOutcome.PARTED: "PARTED       ",
        CaseOutcome.DIFFERS: "DIFFERS      ",
        CaseOutcome.NOT_PERFORMED: "not performed",
    }
    counted = ", ".join(
        f"{n} {k.value.replace('_', ' ')}" for k in CaseOutcome if (n := sum(1 for p in sweep.played if p.outcome is k))
    )
    lines = [f"{len(sweep.played)} case(s) in {sweep.cases_file}{', frozen' if sweep.frozen else ''}: {counted}", ""]
    for p in sweep.played:
        where = f"{p.run} at {p.at}" if p.at is not None else f"{p.run} from the start"
        lines.append(f"  {marks[p.outcome]}  {where:<30}  {p.run_id or '':<12}  {p.words}")
        if p.outcome is CaseOutcome.NOT_PERFORMED:
            lines.append(f"  {'':13}  its output: {p.log}")
    touched = [p for p in sweep.played if p.outcome is not CaseOutcome.SAME]
    lines += [
        "",
        f"{len(touched)} case(s) the change touched" if touched else "every case as its record has it",
    ]
    return "\n".join(lines)
