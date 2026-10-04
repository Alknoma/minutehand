"""Loading scenario and agent files, and assembling drivers from an agent's wake sources."""

from __future__ import annotations

import sys
from datetime import time, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.agent.command import CommandDriver
from minutehand.adapters.agent.polled import PolledDriver
from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.agent.reported import ReportedDriver
from minutehand.application.files import FileRefused, load_agent, load_fork, load_scenario
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest, Booked, Command, GoalByMessage, Polled, Reported
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Scripted

SCENARIO = """\
name: partner_pipeline_build
goal: Three signed integration partnership agreements.
owner: owner
starts_at: 2026-08-24T10:50:03Z
deadline_after: P14D
people:
  - {key: owner, name: Test User, email: owner@example.com, reply: {kind: silent}}
  - key: sofia
    name: Sofia Romano
    email: sofia@example.com
    reply: {kind: scripted, delay: {shortest: PT36H, longest: PT36H}, replies: [{to_ask: 1, text: "Yes."}]}
    working_hours: {timezone: Europe/Rome, opens: 09:00, closes: 17:30}
ticket_fates:
  - {assignee: sofia, becomes: done, after: P3D}
"""


def test_a_yaml_scenario_loads_with_clock_times_read_as_times(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(SCENARIO)
    scn = load_scenario(path)
    sofia = scn.people[1]
    assert sofia.working_hours is not None
    assert (sofia.working_hours.opens, sofia.working_hours.closes) == (time(9), time(17, 30))
    assert isinstance(sofia.reply, Scripted) and sofia.reply.delay.shortest == timedelta(hours=36)
    assert scn.deadline_after == timedelta(days=14) and scn.ticket_fates[0].after == timedelta(days=3)


def test_a_json_agent_loads(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    path.write_text('{"name": "a", "wakes": [{"kind": "command", "argv": ["run-agent"]}, {"kind": "booked"}]}')
    agent = load_agent(path)
    assert agent.wakes == [Command(argv=["run-agent"]), Booked()]


def test_an_invalid_scenario_file_is_refused_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(SCENARIO.replace("owner: owner", "owner: nobody"))
    with pytest.raises(FileRefused, match=r"(?s)broken\.yaml.*no such person: nobody"):
        load_scenario(path)


def test_unparseable_yaml_and_an_unknown_suffix_are_refused_naming_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed")
    with pytest.raises(FileRefused, match=r"bad\.yaml: not valid YAML"):
        load_scenario(bad)
    toml = tmp_path / "agent.toml"
    toml.write_text("name = 'a'")
    with pytest.raises(FileRefused, match=r"agent\.toml"):
        load_agent(toml)


def test_reach_puts_the_reporting_source_first_and_the_polled_one_on_ticks() -> None:
    reach = reach_for(AgentUnderTest(name="a", wakes=[
        Polled(wake_url="http://a/tick", every=timedelta(minutes=10)),
        Reported(wake_url="http://a/wake", report_url="http://a/report"), Booked(),
    ]))
    assert isinstance(reach.main, ReportedDriver) and isinstance(reach.ticks, PolledDriver)
    assert reach.every == timedelta(minutes=10)
    assert isinstance(reach_for(AgentUnderTest(name="a", wakes=[Command(argv=[sys.executable])])).main, CommandDriver)


def test_an_agent_with_only_booked_wakes_is_refused() -> None:
    with pytest.raises(RunRefused, match="only Booked"):
        reach_for(AgentUnderTest(name="a", wakes=[Booked()]))


def test_an_agent_with_only_booked_wakes_that_takes_its_goal_by_message_has_no_driver() -> None:
    reach = reach_for(AgentUnderTest(name="a", wakes=[Booked()], goal=GoalByMessage(provider="slack"),
                                     inbound=[InboundTarget(provider="slack", url="http://a/events")]))
    assert reach.main is None and reach.ticks is None


def test_an_agent_with_two_reporting_sources_is_refused() -> None:
    with pytest.raises(RunRefused, match="2 Reported/Command"):
        reach_for(AgentUnderTest(name="a", wakes=[Command(argv=["x"]), Command(argv=["y"])]))


def test_a_fork_changes_file_loads_with_the_run_and_seq_given_beside_it(tmp_path: Path) -> None:
    path = tmp_path / "fork.yaml"
    path.write_text("overrides:\n  - kind: person_change\n    person: sofia\n    reply: {kind: scripted, "
                    "delay: {shortest: PT36H, longest: PT36H}, replies: [{to_ask: 1, text: 'Yes.'}]}\n")
    fork = load_fork(path, parent_run="r1", at_seq=7)
    assert (fork.parent_run, fork.at_seq, fork.samples) == ("r1", 7, 1)
    assert [o.kind for o in fork.overrides] == ["person_change"]


def test_a_fork_changes_file_naming_its_own_seq_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fork.yaml"
    path.write_text("at_seq: 3\noverrides: []\n")
    with pytest.raises(FileRefused, match="names at_seq"):
        load_fork(path, parent_run="r1", at_seq=7)
