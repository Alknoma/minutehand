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
from minutehand.checks.runner import NO_MODEL, RunResult, discover, discover_judged, evaluate, evaluate_judged
from minutehand.domain.checks import FindingKind
from minutehand.domain.scenario import Expectation, PersonAsked
from tests.checks.world import Log, person, scenario, view
from tests.model.fake_completions import Answer, FakeCompletions, Received, fake_completions

OWNER, SOFIA, TOM = person("owner"), person("sofia"), person("tom")


def judge(received: Received) -> AskedAboutVerdict:
    """A message asks about pricing when it says price."""
    message = received.last.split("Message:", 1)[1]
    return AskedAboutVerdict(
        asks_about="price" in message, rationale="It asks for the price." if "price" in message else "It does not."
    )


def judged_blocked(result: RunResult) -> list[str]:
    return [b for b in result.blocked if b.split(":")[0] in {"asked_about"}]


def model(fake: FakeCompletions) -> OpenAICompatible:
    return OpenAICompatible(base_url=fake.base_url, api_key="sk-judged", model_id="judge-1")


def asked_sofia() -> tuple[list[Expectation], Log]:
    """A scenario's own `about` expectation, and the agent's message it is judged on."""
    log = Log()
    log.message([SOFIA], 1, text="What is the price?")
    return [PersonAsked(person="sofia", about="the price")], log


def test_the_only_judged_check_is_the_one_a_scenario_asks_for_and_it_is_not_deterministic() -> None:
    """Nothing judges a run from Minutehand's side: the one judged check reads only the scenario's `about`."""
    assert [c.id for c in discover_judged()] == ["asked_about"]
    assert "asked_about" not in {c.id for c in discover()}


async def test_a_run_whose_scenario_asks_nothing_judged_asks_the_model_nothing() -> None:
    log = Log()
    log.ticket("Contract stuff", TOM, 1)
    log.message([SOFIA], 2, text="What is the price?")
    async with fake_completions(judge) as fake:
        result = await evaluate_judged(view(scenario(OWNER, SOFIA, TOM), log), model(fake), stop=None)
    assert fake.received == [] and judged_blocked(result) == []
    assert [f for f in result.findings if f.kind is not FindingKind.INFORMATIONAL] == []


async def test_with_no_model_every_judged_check_is_blocked_and_nothing_is_asked() -> None:
    expect, log = asked_sofia()
    result = await evaluate_judged(view(scenario(OWNER, SOFIA, TOM, expect=expect), log), None, stop=None)

    assert judged_blocked(result) == [f"asked_about: {NO_MODEL}"]
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
        expect, log = asked_sofia()
        result = await evaluate_judged(view(scenario(OWNER, SOFIA, expect=expect), log), model(fake), stop=None)
    [blocked] = judged_blocked(result)
    assert blocked.startswith("asked_about: the model failed:") and "answered 500" in blocked


async def test_a_judged_finding_is_exported_with_its_model_and_prompt_version() -> None:
    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    logs.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    telemetry = OtelTelemetry(TracerProvider(), logs, MeterProvider(metric_readers=[InMemoryMetricReader()]))
    async with fake_completions(judge) as fake:
        _, log = asked_sofia()
        expect: list[Expectation] = [PersonAsked(person="sofia", about="the price", at_least=2)]
        result = await evaluate_judged(view(scenario(OWNER, SOFIA, expect=expect), log), model(fake), stop=None)

    for finding in result.findings:
        telemetry.found(finding)
    [record] = [
        r.log_record
        for r in exporter.get_finished_logs()
        if r.log_record.attributes is not None and r.log_record.attributes["minutehand.check"] == "asked_about"
    ]
    assert record.attributes is not None
    assert record.attributes["minutehand.judge.model"] == "judge-1"
    assert record.attributes["minutehand.judge.prompt_version"] == ASKED_ABOUT_PROMPT_VERSION
    assert record.attributes["minutehand.finding.kind"] == FindingKind.REVIEW.value
    unjudged = [
        r.log_record.attributes
        for r in exporter.get_finished_logs()
        if r.log_record.attributes is not None and r.log_record.attributes["minutehand.check"] != "asked_about"
    ]
    assert all(a is not None and "minutehand.judge.model" not in a for a in unjudged)


@pytest.mark.parametrize("bad", ['{"asks_about": true}', '{"asks_about": "maybe", "rationale": ""}'])
async def test_a_verdict_that_does_not_validate_is_asked_for_again(bad: str) -> None:
    def once_bad(received: Received) -> str | AskedAboutVerdict:
        return bad if len(fake.received) == 1 else AskedAboutVerdict(asks_about=True, rationale="ok")

    expect, log = asked_sofia()
    async with fake_completions(once_bad) as fake:
        result = await evaluate_judged(view(scenario(OWNER, SOFIA, expect=expect), log), model(fake), stop=None)
    assert len(fake.received) == 2 and judged_blocked(result) == []


def test_an_about_expectation_is_not_a_word_match_for_the_deterministic_check() -> None:
    expect: list[Expectation] = [PersonAsked(person="sofia", about="the price")]
    result = evaluate(view(scenario(OWNER, SOFIA, expect=expect), Log()), stop=None)

    assert [f for f in result.findings if f.check == "expectations"] == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (0, 1)
