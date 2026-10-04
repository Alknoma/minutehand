"""The agent called a host no provider claims; the proxy refused it and nothing in the world changed.

The run did not test what the agent would have done with that service, so its
other findings are about a different run from the real one. Whether the service
should be faked or the call should not have been made is a judgement: `review`.

The calls come from `RunView.unmatched_calls`, filled by whoever knows which hosts
were claimed. `Store.events()` cannot supply them: it returns an exchange only
through the event it produced, and these produced none.
"""

from __future__ import annotations

from minutehand.checks._waits import blocked
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity


class UnmatchedCall:
    id = "unmatched_call"
    needs = frozenset({Needs.CALLS})
    pattern = None

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing or view.unmatched_calls is None:
            return CheckReport(blocked=missing)
        return CheckReport(
            findings=[
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"{call.method} {call.host}{call.path} reached no provider and was answered {call.status}",
                    pattern=self.pattern,
                )
                for call in view.unmatched_calls
            ]
        )
