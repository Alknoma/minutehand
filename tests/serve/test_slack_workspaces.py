"""Slack's team, bot user and bot token belong to a world, so two worlds are two workspaces; one world can hold one
agent installed in two workspaces; a person's account is deactivated and reactivated while the world is open; and an
absence shows as the person's status, presence and do-not-disturb while it lasts."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import Claims, CreateWorld, Inbound
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot
from minutehand.testing.client import Refused, Unsupported
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, dm, event_receiver

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
SILENT = {"kind": "silent"}


def _people(*keys: str) -> list[dict[str, object]]:
    return [{"key": k, "name": k.title(), "email": f"{k}@example.com", "reply": SILENT} for k in keys]


def _seed(people: list[dict[str, object]], slack: dict[str, object] | None = None) -> Seed:
    found: dict[str, object] = {"starts_at": START.isoformat(), "people": people}
    if slack is not None:
        found["provider_seeds"] = [{"provider": "slack", "body": slack}]
    return Seed.model_validate(found)


def _workspace(team: str, bot: str, token: str, domain: str, **more: object) -> dict[str, object]:
    return {"team_id": team, "bot_user_id": bot, "bot_id": "B" + team[1:], "domain": domain, "tokens": [token], **more}


def _open(served: Served, seed: Seed, tokens: list[str], inbound: str | None = None) -> OpenWorld:
    spec = CreateWorld(
        seed=seed,
        claims=Claims(tokens=tokens),
        inbound=[Inbound(provider="slack", url=inbound, secret="s")] if inbound is not None else [],
    )
    return OpenWorld(served.client, served.client.create_world(spec))


def test_two_worlds_with_their_own_teams_and_tokens_are_separate_when_hit_at_once(served: Served) -> None:
    alpha = _open(served, _seed(_people("owen", "sofia"), {"workspaces": [
        _workspace("TALPHA", "UALPHABOT", "xoxb-alpha-1", "alpha")]}), ["xoxb-alpha-1"])  # fmt: skip
    beta = _open(served, _seed(_people("owen", "sofia"), {"workspaces": [
        _workspace("TBETA", "UBETABOT", "xoxb-beta-1", "beta")]}), ["xoxb-beta-1"])  # fmt: skip
    seen: dict[str, dict[str, Any]] = {}
    failed: list[BaseException] = []

    def use(token: str) -> None:
        try:
            slack = served.slack(token)
            auth = answer(slack.auth_test())
            users = answer(slack.users_list())["members"]
            channel = dm(served, token, "sofia@example.com")
            for n in range(5):
                answer(slack.chat_postMessage(channel=channel, text=f"{token} message {n}"))
            history = answer(slack.conversations_history(channel=channel))["messages"]
            seen[token] = {"auth": auth, "users": users, "channel": channel, "history": history}
        except BaseException as e:
            failed.append(e)

    try:
        threads = [threading.Thread(target=use, args=(t,)) for t in ("xoxb-alpha-1", "xoxb-beta-1")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not failed, failed
        a, b = seen["xoxb-alpha-1"], seen["xoxb-beta-1"]
        assert (a["auth"]["team_id"], a["auth"]["user_id"]) == ("TALPHA", "UALPHABOT")
        assert (b["auth"]["team_id"], b["auth"]["user_id"]) == ("TBETA", "UBETABOT")
        assert {u["team_id"] for u in a["users"]} == {"TALPHA"} and {u["team_id"] for u in b["users"]} == {"TBETA"}
        assert not {u["id"] for u in a["users"]} & {u["id"] for u in b["users"]}
        assert a["channel"] != b["channel"]
        assert {m["text"].split()[0] for m in a["history"]} == {"xoxb-alpha-1"}
        assert {m["team"] for m in b["history"]} == {"TBETA"}
        texts = [
            e.after.text
            for e in alpha.events(provider="slack", actor=Actor.AGENT)
            if isinstance(e.after, MessageSnapshot)
        ]
        assert len(texts) == 5 and all(t.startswith("xoxb-alpha-1") for t in texts)
    finally:
        served.client.close_world(alpha.world_id)
        served.client.close_world(beta.world_id)


def test_a_token_no_workspace_of_the_world_declares_is_refused(served: Served) -> None:
    world = _open(served, _seed(_people("owen"), {"workspaces": [
        _workspace("TGAMMA", "UGAMMABOT", "xoxb-gamma-1", "gamma")]}), ["xoxb-gamma-1", "xoxb-gamma-stranger"])  # fmt: skip
    try:
        with pytest.raises(SlackApiError) as refused:
            served.slack("xoxb-gamma-stranger").auth_test()
        assert refused.value.response["error"] == "invalid_auth"
    finally:
        served.client.close_world(world.world_id)


def test_one_agent_installed_in_two_workspaces_of_one_world(served: Served) -> None:
    seed = _seed(
        _people("owen", "sofia"),
        {"workspaces": [
            _workspace("TONE", "UONEBOT", "xoxb-one", "one", members=["owen"]),
            _workspace("TTWO", "UTWOBOT", "xoxb-two", "two", oauth_code="code-for-two"),
        ]},
    )  # fmt: skip
    with event_receiver() as receiver:
        world = _open(served, seed, ["xoxb-one", "xoxb-two", "client-of-two-workspaces"], inbound=receiver.url)
        try:
            one, two = served.slack("xoxb-one"), served.slack("xoxb-two")
            assert answer(one.auth_test())["team_id"] == "TONE" and answer(two.auth_test())["team_id"] == "TTWO"
            in_one = {u["name"]: u["id"] for u in answer(one.users_list())["members"]}
            in_two = {u["name"]: u["id"] for u in answer(two.users_list())["members"]}
            assert set(in_one) == {"agent", "owen"} and set(in_two) == {"agent", "owen", "sofia"}
            assert in_one["owen"] != in_two["owen"]
            with pytest.raises(SlackApiError) as elsewhere:
                one.users_lookupByEmail(email="sofia@example.com")
            assert elsewhere.value.response["error"] == "users_not_found"
            one_general = {c["id"] for c in answer(one.conversations_list())["channels"]}
            two_general = {c["id"] for c in answer(two.conversations_list())["channels"]}
            assert one_general and two_general and not one_general & two_general

            world.say("sofia", "only in two")
            world.say("owen", "first workspace")
            pushed = [(p.body, p.timestamp) for p in receiver.pushed]
            envelopes = [json.loads(b) for b, _ in pushed]
            assert [(e["team_id"], e["event"]["text"]) for e in envelopes] == [
                ("TTWO", "only in two"),
                ("TONE", "first workspace"),
            ]
            assert envelopes[0]["authorizations"][0]["user_id"] == "UTWOBOT"

            installed = answer(
                two.oauth_v2_access(client_id="client-of-two-workspaces", client_secret="s", code="code-for-two")
            )
            assert installed["team"]["id"] == "TTWO" and installed["bot_user_id"] == "UTWOBOT"
            minted = served.slack(installed["access_token"])
            assert answer(minted.auth_test())["team_id"] == "TTWO"
        finally:
            served.client.close_world(world.world_id)


def test_a_person_deactivated_and_reactivated_while_the_world_is_open(served: Served) -> None:
    world = _open(served, _seed(_people("owen", "sofia")), ["xoxb-deactivate"])
    try:
        slack = served.slack("xoxb-deactivate")
        sofia = answer(slack.users_lookupByEmail(email="sofia@example.com"))["user"]["id"]
        event = world.deactivate_person("slack", "sofia")
        assert event.actor is Actor.SCENARIO
        assert answer(slack.users_info(user=sofia))["user"]["deleted"] is True
        with pytest.raises(SlackApiError) as disabled:
            slack.conversations_open(users=[sofia])
        assert disabled.value.response["error"] == "user_disabled"
        world.reactivate_person("slack", "sofia")
        assert answer(slack.users_info(user=sofia))["user"]["deleted"] is False
        assert answer(slack.conversations_open(users=[sofia]))["ok"] is True
    finally:
        served.client.close_world(world.world_id)


def test_a_person_deactivated_twice_is_refused(served: Served) -> None:
    world = _open(served, _seed(_people("owen", "sofia")), ["xoxb-twice"])
    try:
        world.deactivate_person("slack", "sofia")
        with pytest.raises(Refused) as refused:
            world.deactivate_person("slack", "sofia")
        assert refused.value.status == 409 and "already deactivated" in refused.value.error
    finally:
        served.client.close_world(world.world_id)


def test_removing_a_slack_account_is_refused_as_unsupported(served: Served) -> None:
    world = _open(served, _seed(_people("owen", "sofia")), ["xoxb-remove"])
    try:
        with pytest.raises(Unsupported) as refused:
            world.remove_person("slack", "sofia")
        assert refused.value.status == 409 and "deactivated" in refused.value.error
    finally:
        served.client.close_world(world.world_id)


def _status(served: Served, token: str, user: str) -> tuple[str, str | None, bool]:
    slack = served.slack(token)
    presence = answer(slack.users_getPresence(user=user))["presence"]
    profile = answer(slack.users_profile_get(user=user))["profile"]
    dnd = answer(slack.api_call("dnd.info", params={"user": user}))
    return presence, profile.get("status_text"), dnd["dnd_enabled"]


def test_an_absence_shows_as_status_presence_and_do_not_disturb_only_while_it_lasts(served: Served) -> None:
    people = _people("owen", "sofia")
    people[1]["absences"] = [{"starts_after": "PT2H", "lasts": "P1D", "reason": "Off-site training"}]
    world = _open(served, _seed(people), ["xoxb-away"])
    try:
        sofia = answer(served.slack("xoxb-away").users_lookupByEmail(email="sofia@example.com"))["user"]["id"]
        assert _status(served, "xoxb-away", sofia) == ("active", None, False)
        world.advance(by=timedelta(hours=3))
        assert _status(served, "xoxb-away", sofia) == ("away", "Off-site training", True)
        info = answer(served.slack("xoxb-away").users_info(user=sofia))["user"]["profile"]
        ends = int((START + timedelta(hours=2, days=1)).timestamp())
        assert (info["status_emoji"], info["status_expiration"]) == (":palm_tree:", ends)
        world.advance(by=timedelta(days=1))
        assert _status(served, "xoxb-away", sofia) == ("active", None, False)
    finally:
        served.client.close_world(world.world_id)


def test_an_absence_that_starts_on_the_first_ask_shows_once_the_agent_asks(served: Served) -> None:
    people = _people("owen", "alba")
    people[1]["absences"] = [{"trigger": "on_first_ask", "lasts": "P2D"}]
    world = _open(served, _seed(people), ["xoxb-first-ask"])
    try:
        slack = served.slack("xoxb-first-ask")
        alba = answer(slack.users_lookupByEmail(email="alba@example.com"))["user"]["id"]
        assert _status(served, "xoxb-first-ask", alba)[0] == "active"
        answer(slack.chat_postMessage(channel=dm(served, "xoxb-first-ask", "alba@example.com"), text="Can you?"))
        assert _status(served, "xoxb-first-ask", alba) == ("away", "Away", True)
        assert any(e.entity.kind is EntityKind.MESSAGE for e in world.events(provider="slack", actor=Actor.AGENT))
    finally:
        served.client.close_world(world.world_id)
