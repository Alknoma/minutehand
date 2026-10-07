"""Gmail v1 driven by Google's own client, stock `googleapiclient` over `httplib2` with stock `google-auth`, in a
process of its own configured only by the environment Minutehand hands an agent, through the proxy.

What a proactive agent does with mail: it sends a question, polls its mailbox's history until the person's reply
lands (written beside the client by the provider's `land`, as the run loop does at the reply's moment), reads the
thread, marks the reply read and follows up in the same thread."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.google_workspace.provider import build
from minutehand.adapters.providers.google_workspace.seed import SeededEmail, WorkspaceSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, Scripted, SignIn
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from tests.providers.google_workspace.proxied import REFRESH, START, serving

pytestmark = pytest.mark.timeout(120)

ROBOT_REFRESH = "1//an-account-of-its-own"


def scenario(emails: list[SeededEmail] | None = None) -> Scenario:
    return Scenario(
        name="supplier_rates",
        goal="Acme's rate is confirmed with Dov.",
        owner="mara",
        starts_at=START,
        people=[
            Person(key="mara", name="Mara Lindqvist", email="mara@example.com", reply=Scripted(replies=[])),
            Person(key="dov", name="Dov Aranha", email="dov@example.com", reply=Scripted(replies=[])),
            Person(key="rosa", name="Rosa Field", email="rosa@example.com", reply=Scripted(replies=[])),
        ],
        sign_ins=[
            SignIn(provider="google_workspace", credential=REFRESH, person="mara"),
            SignIn(provider="google_workspace", credential=ROBOT_REFRESH),
        ],
        provider_seeds=[
            ProviderSeed(provider="google_workspace", body=WorkspaceSeed(emails=emails or []).model_dump_json())
        ],
    )


GMAIL = """
import base64
from email.message import EmailMessage
gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

def raw(message):
    return base64.urlsafe_b64encode(message.as_bytes()).decode()

def text(part):
    return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode()

def headers(message):
    return {h["name"]: h["value"] for h in message["payload"]["headers"]}
"""


async def test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up(
    tmp_path: Path,
) -> None:
    async with serving(tmp_path, scenario()) as google:
        client = await google.client(
            GMAIL
            + """
profile = gmail.users().getProfile(userId="me").execute()
labels = gmail.users().labels().list(userId="me").execute()
asked = EmailMessage()
asked["To"] = "Dov Aranha <dov@example.com>"
asked["Subject"] = "Supplier rates"
asked.set_content("Could you confirm Acme's hourly rate?")
sent = gmail.users().messages().send(userId="me", body={"raw": raw(asked)}).execute()
got = gmail.users().messages().get(userId="me", id=sent["id"]).execute()
say(profile=profile, labels=[label["id"] for label in labels["labels"]], sent=sent, headers=headers(got),
    body=text(got["payload"]), snippet=got["snippet"], size=got["sizeEstimate"])
wait()
history = gmail.users().history().list(userId="me", startHistoryId=profile["historyId"]).execute()
unread = gmail.users().messages().list(userId="me", q="from:dov is:unread").execute()
reply_id = unread["messages"][0]["id"]
thread = gmail.users().threads().get(userId="me", id=sent["threadId"]).execute()
marked = gmail.users().messages().modify(userId="me", id=reply_id, body={"removeLabelIds": ["UNREAD"]}).execute()
read = gmail.users().history().list(userId="me", startHistoryId=history["historyId"],
                                    historyTypes="labelRemoved").execute()
still = gmail.users().messages().list(userId="me", q="from:dov is:unread").execute()
reply_headers = headers(thread["messages"][1])
thanks = EmailMessage()
thanks["To"] = "dov@example.com"
thanks["Subject"] = "Re: Supplier rates"
thanks["In-Reply-To"] = reply_headers["Message-ID"]
thanks["References"] = reply_headers["References"] + " " + reply_headers["Message-ID"]
thanks.set_content("Thank you, Dov.")
followed = gmail.users().messages().send(userId="me", body={"raw": raw(thanks), "threadId": sent["threadId"]}).execute()
fresh = EmailMessage()
fresh["To"] = "dov@example.com"
fresh["Subject"] = "Another matter"
fresh.set_content("Unrelated.")
other = gmail.users().messages().send(userId="me", body={"raw": raw(fresh), "threadId": sent["threadId"]}).execute()
threads = gmail.users().threads().list(userId="me", q="subject:supplier").execute()
say(history=history, reply_id=reply_id, thread=[(headers(m)["From"], headers(m)["Subject"], text(m["payload"]))
                                                for m in thread["messages"]],
    reply_headers=reply_headers, marked=marked, read=read, still=still, followed=followed, other=other,
    threads=threads)
