"""Checks a model judges, over hand-built worlds, against a completions server on this machine.

The server's rules say what the model would answer; what is tested is what it is shown, which entities it is
never shown, and what becomes of its verdict."""

from __future__ import annotations

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.adapters.telemetry.otel import OtelTelemetry
from minutehand.checks.judged.asked_about import ASKED_ABOUT_PROMPT_VERSION, AskedAboutVerdict
from minutehand.checks.judged.ticket_is_actionable import TICKET_PROMPT_VERSION, TicketVerdict
from minutehand.checks.runner import NO_MODEL, RunResult, discover, discover_judged, evaluate, evaluate_judged
from minutehand.domain.checks import FindingKind, Severity
from minutehand.domain.conversation import Judgement
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Expectation, PersonAsked
from minutehand.domain.world import Actor, Operation
from tests.checks.world import Log, person, scenario, view
from tests.model.fake_completions import Answer, FakeCompletions, Received, fake_completions

OWNER, SOFIA, TOM = person("owner"), person("sofia"), person("tom")
CLEAR = "Sign the Acme order form by Friday 11 September"
VAGUE = "Contract stuff"


def judge(received: Received) -> TicketVerdict | AskedAboutVerdict:
    """A ticket is clear when its title names a deadline; a message asks about pricing when it says price."""
    if received.schema_name == "TicketVerdict":
        clear = "by Friday" in received.last
        return TicketVerdict(
            knows_what=clear, knows_by_when=clear, rationale="Clear." if clear else "No task and no date."
        )
    message = received.last.split("Message:", 1)[1]
    return AskedAboutVerdict(
        asks_about="price" in message, rationale="It asks for the price." if "price" in message else "It does not."
    )


def judged_blocked(result: RunResult) -> list[str]:
    return [b for b in result.blocked if b.split(":")[0] in {"asked_about", "ticket_is_actionable"}]


def model(fake: FakeCompletions) -> OpenAICompatible:
    return OpenAICompatible(base_url=fake.base_url, api_key="sk-judged", model_id="judge-1")


def tickets() -> Log:
    log = Log()
    log.ticket(CLEAR, TOM, 1)
    log.ticket(VAGUE, TOM, 2)
    return log


def test_the_two_judged_checks_are_found_and_are_not_deterministic_checks() -> None:
    assert [c.id for c in discover_judged()] == ["asked_about", "ticket_is_actionable"]
    assert not {c.id for c in discover()} & {"asked_about", "ticket_is_actionable"}


async def test_an_unclear_ticket_is_a_review_finding_carrying_the_model_and_prompt_version() -> None:
    world = view(scenario(OWNER, TOM), tickets())
    async with fake_completions(judge) as fake:
        result = await evaluate_judged(world, model(fake), stop=StopReason.AGENT_DONE)

    [finding] = [f for f in result.findings if f.check == "ticket_is_actionable"]
    assert finding.kind is FindingKind.REVIEW and finding.severity is Severity.WARNING
    assert finding.judged == Judgement(
        model="judge-1", prompt_version=TICKET_PROMPT_VERSION, rationale="No task and no date."
    )
    assert finding.evidence == [2] and VAGUE in finding.message and "what is asked or by when" in finding.message
    assert result.exit_code == 0
    shown = [r.last for r in fake.for_schema("TicketVerdict")]
    assert len(shown) == 2 and all("Assigned to: Tom" in s for s in shown)
    assert all(r.temperature == 0 for r in fake.received)


async def test_a_ticket_a_deterministic_check_failed_is_not_judged() -> None:
    log = Log()
    log.ticket("Review the Acne order form by Friday", TOM, 1)
    log.ticket(VAGUE, TOM, 2)
    world = view(scenario(OWNER, TOM).model_copy(update={"protected_names": ["Acme"]}), log)
    async with fake_completions(judge) as fake:
        result = await evaluate_judged(world, model(fake), stop=None)

    assert [f.check for f in result.findings if f.kind is FindingKind.FAIL] == ["near_miss_name"]
    assert [VAGUE in r.last for r in fake.for_schema("TicketVerdict")] == [True]


async def test_a_deleted_ticket_is_not_judged_and_an_edited_one_is_judged_as_it_ended() -> None:
    log = Log()
    gone = log.ticket(VAGUE, TOM, 1)
    log._add(2, Actor.AGENT, Operation.DELETE, gone.entity, None, 1)
    edited = log.ticket(VAGUE, TOM, 3)
    log.ticket(CLEAR, TOM, 4, operation=Operation.UPDATE, external_id=edited.entity.external_id)
    async with fake_completions(judge) as fake:
        result = await evaluate_judged(view(scenario(OWNER, TOM), log), model(fake), stop=None)

    assert [CLEAR in r.last for r in fake.for_schema("TicketVerdict")] == [True]
    assert [f for f in result.findings if f.check == "ticket_is_actionable"] == []


async def test_with_no_model_every_judged_check_is_blocked_and_nothing_is_asked() -> None:
    expect: list[Expectation] = [PersonAsked(person="sofia", about="the price")]
    log = tickets()
    log.message([SOFIA], 3, text="What is the price?")
    result = await evaluate_judged(view(scenario(OWNER, SOFIA, TOM, expect=expect), log), None, stop=None)

    assert judged_blocked(result) == [f"asked_about: {NO_MODEL}", f"ticket_is_actionable: {NO_MODEL}"]
    assert [f for f in result.findings if f.kind is not FindingKind.INFORMATIONAL] == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (0, 1)


