"""In a run, a model call that fails to write what a person says leaves their answer owed: the failure is kept with
the world, the run's next turn tries again, and the answer lands at its moment, or then if that is later."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.application.replier import PeopleReplier, WrittenReply
from minutehand.domain.scenario import Answers, DelayRange, Person, Silent
from minutehand.domain.world import Actor, MessageSnapshot, Operation
from tests.model.fake_completions import Answer, Received, fake_completions
from tests.orchestrator.rig import T0, person, rigged, scenario


async def test_a_failed_call_leaves_the_answer_owed_and_the_next_turn_writes_it(tmp_path: Path) -> None:
    sofia = Person(
        key="sofia",
        name="Sofia",
        email="sofia@example.com",
        facts=["It is 40k a year."],
        reply=Answers(delay=DelayRange(shortest=timedelta(hours=1), longest=timedelta(hours=1))),
    )
    scn = scenario(people=[person("owner", Silent()), sofia, person("tom", Silent())])
    tries: list[Received] = []

    def flaky(received: Received) -> WrittenReply | Answer:
        tries.append(received)
        if len(tries) == 1:
            return Answer(status=503, body='{"error": {"message": "overloaded"}}')
        return WrittenReply(replies=True, text="It is 40k a year.")

    async with fake_completions(flaky) as fake, rigged(tmp_path) as rig:
        model = OpenAICompatible(base_url=fake.base_url, api_key="k", model_id="people-1")
        record, store, _ = await rig.run(scn, rig.agent("ask_and_file"), replier=PeopleReplier(scn, model))

    failed, written = store.person_calls()[:2]
    assert failed.answer is None and failed.failure is not None and "503" in failed.failure
    assert written.answer is not None and written.asked == failed.asked
    [first] = [
        e
        for e in store.events()
        if e.actor is Actor.PERSON and e.operation is Operation.CREATE and isinstance(e.after, MessageSnapshot)
    ][:1]
    # Drawn for an hour after the ask; written on the run's next turn, before that hour was up, so it lands on time.
    assert first.sim_time == T0 + timedelta(hours=1) and failed.sim_time == written.sim_time == T0
    assert record.ended_at >= first.sim_time