"""
        )
        first = await client.heard()
        sent = first["sent"]
        assert isinstance(sent, dict)
        assert sent["labelIds"] == ["SENT"] and sent["threadId"] == sent["id"]
        profile = first["profile"]
        assert isinstance(profile, dict) and profile["emailAddress"] == "mara@example.com"
        assert {"INBOX", "SENT", "UNREAD", "STARRED", "TRASH"} <= set(first["labels"])  # type: ignore[arg-type]
        headers = first["headers"]
        assert isinstance(headers, dict)
        assert (
            headers["From"] == "Mara Lindqvist <mara@example.com>" and headers["To"] == "Dov Aranha <dov@example.com>"
        )
        assert headers["Message-ID"].endswith("@mail.gmail.com>") and "Date" in headers
        assert first["body"] == "Could you confirm Acme's hourly rate?\n"
        assert first["snippet"] == "Could you confirm Acme&#39;s hourly rate?"

        asked = [e for e in google.store.events() if e.actor is Actor.AGENT and e.operation is Operation.CREATE]
        assert [e.entity.kind for e in asked] == [EntityKind.MESSAGE, EntityKind.MESSAGE]  # mara's Sent, dov's inbox
        [question] = [e for e in asked if isinstance(e.after, MessageSnapshot)]
        assert isinstance(question.after, MessageSnapshot)
        assert question.entity.external_id == sent["id"]
        assert question.after.recipient_emails == ["dov@example.com"] and question.after.channel == sent["threadId"]
        assert question.after.text == "Supplier rates\n\nCould you confirm Acme's hourly rate?"

        google.clock.jump(START + timedelta(hours=3))
        await google.provider.land(
            PersonReply(
                person="dov",
                in_reply_to=question.entity,
                text="Acme charges 12 an hour.",
                at=google.clock.now(),
            ),
            google.store,
            google.clock,
        )
        await client.go()
        second = await client.heard()
        await client.finished()

        history = second["history"]
        assert isinstance(history, dict)
        added = [m["message"] for record in history["history"] for m in record.get("messagesAdded", [])]
        assert [m["id"] for m in added] == [sent["id"], second["reply_id"]]
        assert added[1]["threadId"] == sent["threadId"] and set(added[1]["labelIds"]) == {"INBOX", "UNREAD"}
        assert second["thread"] == [
            ["Mara Lindqvist <mara@example.com>", "Supplier rates", "Could you confirm Acme's hourly rate?\n"],
            ["Dov Aranha <dov@example.com>", "Re: Supplier rates", "Acme charges 12 an hour.\n"],
        ]
        reply_headers = second["reply_headers"]
        assert isinstance(reply_headers, dict) and reply_headers["In-Reply-To"] == headers["Message-ID"]
        marked = second["marked"]
        assert isinstance(marked, dict) and marked["labelIds"] == ["INBOX"]
        read = second["read"]
        assert isinstance(read, dict)
        assert [r["labelsRemoved"][0]["labelIds"] for r in read["history"]] == [["UNREAD"]]
        assert second["still"] == {"resultSizeEstimate": 0}
        followed, other = second["followed"], second["other"]
        assert isinstance(followed, dict) and followed["threadId"] == sent["threadId"]
        assert isinstance(other, dict) and other["threadId"] == other["id"] != sent["threadId"]
        threads = second["threads"]
        assert isinstance(threads, dict) and [t["id"] for t in threads["threads"]] == [sent["threadId"]]

        [answer] = [
            e for e in google.store.events() if e.actor is Actor.PERSON and isinstance(e.after, MessageSnapshot)
        ]
        assert isinstance(answer.after, MessageSnapshot)
        assert answer.after.text == "Re: Supplier rates\n\nAcme charges 12 an hour."
        assert answer.after.thread_of == sent["id"] and answer.after.channel == sent["threadId"]
        assert answer.sim_time == START + timedelta(hours=3)
        snapshots = [
            e.after for e in google.store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        ]
        assert [s.channel for s in snapshots] == [sent["threadId"], sent["threadId"], other["id"]]
        assert [s.thread_of for s in snapshots] == [None, sent["threadId"], None], "a reply carries its thread"


async def test_a_bad_id_another_mailbox_no_recipient_and_an_account_without_mail_are_refused(
    tmp_path: Path,
) -> None:
    async with serving(tmp_path, scenario()) as google:
        client = await google.client(
            GMAIL
            + f"""
nobody = EmailMessage()
nobody["Subject"] = "To whom?"
nobody.set_content("Nobody is addressed.")
own = Credentials(token=None, refresh_token={ROBOT_REFRESH!r}, token_uri="https://oauth2.googleapis.com/token",
                  client_id="1234.apps.googleusercontent.com", client_secret="client-secret", scopes=SCOPES)
