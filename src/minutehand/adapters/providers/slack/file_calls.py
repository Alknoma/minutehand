"""Files, by Slack's v2 flow: `files.getUploadURLExternal`, the upload to the URL it answers, `files.completeUploadExternal`,
and `files.info`, `files.list` and `files.delete`.

The bytes uploaded are kept exactly as sent and served unchanged at the file's `url_private`. Each method is Slack's as
its page documents it (`CLAIMS.md` lists the claims and their tests); what a page leaves open is refused 501 by name.
"""

from __future__ import annotations

import base64
import math
import mimetypes

from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.message_calls import MessageCalls
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor, DocumentSnapshot, Operation

MAX_ALT_TEXT = 1000
"""`alt_txt_too_large`: "longer than the limit of 1000 character" (https://docs.slack.dev/reference/methods/files.getUploadURLExternal)."""
MAX_SHARED_AT_ONCE = 100
"""`channels_limit_exceeded`: "A maximum of 100 channels is allowed per request"
(https://docs.slack.dev/reference/methods/files.completeUploadExternal)."""
FILE_TYPES = frozenset({"all", "spaces", "snippets", "images", "gdocs", "zips", "pdfs"})
"""The values of `types` that https://docs.slack.dev/reference/methods/files.list lists."""
UNKNOWN_MIMETYPE = "application/octet-stream"
MULTIPART = "multipart/form-data"


def mimetype_of(filename: str) -> str:
    """No page says how Slack decides a file's mimetype; the filename's extension decides it here."""
    guessed, _ = mimetypes.guess_type(filename)
    return guessed if guessed is not None else UNKNOWN_MIMETYPE


def _is_type(mimetype: str, kind: str) -> bool:
    """Whether a hosted file answers to one of the `types` of `files.list`. Posts, snippets and Google documents are
    kinds of file this world holds none of."""
    return {
        "all": True,
        "images": mimetype.startswith("image/"),
        "pdfs": mimetype == "application/pdf",
        "zips": mimetype == "application/zip",
    }.get(kind, False)


