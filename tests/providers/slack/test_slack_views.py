"""Modals as a stack: `views.push`, `views.update` on a pushed view, and what a submission does to the stack.

Each test says whether the claim is DOCUMENTED, with the page; `CLAIMS.md` beside the provider is the table of them.
A trigger is issued the way an interaction hands one to the app: a record in the world, fresh at the run's start.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from starlette.responses import JSONResponse, Response

from minutehand.adapters.providers.slack import interactive, state, wire
from minutehand.adapters.providers.slack.app import open_stack
from minutehand.adapters.providers.slack.interactive import DeliveryRefused
from minutehand.domain.scenario import FormInput
from minutehand.domain.world import Actor, EntityKind, Operation
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, Received, data
from tests.providers.slack.slack_workspace import START, Workspace
from tests.providers.slack.test_slack_conversations import not_served, refusal
from tests.providers.slack.test_slack_interactions import REJECT_MODAL, card_to_tomas, tomas_presses

IRIS = state.user_id("iris")


def modal(title: str = "A modal", **more: Any) -> dict[str, Any]:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}],
        **more,
    }


def trigger(workspace: Workspace, ident: str, *, in_view: str | None = None, issued: int | None = None) -> str:
    """A trigger as an interaction hands one to the app, fresh at the run's start (or `issued`), from `in_view`."""
    workspace.slack.write(
        state.trigger_ref(ident),
        wire.SlackTrigger(
            id=ident, user=IRIS, issued=int(START.timestamp()) if issued is None else issued, in_view=in_view
        ),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=state.TRIGGERS,
    )
    return ident


async def opened(slack: Intercepted, workspace: Workspace, ident: str = "open.1", **more: Any) -> str:
    trigger(workspace, ident)
    shown = data(await slack.asynchronous().views_open(trigger_id=ident, view=modal("Root", **more)))
    return str(shown["view"]["id"])


async def pushed(slack: Intercepted, workspace: Workspace, below: str, ident: str, title: str) -> dict[str, Any]:
    trigger(workspace, ident, in_view=below)
    return data(await slack.asynchronous().views_push(trigger_id=ident, view=modal(title)))["view"]


def stack(workspace: Workspace, root: str) -> list[str]:
    return [v.view.id for v in open_stack(workspace.slack, root)]


async def test_a_pushed_view_goes_on_top_of_the_view_the_interaction_was_in(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `views.push` adds a view to the top of the modal's stack and answers it, with the `root_view_id`
    of the modal and the `previous_view_id` of the view below. https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace)

    top = await pushed(slack, workspace, root, "push.1", "Pushed Modal")

    assert (top["type"], top["title"]["text"], top["root_view_id"], top["previous_view_id"]) == (
        "modal",
        "Pushed Modal",
        root,
        root,
    )
    assert top["id"] != root and top["hash"] and top["app_id"] == workspace.slack.team.app_id
    assert stack(workspace, root) == [root, top["id"]]


async def test_a_trigger_pushes_once(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `exchanged_trigger_id`, "The trigger_id was already exchanged in a previous call".
    https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace)
    await pushed(slack, workspace, root, "push.1", "One")

    answer = await refusal(slack.asynchronous().views_push(trigger_id="push.1", view=modal("Two")))

    assert answer["error"] == "exchanged_trigger_id"