no_mail = build("gmail", "v1", credentials=own, cache_discovery=False)
say(
    bad_id=refused(lambda: gmail.users().messages().get(userId="me", id="not-an-id").execute()),
    missing=refused(lambda: gmail.users().messages().get(userId="me", id="00000fff0123abcd").execute()),
    elsewhere=refused(lambda: gmail.users().getProfile(userId="dov@example.com").execute()),
    no_recipient=refused(lambda: gmail.users().messages().send(userId="me", body={{"raw": raw(nobody)}}).execute()),
    no_raw=refused(lambda: gmail.users().messages().send(userId="me", body={{}}).execute()),
    no_mailbox=refused(lambda: no_mail.users().getProfile(userId="me").execute()),
    grouped=refused(lambda: gmail.users().messages().list(userId="me", q="(from:dov subject:rates)").execute()),
    watch=refused(lambda: gmail.users().watch(userId="me", body={{"topicName": "projects/p/topics/t"}}).execute()),
)
"""
        )
        seen = await client.heard()
        await client.finished()
        assert seen["bad_id"] == [400, "invalidArgument"]
        assert seen["missing"] == [404, "notFound"]
        assert seen["elsewhere"] == [403, "forbidden"]
        assert seen["no_recipient"] == [400, "invalidArgument"]
        assert seen["no_raw"] == [400, "invalidArgument"]
        assert seen["no_mailbox"] == [400, "failedPrecondition"]
        assert seen["grouped"] == [501, "notImplemented"]
        assert seen["watch"] == [501, "notImplemented"]
        assert not [e for e in google.store.events() if e.actor is Actor.AGENT and e.operation is Operation.CREATE]


async def test_an_unknown_label_on_a_held_message_is_refused(tmp_path: Path) -> None:
    async with serving(
        tmp_path, scenario([SeededEmail(sender="dov", to=["mara"], subject="Hi", text="Hello", ago=timedelta(hours=1))])
    ) as google:
        client = await google.client(
            GMAIL
            + """
held = gmail.users().messages().list(userId="me").execute()["messages"][0]["id"]
say(label=refused(lambda: gmail.users().messages().modify(userId="me", id=held,
                                                          body={"addLabelIds": ["Label_9"]}).execute()),
    starred=gmail.users().messages().modify(userId="me", id=held, body={"addLabelIds": ["STARRED"]}).execute())
"""
        )
        seen = await client.heard()
        await client.finished()
        assert seen["label"] == [400, "invalidArgument"]
        starred = seen["starred"]
        assert isinstance(starred, dict) and starred["labelIds"] == ["INBOX", "UNREAD", "STARRED"]


SEEDED = [
    SeededEmail(
        key="invoice",
        sender="dov",
        to=["mara"],
        subject="Invoice for September",
        text="The September invoice is attached in spirit.",
        ago=timedelta(days=2),
    ),
    SeededEmail(
        sender="rosa", to=["mara"], subject="Lunch?", text="Thursday at noon?", ago=timedelta(hours=5), unread=False
    ),
    SeededEmail(
        sender="mara",
        to=["dov"],
        subject="Re: Invoice for September",
        text="Thanks, paying it Friday.",
        ago=timedelta(days=1),
        in_reply_to="invoice",
    ),
]


async def test_search_operators_find_the_seeded_mail_they_name(tmp_path: Path) -> None:
    async with serving(tmp_path, scenario(SEEDED)) as google:
        client = await google.client(
            GMAIL
            + """
def subjects(q):
    found = gmail.users().messages().list(userId="me", q=q).execute()
    return sorted(headers(gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                                                         metadataHeaders=["Subject"]).execute())["Subject"]
                  for m in found.get("messages", []))

threads = gmail.users().threads().list(userId="me").execute()["threads"]
whole = [len(gmail.users().threads().get(userId="me", id=t["id"], format="minimal").execute()["messages"])
         for t in threads]
say(
    invoice=subjects("subject:invoice"), rosa=subjects("from:rosa"), unread=subjects("is:unread"),
    read=subjects("is:read"), sent=subjects("in:sent"), recent=subjects("newer_than:1d"),
    either=subjects("from:rosa OR from:dov"), not_rosa=subjects("-from:rosa"), words=subjects("thursday noon"),
    phrase=subjects('"at noon"'), not_phrase=subjects('"noon at"'), me=subjects("from:me"), threads=whole,
)
"""
        )
        seen = await client.heard()
        await client.finished()
        assert seen["invoice"] == ["Invoice for September", "Re: Invoice for September"]
        assert seen["rosa"] == ["Lunch?"]
        assert seen["unread"] == ["Invoice for September"]
        assert seen["read"] == ["Lunch?", "Re: Invoice for September"]
        assert seen["sent"] == ["Re: Invoice for September"]
        assert seen["recent"] == ["Lunch?"]
        assert seen["either"] == ["Invoice for September", "Lunch?"]
        assert seen["not_rosa"] == ["Invoice for September", "Re: Invoice for September"]
        assert seen["words"] == ["Lunch?"] and seen["phrase"] == ["Lunch?"] and seen["not_phrase"] == []
        assert seen["me"] == ["Re: Invoice for September"]
        assert seen["threads"] == [1, 2]


def test_a_seeded_email_naming_nobody_or_answering_nothing_is_refused(tmp_path: Path) -> None:
    for emails, said in (
        ([SeededEmail(sender="zed", to=["mara"], text="hi", ago=timedelta(hours=1))], "'zed'"),
        ([SeededEmail(sender="dov", to=["mara"], text="hi", ago=timedelta(hours=1), in_reply_to="x")], "'x'"),
    ):
        store = SqliteStore(tmp_path / f"{said.strip(chr(39))}.db", "run", RunClock(START))
        with pytest.raises(ValueError, match=said):
            build().seed(scenario(emails), store)
        assert store.head() == 0