class FileCalls(MessageCalls):
    # ------------------------------------------------------------------ what a file is

    def _shared_in(self, file: str) -> tuple[list[str], list[str], list[str], bool]:
        """The channels, private groups and direct messages a file is shared in, as the file object lists them (private
        ones only where the app is a member), and whether the app is in any of them."""
        channels: list[str] = []
        groups: list[str] = []
        ims: list[str] = []
        seen = False
        for channel in self._world.channels_after(None):
            private = channel.is_private or channel.is_im or channel.is_mpim
            if private and not self._in(channel):
                continue
            if not any(f.id == file for m in self._world.messages(channel.id) for f in m.files or []):
                continue
            seen = seen or self._in(channel)
            (ims if channel.is_im or channel.is_mpim else groups if channel.is_private else channels).append(channel.id)
        return channels, groups, ims, seen

    def _visible(self, file: wire.SlackFile) -> bool:
        """The app may see its own files, and files appearing in the channels it is in
        (https://docs.slack.dev/reference/methods/files.info: "Bot users tokens may use this method to access
        information about files appearing in the channels they belong to")."""
        return file.user == self._world.bot or self._shared_in(file.id)[3]

    def _served_file(self, file: wire.SlackFile) -> wire.SlackFile:
        channels, groups, ims, _ = self._shared_in(file.id)
        return file.model_copy(
            update={
                "channels": channels,
                "groups": groups,
                "ims": ims,
                "comments_count": 0,
                "is_public": file.is_public or bool(channels),
            }
        )

    def _find_file(self, file: str) -> wire.SlackFile:
        if not file:
            raise wire.Refusal("invalid_arguments")
        found = self._world.file(file)
        if found is None:
            raise wire.Refusal("file_deleted" if self._world.was_file(file) else "file_not_found")
        return found

    # ------------------------------------------------------------------ upload

    def files_get_upload_url_external(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.UploadUrlArgs, presented)
        if not args.filename or not args.length.isdecimal():
            raise wire.Refusal("invalid_arguments")
        length = int(args.length)
        if length == 0:
            raise wire.Refusal("missing_argument")
        if args.snippet_type:
            raise NotServed("files.getUploadURLExternal with a snippet_type, whose values the page does not list")
        if args.alt_txt is not None and len(args.alt_txt) > MAX_ALT_TEXT:
            raise wire.Refusal("alt_txt_too_large")
        if "/" in args.filename:
            raise NotServed("files.getUploadURLExternal with a filename holding a slash, which no page speaks of")
        seq = self._world.next_seq()
        file = state.file_id(f"upload|{self._world.team.id}|{seq}")
        ticket = state.minted("upload", seq, self._now()).rsplit(".", 1)[-1]
        upload = wire.SlackUpload(
            file=file,
            team=self._world.team.id,
            ticket=ticket,
            filename=args.filename,
            length=length,
            user=self._world.bot,
            alt_txt=args.alt_txt,
        )
        self._world.write(
            state.upload_ref(file),
            upload,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=state.UPLOADS,
            after=self._recorded(f"upload url for {args.filename}", "uploads"),
        )
        return wire.UploadUrl(upload_url=state.upload_url(upload.team, file, ticket), file_id=file)

    async def upload(self, request: Request) -> Response:
        """The POST of a file's bytes to the URL `files.getUploadURLExternal` answered: "raw bytes or ... multipart form
        encoded", HTTP 200 when it succeeds (https://docs.slack.dev/reference/methods/files.getUploadURLExternal)."""
        team, _, rest = request.path_params["key"].partition("-")
        file, _, ticket = rest.partition("-")
        world = SlackWorld(self._store).team_of(team)
        upload = world.body(state.upload_ref(file), wire.SlackUpload) if world is not None else None
        if world is None or upload is None or upload.ticket != ticket:
            raise NotServed("an upload to a URL this fake did not issue")
        if upload.uploaded or upload.completed:
            raise NotServed("a second upload to one upload URL, which the page does not speak of")
        kind = request.headers["content-type"] if "content-type" in request.headers else ""
        if kind.split(";", 1)[0].strip().lower() == MULTIPART:
            parts = [v for v in (await request.form()).values() if not isinstance(v, str)]
            if len(parts) != 1:
                raise NotServed("a multipart upload of anything but one file part")
            held = await parts[0].read()
        else:
            held = await request.body()
        if len(held) != upload.length:
            raise NotServed(
                f"an upload of {len(held)} bytes to a URL issued for {upload.length}, which the page does not speak of"
            )
        world.write(
            state.content_ref(file),
            wire.SlackFileContent(
                file=file, mimetype=mimetype_of(upload.filename), encoded=base64.b64encode(held).decode()
            ),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=state.FILES,
        )
        world.write(
            state.upload_ref(file),
            upload.model_copy(update={"uploaded": True}),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=state.UPLOADS,
        )
        return Response(status_code=200)

    # ------------------------------------------------------------------ complete, share

    def _targets(self, args: wire.CompleteUploadArgs) -> list[wire.SlackChannel]:
        """The conversations to share in: `channel_id`, or the comma-separated `channels` ("channel IDs or user IDs");
        none leaves the file private."""
        if args.channel_id and args.channels:
            raise NotServed(
                "files.completeUploadExternal with both channel_id and channels, which its page does not speak of"
            )
        named = [args.channel_id] if args.channel_id else [c.strip() for c in args.channels.split(",") if c.strip()]
        if len(named) > MAX_SHARED_AT_ONCE:
            raise wire.Refusal("channels_limit_exceeded")
        if args.thread_ts is not None and len(named) != 1:
            raise NotServed("files.completeUploadExternal with a thread_ts and not exactly one channel")
        found: list[wire.SlackChannel] = []
        for name in named:
            try:
                channel = self._channel(name) if args.channel_id else self._destination(name)
            except wire.Refusal as refused:
                if refused.error != "channel_not_found":
                    raise
                raise wire.Refusal("channel_not_found" if args.channel_id else "invalid_channel") from refused
            if not self._in(channel):
                raise wire.Refusal("not_in_channel")
            if channel.is_archived:
                raise NotServed(
                    "files.completeUploadExternal into an archived channel, which its page does not speak of"
                )
            found.append(channel)
        return found

    def files_complete_upload_external(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.CompleteUploadArgs, presented)
        if not args.files:
            raise wire.Refusal("invalid_arguments")
        uploads: list[tuple[wire.SlackUpload, str | None]] = []
        for asked in args.files:
            found = self._world.body(state.upload_ref(asked.id), wire.SlackUpload)
            if found is None or found.team != self._world.team.id:
                raise wire.Refusal("file_not_found")
            if found.completed:
                raise NotServed(
                    "a second files.completeUploadExternal for one file: the page says it can be called once"
                )
            if not found.uploaded:
                raise NotServed(
                    "files.completeUploadExternal before the bytes were uploaded, which its page does not speak of"
                )
            uploads.append((found, asked.title))
        targets = self._targets(args)
        now = self._now()
        public = any(not (c.is_private or c.is_im or c.is_mpim) for c in targets)
        shared: list[wire.SlackFile] = []
        for upload, title in uploads:
            content = self._world.body(state.content_ref(upload.file), wire.SlackFileContent)
            assert content is not None
            extension = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else "text"
            file = wire.SlackFile(
                id=upload.file,
                created=now,
                timestamp=now,
                name=upload.filename,
                title=title if title else upload.filename,
                mimetype=content.mimetype,
                filetype=extension,
                pretty_type=extension.upper(),
                user=self._world.bot,
                user_team=self._world.team.id,
                size=upload.length,
                is_public=public,
                url_private=state.url_private(upload.file, upload.filename, self._world.team.id),
                url_private_download=state.url_private_download(upload.file, upload.filename, self._world.team.id),
                permalink=(
                    f"https://{self._world.team.domain}.slack.com/files/{self._world.bot}/{upload.file}/{upload.filename}"
                ),
                bot_id=self._world.team.bot_id,
                bot_user_id=self._world.bot,
                alt_txt=upload.alt_txt,
            )
            self._world.write(
                state.file_ref(file.id),
                file,
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                parent=self._world.team.id,
                after=DocumentSnapshot(title=file.title, mime_type=file.mimetype),
            )
            self._world.write(
                state.upload_ref(upload.file),
                upload.model_copy(update={"completed": True}),
                operation=Operation.UPDATE,
                actor=Actor.AGENT,
                parent=state.UPLOADS,
            )
            shared.append(file)
        for channel in targets:
            self._share(channel, shared, args)
        return wire.UploadCompleted(files=[wire.CompletedFile(id=f.id, title=f.title) for f in shared])

    def _share(self, channel: wire.SlackChannel, files: list[wire.SlackFile], args: wire.CompleteUploadArgs) -> None:
        """The message that shares `files` in `channel`: the uploader's, `initial_comment` as its text or else `blocks`
        ("If the `initial_comment` field is provided, the `blocks` field is ignored")."""
        thread_ts = self._thread_of(channel.id, args.thread_ts)
        blocks = None if args.initial_comment else args.blocks
        message = self._from_bot(args.initial_comment, blocks, None, thread_ts).model_copy(
            update={"subtype": "file_share", "files": files}
        )
        written = self._world.write(
            state.message_ref(message.ts),
            message,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=channel.id,
            after=self._snapshot(channel.id, message),
        )
        for nth, file in enumerate(files):
            self._emit(
                wire.FileSharedEvent(
                    channel_id=channel.id,
                    file_id=file.id,
                    user_id=self._world.bot,
                    file=wire.FileId(id=file.id),
                    event_ts=self._stamp(written.seq),
                ),
                written.seq,
                nth,
            )

    # ------------------------------------------------------------------ read, list, delete

    def files_info(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.FileArgs, presented)
        file = self._find_file(args.file)
        wire.decode_cursor(args.cursor)
        if not self._visible(file):
            raise wire.Refusal("not_visible")
        self._world.saw(state.file_ref(file.id), Operation.READ)
        return wire.FileInfo(file=self._served_file(file), response_metadata=wire.ResponseMetadata())

    def files_list(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.FilesListArgs, presented)
        kinds = {t.strip() for t in args.types.split(",") if t.strip()} or {"all"}
        if not kinds <= FILE_TYPES:
            raise wire.Refusal("unknown_type")
        if args.count < 1 or args.page < 1:
            raise wire.Refusal("invalid_arguments")
        if args.user:
            self._user(args.user)
        if args.channel and self._world.channel(args.channel) is None:
            raise NotServed("files.list of a channel the workspace does not have, for which its page gives no error")
        for bound in (args.ts_from, args.ts_to):
            if bound and not bound.isdecimal():
                raise wire.Refusal("invalid_arguments")
        picked: list[wire.SlackFile] = []
        for file in self._world.every_file():
            if not self._visible(file) or (args.user and file.user != args.user):
                continue
            if (args.ts_from and file.created < int(args.ts_from)) or (args.ts_to and file.created > int(args.ts_to)):
                continue
            if not any(_is_type(file.mimetype, kind) for kind in kinds):
                continue
            served = self._served_file(file)
            where = [*(served.channels or []), *(served.groups or []), *(served.ims or [])]
            if args.channel and args.channel not in where:
                continue
            picked.append(served)
        first = (args.page - 1) * args.count
        self._world.saw(state.team_ref(self._world.team.id), Operation.SEARCH)
        return wire.FilesListed(
            files=picked[first : first + args.count],
            paging=wire.Paging(
                count=args.count, total=len(picked), page=args.page, pages=max(1, math.ceil(len(picked) / args.count))
            ),
        )

    def files_delete(self, presented: wire.Presented) -> wire.Ok:
        file = self._find_file(wire.read_args(wire.FileArgs, presented).file)
        if not self._visible(file):
            raise wire.Refusal("file_not_found")
        if file.user != self._world.bot:
            raise wire.Refusal("cant_delete_file")
        written = self._world.delete(
            state.file_ref(file.id),
            actor=Actor.AGENT,
            parent=self._world.team.id,
            before=DocumentSnapshot(title=file.title, mime_type=file.mimetype),
        )
        self._world.delete(state.content_ref(file.id), actor=Actor.AGENT, parent=state.FILES)
        self._emit(wire.FileDeletedEvent(file_id=file.id, event_ts=self._stamp(written.seq)), written.seq)
        return wire.Ok()
