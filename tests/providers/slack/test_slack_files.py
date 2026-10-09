"""Files by Slack's v2 flow, round-tripped through stock `slack_sdk` (`files_upload_v2`), the upload to the URL it is
given, and the real proxy.

Each test says whether the claim is DOCUMENTED, with the page; `CLAIMS.md` beside the provider is the table of them.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
from typing import Any

import pytest

from minutehand.adapters.providers.slack import seed, state, wire
from minutehand.adapters.providers.slack.state import BOT_USER_ID
from minutehand.domain.scenario import SeededFile
from minutehand.domain.world import Actor
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, data
from tests.providers.slack.slack_workspace import GENERAL, Workspace
from tests.providers.slack.test_slack_conversations import not_served, refusal

IRIS = state.user_id("iris")
BINARY = bytes(range(256)) + b"\x00\xff\xfe end"


async def uploaded(
    slack: Intercepted, *, name: str = "report.pdf", content: bytes = BINARY, **more: Any
) -> dict[str, Any]:
    """`files_upload_v2` as an agent calls it, and the file it answers."""
    done = data(await slack.asynchronous().files_upload_v2(filename=name, content=content, **more))
    found = done["files"]
    assert isinstance(found, list) and len(found) == 1
    return done


def someones_file(workspace: Workspace, channel: str, name: str = "theirs.txt", text: str = "their words") -> str:
    """A file a person shared in `channel` before the app looked."""
    at = int(workspace.clock.now().timestamp())
    file = seed.write_file(
        workspace.slack, SeededFile(name=name, text=text), IRIS, at, seed=f"{channel}|{name}", actor=Actor.SCENARIO
    )
    seed.write_post(workspace.slack, channel, IRIS, "A file", None, [file], at=at, actor=Actor.SCENARIO)
    return file.id


# ---------------------------------------------------------------------- the whole flow


async def test_an_uploaded_file_is_shared_in_the_channel_and_comes_back_byte_for_byte(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: the flow is `files.getUploadURLExternal`, a POST of the bytes to the URL, `files.completeUploadExternal`
    sharing it in `channel_id` with `initial_comment` as the message text; `files.info` describes it and its
    `url_private` serves what was sent. https://docs.slack.dev/reference/methods/files.getUploadURLExternal,
    https://docs.slack.dev/reference/methods/files.completeUploadExternal, https://docs.slack.dev/reference/methods/files.info"""
    sdk = slack.asynchronous()

    done = await uploaded(slack, title="Quarterly report", channel=GENERAL, initial_comment="Here it is")

    file = done["files"][0]
    assert file["title"] == "Quarterly report"
    info = data(await sdk.files_info(file=file["id"]))
    described = info["file"]
    assert (described["id"], described["name"], described["title"], described["size"]) == (
        file["id"],
        "report.pdf",
        "Quarterly report",
        len(BINARY),
    )
    assert (described["user"], described["mode"], described["mimetype"], described["filetype"]) == (
        BOT_USER_ID,
        "hosted",
        "application/pdf",
        "pdf",
    )
    assert described["channels"] == [GENERAL] and described["groups"] == [] and described["ims"] == []
    assert described["is_public"] is True and described["comments_count"] == 0
    assert (info["comments"], described["bot_id"], described["created"]) == (
        [],
        workspace.slack.team.bot_id,
        int(workspace.clock.now().timestamp()),
    )
    async with slack.http() as http:
        got = await http.get(described["url_private"], headers={"Authorization": "Bearer xoxb-1"})
    assert got.status_code == 200 and got.content == BINARY and got.headers["content-type"] == "application/pdf"
    message = data(await sdk.conversations_history(channel=GENERAL))["messages"][0]
    assert (message["subtype"], message["text"], message["user"]) == ("file_share", "Here it is", BOT_USER_ID)
    assert [f["id"] for f in message["files"]] == [file["id"]]


async def test_a_file_shared_in_no_channel_stays_private_to_the_app(slack: Intercepted) -> None:
    """DOCUMENTED: "If the `channel_id` is not specified, the file will remain private".
    https://docs.slack.dev/reference/methods/files.completeUploadExternal"""
    sdk = slack.asynchronous()

    done = await uploaded(slack, name="draft.txt", content=b"not yet")

    info = data(await sdk.files_info(file=done["files"][0]["id"]))["file"]
    assert (info["channels"], info["is_public"], info["mimetype"]) == ([], False, "text/plain")
    assert data(await sdk.conversations_history(channel=GENERAL))["messages"] == []
    assert [f["id"] for f in data(await sdk.files_list())["files"]] == [info["id"]]


