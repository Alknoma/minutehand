"""Files in the folders of the agent's machine its agent file watches (`AgentUnderTest.watches`), recorded as they
change: by the agent in a wake, by a scenario's machine command between."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from minutehand.checks.expectations import Expectations
from minutehand.checks.runner import view_of
from minutehand.domain.checks import FindingKind
from minutehand.domain.scenario import FileRemoved, MachineCommand
from minutehand.domain.world import Actor, EntityKind, FileSnapshot, Operation
from tests.orchestrator.rig import T0, Rig, scenario


async def test_what_the_agent_removes_and_what_a_machine_command_adds_are_recorded_by_whom(
    rig: Rig, tmp_path: Path
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "old.zip").write_bytes(b"x" * 300)
    (downloads / "keep.pdf").write_bytes(b"y" * 10)
    new = downloads / "toolkit.pkg"
    add = MachineCommand(
        after=timedelta(days=2),
        argv=[sys.executable, "-c", f"open({str(new)!r}, 'wb').write(b'z' * 42)"],
        said="a download appears",
    )
    expect = [
        FileRemoved(path="*/Downloads/old.zip"),
        FileRemoved(path="*/Downloads/keep.pdf", at_least=0, at_most=0),
    ]
    scn = scenario(ticket_fates=[], machine=[add], expect=expect)
    agent = rig.agent("tidy").model_copy(update={"watches": [str(downloads)]})

    record, store, _ = await rig.run(scn, agent, env=rig.env(TIDY_FILE=str(downloads / "old.zip")))

    files = [e for e in store.events() if e.entity.kind is EntityKind.FILE]
    assert [(e.actor, e.operation, Path(e.entity.external_id).name, e.sim_time) for e in files] == [
        (Actor.AGENT, Operation.DELETE, "old.zip", T0),
        (Actor.SCENARIO, Operation.CREATE, "toolkit.pkg", T0 + timedelta(days=2)),
    ]
    assert files[1].after == FileSnapshot(path=str(new), size=42)
    assert record.wakes[0].world_changes == 1, "the agent's removal is a change it made in its wake"
    view = view_of(scn, store.events(), record.wakes, store.replies())
    assert [f.kind for f in Expectations().run(view).findings] == [FindingKind.INFORMATIONAL] * 2


async def test_with_no_folder_watched_nothing_of_the_machine_is_recorded(rig: Rig, tmp_path: Path) -> None:
    gone = tmp_path / "old.zip"
    gone.write_bytes(b"x")
    _, store, _ = await rig.run(scenario(ticket_fates=[]), rig.agent("tidy"), env=rig.env(TIDY_FILE=str(gone)))

    assert not gone.exists() and not [e for e in store.events() if e.entity.kind is EntityKind.FILE]