async def test_unjudged_runs_neither_judge_nor_list_judged_checks_as_blocked_and_meet_no_about() -> None:
    expect: list[Expectation] = [PersonAsked(person="sofia", about="the price"), PersonAsked(person="sofia")]
    log = Log()
    log.message([SOFIA], 3, text="What is the price?")
    result = evaluate(view(scenario(OWNER, SOFIA, expect=expect), log), stop=None)

    assert judged_blocked(result) == []
    assert "expectations: sofia asked about 'the price': left to the judged check asked_about" in result.notes
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (1, 2)


async def test_asked_about_counts_only_the_messages_the_model_says_ask_about_it() -> None:
    expect: list[Expectation] = [
        PersonAsked(person="sofia", about="the partner price"),
        PersonAsked(person="sofia", about="the partner price", at_least=2),
        PersonAsked(person="sofia", about="the partner price", mentions=["renewal"]),
    ]
    log = Log()
    log.message([SOFIA], 1, text="Hi Sofia, I am handling the partner deal.")
    asked = log.message([SOFIA], 2, text="What price did we agree for partners?")
    log.message([TOM], 3, text="What price did we agree for partners?")
    async with fake_completions(judge) as fake:
        result = await evaluate_judged(view(scenario(OWNER, SOFIA, TOM, expect=expect), log), model(fake), stop=None)

    findings = [f for f in result.findings if f.check == "asked_about"]
    assert [f.message for f in findings] == [
        "sofia asked about 'the partner price': wanted at least 2, judged 1",
        "sofia asked about 'the partner price' mentioning ['renewal']: wanted at least 1, judged 0",
    ]
    assert all(f.kind is FindingKind.REVIEW for f in findings)
    assert findings[0].evidence == [1, asked.seq] and findings[1].evidence == []
    assert findings[0].judged is not None and findings[0].judged.prompt_version == ASKED_ABOUT_PROMPT_VERSION
    assert f"[seq {asked.seq}] It asks for the price." in findings[0].judged.rationale
    # Two expectations judged Sofia's two messages each; the third had none to judge. Tom's was never shown.
    shown = fake.for_schema("AskedAboutVerdict")
    assert len(shown) == 4 and all("To: Sofia" in r.last and "Topic: the partner price" in r.last for r in shown)
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (1, 3)


async def test_a_model_that_fails_blocks_the_check_it_failed_in() -> None:
    async with fake_completions(lambda _: Answer(status=500, body='{"error": "down"}')) as fake:
        result = await evaluate_judged(view(scenario(OWNER, TOM), tickets()), model(fake), stop=None)
    [blocked] = judged_blocked(result)
    assert blocked.startswith("ticket_is_actionable: the model failed:") and "answered 500" in blocked


async def test_a_judged_finding_is_exported_with_its_model_and_prompt_version() -> None:
    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    logs.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    telemetry = OtelTelemetry(TracerProvider(), logs, MeterProvider(metric_readers=[InMemoryMetricReader()]))
    async with fake_completions(judge) as fake:
        result = await evaluate_judged(view(scenario(OWNER, TOM), tickets()), model(fake), stop=None)

    for finding in result.findings:
        telemetry.found(finding)
    [record] = [
        r.log_record
        for r in exporter.get_finished_logs()
        if r.log_record.attributes is not None and r.log_record.attributes["minutehand.check"] == "ticket_is_actionable"
    ]
    assert record.attributes is not None
    assert record.attributes["minutehand.judge.model"] == "judge-1"
    assert record.attributes["minutehand.judge.prompt_version"] == TICKET_PROMPT_VERSION
    assert record.attributes["minutehand.finding.kind"] == FindingKind.REVIEW.value
    unjudged = [
        r.log_record.attributes
        for r in exporter.get_finished_logs()
        if r.log_record.attributes is not None and r.log_record.attributes["minutehand.check"] != "ticket_is_actionable"
    ]
    assert all(a is not None and "minutehand.judge.model" not in a for a in unjudged)


@pytest.mark.parametrize(
    "bad", ['{"knows_what": true}', '{"knows_what": "maybe", "knows_by_when": true, "rationale": ""}']
)
async def test_a_verdict_that_does_not_validate_is_asked_for_again(bad: str) -> None:
    def once_bad(received: Received) -> str | TicketVerdict:
        return bad if len(fake.received) == 1 else TicketVerdict(knows_what=True, knows_by_when=True, rationale="ok")

    log = Log()
    log.ticket(CLEAR, TOM, 1)
    async with fake_completions(once_bad) as fake:
        result = await evaluate_judged(view(scenario(OWNER, TOM), log), model(fake), stop=None)
    assert len(fake.received) == 2 and judged_blocked(result) == []


def test_an_about_expectation_is_not_a_word_match_for_the_deterministic_check() -> None:
    expect: list[Expectation] = [PersonAsked(person="sofia", about="the price")]
    result = evaluate(view(scenario(OWNER, SOFIA, expect=expect), Log()), stop=None)

    assert [f for f in result.findings if f.check == "expectations"] == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (0, 1)