async def test_a_modal_holds_three_views_and_a_fourth_is_push_limit_reached(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: "Three views can exist in the view stack at any one time"; `push_limit_reached`, "Currently the
    limit is 3". https://docs.slack.dev/surfaces/modals, https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace)
    second = await pushed(slack, workspace, root, "push.1", "Two")
    third = await pushed(slack, workspace, second["id"], "push.2", "Three")
    trigger(workspace, "push.3", in_view=third["id"])

    answer = await refusal(slack.asynchronous().views_push(trigger_id="push.3", view=modal("Four")))

    assert answer["error"] == "push_limit_reached"
    assert stack(workspace, root) == [root, second["id"], third["id"]]


async def test_a_pushed_view_can_be_updated_and_keeps_its_place_in_the_stack(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `views.update` takes the `view_id` of any view; the view stays where it is.
    https://docs.slack.dev/reference/methods/views.update"""
    root = await opened(slack, workspace)
    top = await pushed(slack, workspace, root, "push.1", "Pushed")

    updated = data(await slack.asynchronous().views_update(view_id=top["id"], view=modal("Pushed again")))["view"]

    assert (updated["title"]["text"], updated["previous_view_id"], updated["root_view_id"]) == (
        "Pushed again",
        root,
        root,
    )
    assert updated["hash"] != top["hash"] and stack(workspace, root) == [root, top["id"]]


@pytest.mark.parametrize(
    ("ident", "code"),
    [("push.never", "invalid_trigger_id"), ("", "invalid_arguments")],
)
async def test_a_push_with_a_trigger_nobody_issued_or_none_is_refused(
    slack: Intercepted, workspace: Workspace, ident: str, code: str
) -> None:
    """DOCUMENTED: `invalid_trigger_id`; `invalid_arguments`. https://docs.slack.dev/reference/methods/views.push"""
    await opened(slack, workspace)

    answer = await refusal(slack.asynchronous().views_push(trigger_id=ident, view=modal("x")))

    assert answer["error"] == code


async def test_a_trigger_older_than_three_seconds_is_refused_expired_trigger_id(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `expired_trigger_id`; a trigger lives three seconds.
    https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace)
    trigger(workspace, "push.late", in_view=root)
    workspace.clock.jump(workspace.clock.now() + timedelta(seconds=4))

    answer = await refusal(slack.asynchronous().views_push(trigger_id="push.late", view=modal("x")))

    assert answer["error"] == "expired_trigger_id"


async def test_a_push_onto_a_view_that_was_closed_is_refused_not_found(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `not_found`, "the requested view can't be found". https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace)
    trigger(workspace, "push.1", in_view=root)
    shown = workspace.slack.body(state.view_ref(root), wire.OpenView)
    assert shown is not None
    workspace.slack.write(
        state.view_ref(root),
        shown.model_copy(update={"open": False}),
        operation=Operation.UPDATE,
        actor=Actor.AGENT,
        parent=state.VIEWS,
    )

    answer = await refusal(slack.asynchronous().views_push(trigger_id="push.1", view=modal("x")))

    assert answer["error"] == "not_found"


async def test_a_pushed_view_cannot_take_an_external_id_in_use_or_be_a_home_tab(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `duplicate_external_id`; `invalid_arguments` for a view that is not a modal.
    https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace, external_id="taken")
    trigger(workspace, "push.1", in_view=root)
    trigger(workspace, "push.2", in_view=root)
    sdk = slack.asynchronous()

    assert (await refusal(sdk.views_push(trigger_id="push.1", view=modal("x", external_id="taken"))))[
        "error"
    ] == "duplicate_external_id"
    assert (await refusal(sdk.views_push(trigger_id="push.2", view={"type": "home", "blocks": []})))[
        "error"
    ] == "invalid_arguments"


def view_of(size: int) -> dict[str, Any]:
    """A modal of about `size` bytes once the fake has read it."""

    def blocks(n: int) -> list[dict[str, Any]]:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * n}} for _ in range(100)]

    base = len(wire.ViewSpec.model_validate(modal("Big", blocks=blocks(0))).model_dump_json())
    return modal("Big", blocks=blocks((size - base) // 100))


async def test_a_view_of_more_than_two_hundred_and_fifty_kilobytes_is_too_large(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `view_too_large`, "greater than 250kb"; between 250,000 and 256,000 bytes depends on what a
    kilobyte is, which no page says, so it is refused 501. https://docs.slack.dev/reference/methods/views.update,
    https://docs.slack.dev/reference/methods/views.push"""
    root = await opened(slack, workspace)
    trigger(workspace, "push.1", in_view=root)
    sdk = slack.asynchronous()

    assert (await refusal(sdk.views_update(view_id=root, view=view_of(300_000))))["error"] == "view_too_large"
    assert (await refusal(sdk.views_push(trigger_id="push.1", view=view_of(300_000))))["error"] == "view_too_large"
    assert "kilobyte" in await not_served(sdk.views_update(view_id=root, view=view_of(253_000)))
    assert data(await sdk.views_update(view_id=root, view=view_of(240_000)))["ok"] is True


async def test_a_push_from_an_interaction_outside_a_modal_or_from_a_view_below_the_top_is_refused_by_name(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED GAP: the page says the trigger comes from an interaction within the modal and names no error for a
    trigger from a message, nor for a view that is not the top of its stack."""
    root = await opened(slack, workspace)
    await pushed(slack, workspace, root, "push.1", "Two")
    trigger(workspace, "outside")
    trigger(workspace, "below", in_view=root)
    sdk = slack.asynchronous()

    assert "not in a modal" in await not_served(sdk.views_push(trigger_id="outside", view=modal("x")))
    assert "top of its stack" in await not_served(sdk.views_push(trigger_id="below", view=modal("x")))


async def test_an_interactivity_pointer_is_refused_by_name(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED GAP: the page offers `interactivity_pointer` beside `trigger_id`; the fake serves triggers."""
    root = await opened(slack, workspace)
    trigger(workspace, "push.1", in_view=root)

    said = await not_served(
        slack.asynchronous().views_push(trigger_id="push.1", view=modal("x"), interactivity_pointer="abc")
    )

    assert "interactivity_pointer" in said


# ---------------------------------------------------------------------- update keeps what was entered


async def test_an_update_keeps_what_was_entered_in_the_inputs_it_holds_again(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: "Data entered or selected in `input` blocks can be preserved while updating views": the new view
    holds "the same input blocks and elements with identical `block_id` and `action_id` values".
    https://docs.slack.dev/reference/methods/views.update"""
    form = {
        "type": "input",
        "block_id": "why",
        "label": {"type": "plain_text", "text": "Why?"},
        "element": {"type": "plain_text_input", "action_id": "reason"},
    }
    root = await opened(slack, workspace, blocks=[form])
    shown = workspace.slack.body(state.view_ref(root), wire.OpenView)
    assert shown is not None
    entered = wire.ViewState(values={"why": {"reason": wire.ViewInputValue(type="plain_text_input", value="Too dear")}})
    workspace.slack.write(
        state.view_ref(root),
        shown.model_copy(update={"view": shown.view.model_copy(update={"state": entered})}),
        operation=Operation.UPDATE,
        actor=Actor.PERSON,
        parent=state.VIEWS,
    )
    sdk = slack.asynchronous()

    same = data(await sdk.views_update(view_id=root, view=modal("Root", blocks=[form])))["view"]
    renamed = {**form, "element": {"type": "plain_text_input", "action_id": "other"}}
    other = data(await sdk.views_update(view_id=root, view=modal("Root", blocks=[renamed])))["view"]

    assert same["state"]["values"] == {"why": {"reason": {"type": "plain_text_input", "value": "Too dear"}}}
    assert other["state"]["values"] == {}


# ---------------------------------------------------------------------- a submission and the stack


async def stacked_by_a_submission(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint, last: Response
) -> tuple[str, wire.OpenView, Any]:
    """A press whose modal is filled and submitted, the agent answering that with a push; then the second view is
    submitted, the agent answering `last`. Answers the root's id, the second view, and the person's reply."""
    answers = [JSONResponse({"response_action": "push", "view": modal("Second")}), last]

    async def answer(got: Received) -> Response:
        payload = got.payload
        if payload["type"] == "block_actions":
            await slack.asynchronous().views_open(trigger_id=payload["trigger_id"], view=REJECT_MODAL)
            return Response(status_code=200)
        return answers.pop(0)

    agent.answer = answer
    asked = await card_to_tomas(slack, workspace)
    reply = tomas_presses(asked, "Reject", form=[FormInput(value="no")])
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)
    everything = workspace.slack.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView)
    [root] = [v.view.id for v in everything if v.view.previous_view_id is None]
    [second] = [v for v in everything if v.view.previous_view_id == root]
    assert stack(workspace, root) == [root, second.view.id]
    return root, second, reply


async def submit_top(workspace: Workspace, agent: AgentEndpoint, top: wire.OpenView, reply: Any) -> None:
    await interactive.submit(
        workspace.slack,
        workspace.store,
        top,
        [],
        "tomas",
        state.user_id("tomas"),
        reply,
        agent.target(),
        workspace.clock,
        SECRET,
    )


async def test_a_push_in_answer_to_a_submission_stacks_a_view_on_the_submitted_one(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `response_action` `push` adds a view on top of the submitted view, which stays in the stack.
    https://docs.slack.dev/surfaces/modals ("Add a new view via response_action")"""
    root, second, _ = await stacked_by_a_submission(slack, workspace, agent, Response(status_code=200))

    assert second.view.root_view_id == root and second.view.title is not None and second.view.title.text == "Second"


async def test_an_empty_answer_closes_only_the_submitted_view_and_the_one_below_is_shown_again(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: an empty acknowledgement "will immediately close the submitted view and remove it from the view
    stack"; the next view down is displayed. https://docs.slack.dev/surfaces/modals ("Closing a view")"""
    root, second, reply = await stacked_by_a_submission(slack, workspace, agent, Response(status_code=200))

    await submit_top(workspace, agent, second, reply)

    assert stack(workspace, root) == [root]


async def test_clearing_closes_every_view_of_the_stack(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `response_action` `clear` empties the stack "regardless of the number of views in the stack".
    https://docs.slack.dev/surfaces/modals ("Closing a view")"""
    root, second, reply = await stacked_by_a_submission(
        slack, workspace, agent, JSONResponse({"response_action": "clear"})
    )

    await submit_top(workspace, agent, second, reply)

    assert stack(workspace, root) == []


async def test_a_push_onto_a_full_stack_in_answer_to_a_submission_fails_the_agent(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: "Three views can exist in the view stack at any one time"; a push past that is not answered as a
    view, so the answer is refused. https://docs.slack.dev/surfaces/modals"""
    root, second, reply = await stacked_by_a_submission(
        slack, workspace, agent, JSONResponse({"response_action": "push", "view": modal("Third")})
    )
    await submit_top(workspace, agent, second, reply)
    everything = workspace.slack.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView)
    [third] = [v for v in everything if v.view.previous_view_id == second.view.id]
    agent.answer = _push_again

    with pytest.raises(DeliveryRefused, match="holds 3 views already"):
        await submit_top(workspace, agent, third, reply)

    assert stack(workspace, root) == [root, second.view.id, third.view.id]


async def _push_again(got: Received) -> Response:
    return JSONResponse({"response_action": "push", "view": modal("Fourth")})


async def test_a_trigger_from_a_submission_pushes_a_view(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `views.push` takes "a valid `trigger_id` generated from an interaction within the existing modal",
    and a submission is one. https://docs.slack.dev/reference/methods/views.push"""
    seen: list[str] = []

    async def answer(got: Received) -> Response:
        payload = got.payload
        if payload["type"] == "block_actions":
            await slack.asynchronous().views_open(trigger_id=payload["trigger_id"], view=REJECT_MODAL)
        else:
            seen.append(payload["trigger_id"])
            await slack.asynchronous().views_push(trigger_id=payload["trigger_id"], view=modal("Pushed by the agent"))
            return JSONResponse({"response_action": "errors", "errors": {"rejection_reason_block": "Say more."}})
        return Response(status_code=200)

    agent.answer = answer
    asked = await card_to_tomas(slack, workspace)
    reply = tomas_presses(asked, "Reject", form=[FormInput(value="no")])

    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)

    views = workspace.slack.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView)
    [top] = [v for v in views if v.view.previous_view_id is not None]
    assert top.view.title is not None and top.view.title.text == "Pushed by the agent"
    assert top.trigger_id == seen[0]
    with pytest.raises(SlackApiError) as again:
        await slack.asynchronous().views_push(trigger_id=seen[0], view=modal("Twice"))
    assert again.value.response["error"] == "exchanged_trigger_id"
