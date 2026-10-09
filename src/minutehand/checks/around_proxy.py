"""The agent called a service Minutehand fakes without going through Minutehand.

Its HTTP client ignored `HTTPS_PROXY` and reached the real service: the run tested a different world from the one the
scenario set up, and whatever the real service did is in no record. When the agent's own telemetry shows the calls
(`RunView.around_proxy`), that is a fact, stated as a review, and a failure only when the agent file or the
scenario names `around_proxy` in `fail_on_integrity`. When the only sign is that the agent was woken and called
nothing of any provider the run names (`RunView.uncalled_providers`), it may as well have done nothing: `review`.
"""

from __future__ import annotations

from minutehand.domain.assessments import IntegrityCheck
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity

FIXES = (
    "Node's built-in fetch (and @slack/web-api v8 on it) reads HTTPS_PROXY only with NODE_USE_ENV_PROXY=1 on Node 24 "
    "or later, which Minutehand hands out to every agent: an older Node needs one of the last two fixes; httplib2 needs "
    "PySocks installed beside it; aiohttp needs ClientSession(trust_env=True). Any other client: give it a base URL "
    "(`base_urls` in the agent file), or run the agent in a Linux container with --transparent-port, which captures "
    "a client that ignores every proxy variable (docs/containers.md). `minutehand doctor` names the clients it can "
    "probe"
)


class WentAroundProxy:
    id = "around_proxy"
    needs = frozenset[Needs]()
    pattern = None

    def run(self, view: RunView) -> CheckReport:
        kind, severity = view.integrity(IntegrityCheck.AROUND_PROXY)
        findings = [
            Finding(
                check=self.id,
                severity=severity,
                kind=kind,
                message=f"the agent's own telemetry shows {went.by_agent} call{'s' if went.by_agent != 1 else ''} to "
                f"{went.host} ({went.provider}) and the proxy saw {went.through_proxy}: {went.around} went around "
                f"Minutehand to the real {went.host} (one: {went.example}), so the run did not test the world the "
                f"scenario set up. The agent's HTTP client for {went.host} ignores HTTPS_PROXY. {FIXES}",
            )
            for went in view.around_proxy or []
        ]
        if view.uncalled_providers and not findings:
            providers = ", ".join(view.uncalled_providers)
            unseen = (
                " and exported no span of an HTTP call, so its telemetry cannot say whether it made one"
                if view.around_proxy is None
                else ""
            )
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"the agent was woken {len(view.wakes)} time{'s' if len(view.wakes) != 1 else ''} and "
                    f"called nothing of {providers} through Minutehand{unseen}. If it acted on {providers}, its HTTP "
                    f"client ignores HTTPS_PROXY and reached the real service. {FIXES}",
                )
            )
        return CheckReport(findings=findings)
