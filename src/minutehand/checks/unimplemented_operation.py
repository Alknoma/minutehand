"""The agent called an operation of a real service that Minutehand's fake of it does not implement; the agent was
answered 501 in the vendor's error shape.

The run did not test what the agent would have done with that operation's real answer, so its other findings are
about a different run from the real one. Whether the fake should grow the operation or the agent should not have
called it is a judgement: `review`. Each operation is named once, with how many times it was called.
"""

from __future__ import annotations

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.errors import AnswerKind


class UnimplementedOperation:
    id = "unimplemented_operation"
    needs = frozenset({Needs.CALLS})
    pattern = None

    def run(self, view: RunView) -> CheckReport:
        if view.failed_calls is None:
            return CheckReport(blocked=[f"{self.id}: nobody recorded which calls the fakes could not answer"])
        counted: dict[str, int] = {}
        for call in view.failed_calls:
            if call.answer is AnswerKind.NOT_IMPLEMENTED:
                operation = f"{call.method} {call.host}{call.path.split('?', 1)[0]}"
                counted[operation] = counted.get(operation, 0) + 1
        return CheckReport(
            findings=[
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"{operation} is not implemented by Minutehand's fake, and was answered 501 "
                    f"{_times(count)}: the run did not test what the agent does with the real answer",
                    pattern=self.pattern,
                )
                for operation, count in counted.items()
            ]
        )


def _times(count: int) -> str:
    return "once" if count == 1 else f"{count} times"