async def test_bytes_posted_as_a_multipart_form_are_kept_as_the_one_file_part(slack: Intercepted) -> None:
    """DOCUMENTED: "Files can be sent as raw bytes or can be multipart form encoded"; HTTP 200 on success.
    https://docs.slack.dev/reference/methods/files.getUploadURLExternal"""
    sdk = slack.asynchronous()
    got = data(await sdk.files_getUploadURLExternal(filename="form.bin", length=len(BINARY)))
    async with slack.http() as http:
        sent = await http.post(got["upload_url"], files={"filename": ("form.bin", BINARY, "application/octet-stream")})
    assert sent.status_code == 200

    data(await sdk.files_completeUploadExternal(files=[{"id": got["file_id"], "title": "form"}]))

    async with slack.http() as http:
        again = await http.get(data(await sdk.files_info(file=got["file_id"]))["file"]["url_private"])
    assert again.content == BINARY


async def test_a_file_shared_in_several_channels_is_one_message_each_and_a_file_shared_event_each(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `channels` is "Comma-separated string of channel IDs or user IDs where the file will be shared";
    `file_shared` is sent when a file is shared, with `channel_id`, `file_id`, `user_id`, `file` and `event_ts`.
    https://docs.slack.dev/reference/methods/files.completeUploadExternal, https://docs.slack.dev/reference/events/file_shared"""
    workspace.provider.listen(agent.target(), SECRET)
    other = workspace.public_channel("elsewhere")
    sdk = slack.asynchronous()

    done = await uploaded(slack, channels=[GENERAL, other, IRIS])

    file = done["files"][0]["id"]
    info = data(await sdk.files_info(file=file))["file"]
    assert sorted(info["channels"]) == sorted([GENERAL, other]) and info["ims"] == [workspace.dm("iris")]
    for _ in range(300):
        if len(agent.received) >= 3:
            break
        await asyncio.sleep(0.02)
    events = [r.json for r in agent.received]
    assert sorted(e["event"]["channel_id"] for e in events) == sorted([GENERAL, other, workspace.dm("iris")])
    assert {
        (e["event"]["type"], e["event"]["file_id"], e["event"]["user_id"], e["event"]["file"]["id"]) for e in events
    } == {("file_shared", file, BOT_USER_ID, file)}


async def test_two_files_completed_together_are_shared_in_one_message(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `files` is an "Array of file ids and their corresponding (optional) titles", and
    `initial_comment` is "The message text introducing the file in specified channels".
    https://docs.slack.dev/reference/methods/files.completeUploadExternal"""
    workspace.provider.listen(agent.target(), SECRET)
    sdk = slack.asynchronous()
    first = data(await sdk.files_getUploadURLExternal(filename="a.txt", length=1))
    second = data(await sdk.files_getUploadURLExternal(filename="b.txt", length=2))
    async with slack.http() as http:
        await http.post(first["upload_url"], content=b"a")
        await http.post(second["upload_url"], content=b"bb")

    done = data(
        await sdk.files_completeUploadExternal(
            files=[{"id": first["file_id"]}, {"id": second["file_id"], "title": "Bee"}],
            channel_id=GENERAL,
            initial_comment="Two files",
        )
    )

    assert done["files"] == [{"id": first["file_id"], "title": "a.txt"}, {"id": second["file_id"], "title": "Bee"}]
    messages = data(await sdk.conversations_history(channel=GENERAL))["messages"]
    assert len(messages) == 1 and [f["name"] for f in messages[0]["files"]] == ["a.txt", "b.txt"]
    for _ in range(300):
        if len(agent.received) >= 2:
            break
        await asyncio.sleep(0.02)
    ids = [r.json["event_id"] for r in agent.received]
    assert len(ids) == 2 and len(set(ids)) == 2


async def test_a_file_shared_as_a_reply_is_in_the_thread(slack: Intercepted) -> None:
    """DOCUMENTED: `thread_ts` uploads the file "as a reply". https://docs.slack.dev/reference/methods/files.completeUploadExternal"""
    sdk = slack.asynchronous()
    root = data(await sdk.chat_postMessage(channel=GENERAL, text="Thread"))["ts"]

    await uploaded(slack, name="reply.txt", content=b"x", channel=GENERAL, thread_ts=root)

    replies = data(await sdk.conversations_replies(channel=GENERAL, ts=root))["messages"]
    assert replies[0]["ts"] == root and replies[1]["files"][0]["name"] == "reply.txt"
    assert replies[1]["thread_ts"] == root


# ---------------------------------------------------------------------- what is refused when asking for a URL


async def test_an_upload_url_asked_for_with_a_length_of_nothing_a_long_description_or_a_snippet_is_refused(
    slack: Intercepted,
) -> None:
    """DOCUMENTED: `missing_argument` "Typically only occurs when the `length` provided is 0"; `alt_txt_too_large`
    over 1000 characters; a `snippet_type` is refused 501 since the page lists none.
    https://docs.slack.dev/reference/methods/files.getUploadURLExternal"""
    sdk = slack.asynchronous()

    assert (await refusal(sdk.files_getUploadURLExternal(filename="a.txt", length=0)))["error"] == "missing_argument"
    assert (await refusal(sdk.files_getUploadURLExternal(filename="a.txt", length=1, alt_txt="x" * 1001)))[
        "error"
    ] == "alt_txt_too_large"
    assert data(await sdk.files_getUploadURLExternal(filename="a.txt", length=1, alt_txt="x" * 1000))["ok"] is True
    assert "snippet_type" in await not_served(
        sdk.files_getUploadURLExternal(filename="a.py", length=1, snippet_type="python")
    )
    assert (await refusal(sdk.files_getUploadURLExternal(filename="", length=1)))["error"] == "invalid_arguments"


async def test_the_url_answers_a_files_slack_com_upload_address_and_a_file_id(slack: Intercepted) -> None:
    """DOCUMENTED: `upload_url` of the form `https://files.slack.com/upload/v1/…` and a `file_id`.
    https://docs.slack.dev/reference/methods/files.getUploadURLExternal"""
    got = data(await slack.asynchronous().files_getUploadURLExternal(filename="a.txt", length=1))

    assert got["upload_url"].startswith("https://files.slack.com/upload/v1/")
    assert got["file_id"].startswith("F")


async def test_an_upload_that_is_not_the_length_asked_for_a_second_one_and_a_stranger_url_are_refused_by_name(
    slack: Intercepted,
) -> None:
    """DOCUMENTED GAP: the page says only that a non-200 response "indicates a failure" and gives no case for these;
    each is answered 501."""
    got = data(await slack.asynchronous().files_getUploadURLExternal(filename="a.txt", length=3))
    async with slack.http() as http:
        short = await http.post(got["upload_url"], content=b"ab")
        fine = await http.post(got["upload_url"], content=b"abc")
        twice = await http.post(got["upload_url"], content=b"abc")
        stranger = await http.post("https://files.slack.com/upload/v1/T0WORKSPACE-F0NOSUCH-nothing", content=b"abc")

    assert (short.status_code, fine.status_code, twice.status_code, stranger.status_code) == (501, 200, 501, 501)
    assert "issued for 3" in short.json()["response_metadata"]["messages"][0]


# ---------------------------------------------------------------------- what is refused when completing


async def test_completing_what_was_never_asked_for_or_never_uploaded_or_twice_is_refused(slack: Intercepted) -> None:
    """DOCUMENTED: `file_not_found`, "Could not find the file from the upload ticket"; "This method can only be called
    once"; completing before the bytes arrive is refused 501 since the page names no error.
    https://docs.slack.dev/reference/methods/files.completeUploadExternal"""
    sdk = slack.asynchronous()
    got = data(await sdk.files_getUploadURLExternal(filename="a.txt", length=1))

    assert (await refusal(sdk.files_completeUploadExternal(files=[{"id": "F0NOSUCH"}])))["error"] == "file_not_found"
    assert "before the bytes" in await not_served(sdk.files_completeUploadExternal(files=[{"id": got["file_id"]}]))
    async with slack.http() as http:
        await http.post(got["upload_url"], content=b"a")
    data(await sdk.files_completeUploadExternal(files=[{"id": got["file_id"]}]))
    assert "once" in await not_served(sdk.files_completeUploadExternal(files=[{"id": got["file_id"]}]))


async def test_sharing_where_the_app_cannot_is_refused_with_the_code_for_it(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `channel_not_found` for `channel_id`, `invalid_channel` for `channels`, `not_in_channel`,
    `channels_limit_exceeded` past 100 channels. https://docs.slack.dev/reference/methods/files.completeUploadExternal"""
    outside = workspace.channel_without_the_app("elsewhere", members=["iris"])

    assert (await refusal(slack.asynchronous().files_upload_v2(filename="a.txt", content=b"a", channel="C0NOSUCH")))[
        "error"
    ] == "channel_not_found"
    assert (await refusal(slack.asynchronous().files_upload_v2(filename="a.txt", content=b"a", channels=["C0NOSUCH"])))[
        "error"
    ] == "invalid_channel"
    assert (await refusal(slack.asynchronous().files_upload_v2(filename="a.txt", content=b"a", channel=outside)))[
        "error"
    ] == "not_in_channel"
    many = [f"C{n:09d}" for n in range(101)]
    assert (await refusal(slack.asynchronous().files_upload_v2(filename="a.txt", content=b"a", channels=many)))[
        "error"
    ] == "channels_limit_exceeded"


async def test_both_channel_arguments_a_customised_sender_and_a_thread_across_channels_are_refused_by_name(
    slack: Intercepted,
) -> None:
    """DOCUMENTED GAP: the page does not say what `channel_id` with `channels` does, nor `thread_ts` with other than one
    channel; `username`, `icon_url` and `icon_emoji` need `chat:write.customize`, which is not modelled."""
    sdk = slack.asynchronous()
    got = data(await sdk.files_getUploadURLExternal(filename="a.txt", length=1))
    async with slack.http() as http:
        await http.post(got["upload_url"], content=b"a")
    files = [{"id": got["file_id"]}]

    assert "both" in await not_served(
        sdk.files_completeUploadExternal(files=files, channel_id=GENERAL, channels=[GENERAL])
    )
    assert "thread_ts" in await not_served(sdk.files_completeUploadExternal(files=files, thread_ts="1.000001"))
    assert "username" in await not_served(sdk.files_completeUploadExternal(files=files, username="Bot"))


# ---------------------------------------------------------------------- info, list, delete


async def test_a_file_that_never_was_is_file_not_found_and_one_in_a_channel_the_app_is_not_in_is_not_visible(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `file_not_found`, `not_visible` ("Do not have permission to view the file"); a bot may read "files
    appearing in the channels they belong to". https://docs.slack.dev/reference/methods/files.info"""
    sdk = slack.asynchronous()
    hidden = someones_file(workspace, workspace.channel_without_the_app("elsewhere", members=["iris"]))
    seen = someones_file(workspace, GENERAL, name="seen.txt")

    assert (await refusal(sdk.files_info(file="F0NOSUCH")))["error"] == "file_not_found"
    assert (await refusal(sdk.files_info(file=hidden)))["error"] == "not_visible"
    seen_info = data(await sdk.files_info(file=seen))["file"]
    assert (seen_info["channels"], seen_info["is_public"]) == ([GENERAL], True)
    assert (await refusal(sdk.files_delete(file=hidden)))["error"] == "file_not_found"


async def test_files_list_filters_by_user_channel_type_and_time_and_pages(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `user`, `channel`, `types`, `ts_from` and `ts_to` ("inclusive") filter, `count` and `page` slice, and
    `paging` answers `count`, `total`, `page`, `pages`; the example lists the earlier-created file first.
    https://docs.slack.dev/reference/methods/files.list"""
    sdk = slack.asynchronous()
    theirs = someones_file(workspace, GENERAL, name="theirs.txt")
    workspace.clock.jump(workspace.clock.now() + timedelta(minutes=10))
    mine = (await uploaded(slack, name="pic.png", content=b"\x89PNG", channel=GENERAL))["files"][0]["id"]
    workspace.clock.jump(workspace.clock.now() + timedelta(minutes=10))
    private = (await uploaded(slack, name="notes.txt", content=b"mine"))["files"][0]["id"]
    created = {f["id"]: f["created"] for f in data(await sdk.files_list())["files"]}

    everything = data(await sdk.files_list())
    assert [f["id"] for f in everything["files"]] == [theirs, mine, private]
    assert everything["paging"] == {"count": 100, "total": 3, "page": 1, "pages": 1}
    assert [f["id"] for f in data(await sdk.files_list(user=BOT_USER_ID))["files"]] == [mine, private]
    assert [f["id"] for f in data(await sdk.files_list(channel=GENERAL))["files"]] == [theirs, mine]
    assert [f["id"] for f in data(await sdk.files_list(types="images"))["files"]] == [mine]
    assert [f["id"] for f in data(await sdk.files_list(types="pdfs,spaces"))["files"]] == []
    assert [
        f["id"] for f in data(await sdk.files_list(ts_from=str(created[mine]), ts_to=str(created[mine])))["files"]
    ] == [mine]
    second = data(await sdk.files_list(count=2, page=2))
    assert [f["id"] for f in second["files"]] == [private]
    assert second["paging"] == {"count": 2, "total": 3, "page": 2, "pages": 2}


async def test_files_list_refuses_a_type_slack_does_not_list_and_a_user_who_is_no_member(slack: Intercepted) -> None:
    """DOCUMENTED: `unknown_type`, `user_not_found`. https://docs.slack.dev/reference/methods/files.list"""
    sdk = slack.asynchronous()

    assert (await refusal(sdk.files_list(types="galaxies")))["error"] == "unknown_type"
    assert (await refusal(sdk.files_list(user="U0NOSUCH")))["error"] == "user_not_found"
    assert "show_files_hidden_by_limit" in await not_served(sdk.files_list(show_files_hidden_by_limit=True))


async def test_a_deleted_file_is_gone_from_info_list_and_its_url_and_deleting_twice_says_so(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `files.delete` deletes a file; `file_deleted` "The file has already been deleted"; `file_deleted`
    is sent to the workspace, with `file_id` and `event_ts`. https://docs.slack.dev/reference/methods/files.delete,
    https://docs.slack.dev/reference/methods/files.info, https://docs.slack.dev/reference/events/file_deleted"""
    sdk = slack.asynchronous()
    file = (await uploaded(slack, channel=GENERAL))["files"][0]["id"]
    url = data(await sdk.files_info(file=file))["file"]["url_private"]
    workspace.provider.listen(agent.target(), SECRET)

    assert data(await sdk.files_delete(file=file)) == {"ok": True}

    assert (await refusal(sdk.files_info(file=file)))["error"] == "file_deleted"
    assert (await refusal(sdk.files_delete(file=file)))["error"] == "file_deleted"
    assert data(await sdk.files_list())["files"] == []
    async with slack.http() as http:
        assert (await http.get(url)).status_code == 404
    for _ in range(300):
        if agent.received:
            break
        await asyncio.sleep(0.02)
    event = agent.received[0].json["event"]
    assert (event["type"], event["file_id"]) == ("file_deleted", file) and event["event_ts"]
    assert workspace.slack.body(state.content_ref(file), wire.SlackFileContent) is None


async def test_a_file_someone_else_uploaded_cannot_be_deleted(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `cant_delete_file`, "Authenticated user does not have permission to delete this file".
    https://docs.slack.dev/reference/methods/files.delete"""
    theirs = someones_file(workspace, GENERAL)

    answer = await refusal(slack.asynchronous().files_delete(file=theirs))

    assert answer["error"] == "cant_delete_file"
    assert data(await slack.asynchronous().files_info(file=theirs))["ok"] is True


@pytest.mark.parametrize(("argument", "value"), [("count", 5), ("page", 2)])
async def test_the_legacy_paging_of_files_info_is_refused_by_name_off_its_default(
    slack: Intercepted, argument: str, value: int
) -> None:
    """DOCUMENTED GAP: `files.info` pages its file comments, and the world holds none."""
    file = (await uploaded(slack))["files"][0]["id"]

    said = await not_served(slack.asynchronous().api_call("files.info", params={"file": file, argument: value}))

    assert argument in said


def test_the_files_the_scenario_seeds_keep_their_text_as_the_bytes_that_are_served(workspace: Workspace) -> None:
    """The seeded `text` of a file is stored as its UTF-8 bytes (`SlackFileContent.encoded`)."""
    file = someones_file(workspace, GENERAL, text="héllo wörld")

    content = workspace.slack.body(state.content_ref(file), wire.SlackFileContent)

    assert content is not None
    assert base64.b64decode(content.encoded) == "héllo wörld".encode()
