"""A person asked by email answers, through a captured channel, with no provider in the run.

The reference agent emails Rosa through `api.mail.example`, declared `acknowledge` with `replies`: Minutehand
answers the send itself with a message id, and when Rosa's scripted answer falls due it is written into the world
as her message and delivered, signed, to the agent's own inbound webhook, which checks the signature. Before,
nobody could answer a captured send: the run failed `relayed`, and the question opened no wait.
"""

from __future__ import annotations

from tests.architecture.support import Rig


def test_a_person_asked_by_email_answers_through_the_declared_webhook_and_the_wait_settles(rig: Rig) -> None:
    done = rig.run("scenario.yaml")

    assert done.code == 0, done.out + done.err[-2000:]
    assert "Passed: no check failed, and the agent reported it was done." in done.out
    assert "waits opened: 1, still open at the end: 0" in done.out
    assert "owen told what rosa said ('LH-2291'): met by the message to Owen Hart" in done.out
    replies = [(r["from_addr"], r["in_reply_to"]) for r in rig.memory(done.run_id, "replies").values()]
    # the thread is the id the email API answered the ask with, read back from the acknowledged answer
    asked = [str(r["message_id"]) for r in rig.memory(done.run_id, "sent").values() if r["kind"] == "ask"]
    assert len(asked) == 1 and asked[0].startswith("mail-")
    assert replies == [("rosa@lakeside.example", asked[0])]
    with rig.world(done.run_id) as world:
        answer = [
            e
            for e in world.events()
            if e.actor.value == "person" and e.entity.provider == "mail" and e.entity.kind.value == "message"
        ]
    assert len(answer) == 1 and answer[0].after is not None


def test_an_unanswered_email_question_opens_a_wait_that_is_followed_up(rig: Rig) -> None:
    done = rig.run("scenario_silent.yaml", env={"REFERENCE_BEHAVIOUR": "forgetful"})

    assert done.code == 1, done.out + done.err[-2000:]
    assert "follows_up_when_due: wait on rosa: their answer was due and an hour later" in done.out
    assert "waits opened: 1, still open at the end: 1" in done.out


def test_an_agent_that_refuses_the_reply_fails_the_run_saying_so(rig: Rig) -> None:
    """Minutehand signs with the secret in its own variable; the agent was configured with another, and refuses the
    reply 401: the run stops AGENT_FAILED, naming the refusal."""
    done = rig.run(
        "scenario.yaml",
        env={"MAIL_SECRET_FOR_MINUTEHAND": "one-secret", "REFERENCE_MAIL_SECRET": "another-secret"},
        secret={"kind": "from_env", "env": "MAIL_SECRET_FOR_MINUTEHAND"},
    )

    assert done.code == 1, done.out + done.err[-2000:]
    assert "because the agent could not be reached or answered with an error" in done.out
    assert "answered 401 to a reply through api.mail.example" in done.out
