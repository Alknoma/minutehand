"""`examples/follow_up` as a user runs it, with its outbound hosts: the email to Owen is acknowledged and counts
as the message that tells him, and the venue search reaches a local server standing in for the real one."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.files import load_agent, load_scenario
from minutehand.domain.agent import Reported
from minutehand.domain.outbound import Acknowledge, PassThrough
from minutehand.domain.run import VerdictKind
from minutehand.domain.world import CaptureMode, MessageSnapshot
from tests.e2e.support import free_port
from tests.proxy.upstream import Answer, make_authority, model_api

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "follow_up"


async def test_the_example_emails_owen_what_rosa_said_and_looks_the_venue_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = free_port()
    written = load_agent(EXAMPLE / "agent.yaml")
    mail, places = written.outbound
    assert isinstance(mail, Acknowledge) and isinstance(places, PassThrough) and places.host == "places.example"
    base = f"http://127.0.0.1:{port}"
    agent = written.model_copy(
        update={
            "wakes": [Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")],
            "inbound": [written.inbound[0].model_copy(update={"url": f"{base}/slack/events"})],
            # The real venue search, here a local server: the only change to the example's declarations.
            "outbound": [mail, places.model_copy(update={"host": "::1"})],
        }
    )
    authority = make_authority(tmp_path / "upstream-ca")
    found = Answer("application/json", [json.dumps({"summary": "on the lake, seats 80"}).encode()])
    async with model_api(authority, found, host="::1") as real:
        monkeypatch.setenv("PORT", str(port))
        monkeypatch.setenv("LOOKUP_URL", f"https://[::1]:{real.port}/v1/search?q=")
        [outcome] = await session.play(
            load_scenario(EXAMPLE / "scenario.yaml"),
            agent,
            state=tmp_path / "state",
            command=[sys.executable, str(EXAMPLE / "agent.py")],
            listen=session.Listen(upstream_ca=authority.ca_cert, receive_telemetry=False),
        )
    assert [r.path for r in real.received] == ["/v1/search?q=The%20lakeside%20hall%2C%20booked%20for%20the%2014th."]
    assert outcome.result.verdict.kind is VerdictKind.PASSED, outcome.result.verdict.words
    [relayed] = [f for f in outcome.result.findings if "owen told what rosa said" in f.message]
    assert "met by the message to Owen Hart" in relayed.message and "on the lake, seats 80" in relayed.message
    assert [(u.host, u.mode) for u in outcome.record.outbound] == [
        ("::1", CaptureMode.PASS_THROUGH),
        ("api.mail.example", CaptureMode.ACKNOWLEDGE),
    ]
    with session.reading(tmp_path / "state", outcome.record.run_id) as world:
        [emailed] = [e for e in world.events() if e.entity.provider == "api_mail_example"]
    assert isinstance(emailed.after, MessageSnapshot) and emailed.after.recipient_emails == ["owen@example.com"]
    assert emailed.after.text.startswith("Offsite venue\n\nThe offsite venue is confirmed: The lakeside hall")
