"""The webhooks GitHub sends the agent for what happens to issues, comments, pull requests and reviews.

A person's move (closing an issue, merging or reviewing a pull request) is pushed to the agent's inbound target for
GitHub, one POST per event, with the headers the reference lists
(https://docs.github.com/en/webhooks/webhook-events-and-payloads#delivery-headers): `X-GitHub-Event`,
`X-GitHub-Delivery` (a GUID), `User-Agent` (prefixed `GitHub-Hookshot/`), `Content-Type: application/json`, and, only
when the world declares a secret for the target, `X-Hub-Signature-256` (the HMAC hex digest of the body under the
secret, SHA-256) and the older `X-Hub-Signature` (the same under SHA-1). The webhook a repository configures has an id
and a target of its own, which the world does not hold, so `X-GitHub-Hook-ID` and the installation target headers are
not sent.

GitHub does not send a delivery again when it fails, and waits ten seconds for the answer
(https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries); an agent that cannot be
reached, or answers anything but 2xx, has failed its run.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from pydantic import JsonValue

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.answers import Caller
from minutehand.application.refusals import AgentFailed
from minutehand.domain.people import Delivery, InboundTarget
from minutehand.domain.world import Actor

if TYPE_CHECKING:
    from minutehand.adapters.providers.github.app import GitHubApi

TIMEOUT = 10.0
"""GitHub waits this long, in seconds, for the answer to a delivery."""
HOOKSHOT = "GitHub-Hookshot/044aadd"
"""A `User-Agent` of the form the reference shows: its prefix is what it promises."""
FIRST_DELIVERY = 1


class DeliveryRefused(AgentFailed):
    """The agent answered a webhook with something other than 2xx, or could not be reached for it."""

    def __init__(self, url: str, status: int | None, text: str, event: str) -> None:
        answered = f"answered {status}" if status is not None else "could not be reached"
        super().__init__(f"{url} {answered} to a GitHub {event} webhook: {text[:200]}")
        self.status = status


def signature(algorithm: str, secret: str, body: bytes) -> str:
    """`X-Hub-Signature-256` is `sha256=` and the HMAC hex digest of the body under the secret; `X-Hub-Signature` the
    same with `sha1=`."""
    digest = hashlib.sha256 if algorithm == "sha256" else hashlib.sha1
    return f"{algorithm}=" + hmac.new(secret.encode(), body, digest).hexdigest()


@dataclass(frozen=True)
class Pusher:
    """Where webhooks go: the agent's inbound target for GitHub, and the secret that signs them when the world
    declares one for it."""

    target: InboundTarget
    secret: str | None

    def __post_init__(self) -> None:
        if self.target.delivery is not Delivery.REQUEST_URL:
            raise ValueError("GitHub pushes a webhook to a URL; the target is taken in another way")

    async def send(self, event: str, body: wire.WebhookBody, delivery: str) -> None:
        encoded = body.encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": HOOKSHOT,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
        }
        if self.secret:
            headers["X-Hub-Signature"] = signature("sha1", self.secret, encoded)
            headers["X-Hub-Signature-256"] = signature("sha256", self.secret, encoded)
        url = self.target.request_url()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
                answered = await client.post(url, content=encoded, headers=headers)
        except httpx.HTTPError as error:
            raise DeliveryRefused(url, None, repr(error), event) from error
        if not answered.is_success:
            raise DeliveryRefused(url, answered.status_code, answered.text, event)


class Hooks:
    """What the provider pushes, to the agent's target when there is one: nothing, where the call came through the
    API and the world holds no target to push to."""

    def __init__(self, api: GitHubApi, pusher: Pusher | None) -> None:
        self._api = api
        self._pusher = pusher

    def _delivery(self) -> str:
        """A GUID for one delivery, the same in every replay of the run: made from the run and a count of deliveries."""
        world = self._api.world
        count = world.next_id("delivery", first=FIRST_DELIVERY, actor=Actor.SYSTEM)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"github-delivery/{world.store.run_id}/{count}"))

    async def _push(self, event: str, parts: dict[str, JsonValue]) -> None:
        assert self._pusher is not None
        await self._pusher.send(event, wire.WebhookBody(keys=parts), self._delivery())

    def _common(self, repository: wire.StoredRepository, sender: wire.StoredAccount) -> tuple[JsonValue, JsonValue]:
        shown = self._api.repository_out(Caller(account=sender, token=None), repository)
        return wire.as_value(shown), wire.as_value(self._api.account_out(sender.login))

    def _issue(self, repository: wire.StoredRepository, issue: wire.StoredIssue) -> JsonValue:
        """The issue as a webhook carries it: the REST answer and the two keys a webhook's schema requires besides,
        a null `performed_via_github_app` and the rollup of reactions (none)."""
        shown = wire.as_value(self._api.tracker.present(repository, [issue])[0])
        assert isinstance(shown, dict)
        url = f"{wire.API}/repos/{repository.full_name}/issues/{issue.number}/reactions"
        return {**shown, "performed_via_github_app": None, "reactions": wire.as_value(wire.ReactionsOut(url=url))}

    def _pull(self, caller: Caller, repository: wire.StoredRepository, issue: wire.StoredIssue) -> JsonValue:
        return wire.as_value(self._api.pulls.present(caller, repository, [issue], full=True)[0])

    async def state_changed(
        self,
        repository: wire.StoredRepository,
        before: wire.StoredIssue,
        after: wire.StoredIssue,
        sender: wire.StoredAccount,
    ) -> None:
        """An issue or pull request closed or reopened: an `issues` event, or a `pull_request` event whose pull
        request says whether it was merged."""
        if self._pusher is None or after.state is before.state:
            return
        action = "closed" if after.state is wire.IssueState.CLOSED else "reopened"
        shown, who = self._common(repository, sender)
        if after.pull is None:
            await self._push(
                "issues",
                {"action": action, "issue": self._issue(repository, after), "repository": shown, "sender": who},
            )
            return
        pull = self._pull(Caller(account=sender, token=None), repository, after)
        await self._push(
            "pull_request",
            {"action": action, "number": after.number, "pull_request": pull, "repository": shown, "sender": who},
        )

    async def comment_created(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        comment: wire.StoredComment,
        sender: wire.StoredAccount,
    ) -> None:
        """A comment on an issue or pull request: an `issue_comment` event."""
        if self._pusher is None:
            return
        shown, who = self._common(repository, sender)
        written = wire.as_value(self._api.tracker.comment_out(repository, comment))
        assert isinstance(written, dict)
        url = f"{wire.API}/repos/{repository.full_name}/issues/comments/{comment.id}/reactions"
        written = {**written, "performed_via_github_app": None, "reactions": wire.as_value(wire.ReactionsOut(url=url))}
        await self._push(
            "issue_comment",
            {
                "action": "created",
                "issue": self._issue(repository, issue),
                "comment": written,
                "repository": shown,
                "sender": who,
            },
        )

    async def review_submitted(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        review: wire.StoredReview,
        sender: wire.StoredAccount,
    ) -> None:
        """A review of a pull request: a `pull_request_review` event."""
        if self._pusher is None:
            return
        shown, who = self._common(repository, sender)
        await self._push(
            "pull_request_review",
            {
                "action": "submitted",
                "review": wire.as_value(self._api.pulls.review_out(repository, review)),
                "pull_request": self._pull(Caller(account=sender, token=None), repository, issue),
                "repository": shown,
                "sender": who,
            },
        )
