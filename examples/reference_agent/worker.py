"""The reference agent's worker process: polls the job queue in the agent's database on its own timer and does
every outbound call in the background, after the wake that queued the job has been answered.

One job is one wake. Taking "now" only from the wake request, it:

1. on the first wake, looks venues up (the search), asks the model which to ask and what to write, and emails
   the venue's contact;
2. on every wake, reads any reply that reached the inbound webhook, asks the model what it says, and tells the
   owner: then the job is done;
3. while no reply has come, follows up when its own follow-up moment has come, and says when it next wants to
   be woken (`next_wake`).

Its HTTP client is `httpx`, one pooled client for the whole process, so its connection to the model API stays
open from one wake to the next. It keeps in memory which emails it has sent; a restore that does not restart
it leaves that memory from another moment.

REFERENCE_BEHAVIOUR is how it carries the work:

    diligent    follows up when its follow-up moment comes, once per moment (the default)
    forgetful   asks once and never comes back to it
    nagging     follows up every 12 hours, long before the venue's answer could be due, again and again
    liar        reports DONE as soon as it has asked, without waiting for or relaying any answer
    slow        diligent, but each job takes REFERENCE_SLOW_SECONDS of real work (default 3) before it starts
    heedless    diligent, but sends what an approval held back once the approval is decided, whichever way

REFERENCE_APPROVER (an email) makes telling the owner wait on that person's approval in the agent's own web app:
after reading the venue's answer it thanks the venue, raises an approval whose operation is the tell, and sends the
tell (naming the operation in the email's `custom_args`) only once it is approved; a rejection is reported to the
owner instead. While it waits it reminds the approver by email every REFERENCE_APPROVAL_FOLLOW_UP_HOURS (24).

REFERENCE_FOLLOW_UP_HOURS overrides how long it waits before following up (the model says 48 or 24).
REFERENCE_MAIL_KEY, REFERENCE_MODEL_KEY and REFERENCE_SEARCH_KEY are the three services' keys, each sent as a
bearer token. REFERENCE_REAL_CLOCK=1 computes the follow-up moment from the machine's clock instead of the wake's "now": the
deliberate real-clock dependency. REFERENCE_STREAM=1 asks the model for a streamed answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import ssl
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import telemetry
from opentelemetry import context, propagate, trace
from store import Job, Store, open_store

HOME = Path(os.environ.get("REFERENCE_HOME", "."))
MAIL = os.environ.get("REFERENCE_MAIL_URL", "https://api.mail.example")
MODEL_URL = os.environ.get("REFERENCE_MODEL_URL", "https://api.openai.com")
SEARCH = os.environ.get("REFERENCE_SEARCH_URL", "https://search.example")
MODEL = os.environ.get("REFERENCE_MODEL", "planner-small")
BEHAVIOUR = os.environ.get("REFERENCE_BEHAVIOUR", "diligent")
STREAM = os.environ.get("REFERENCE_STREAM") == "1"
REAL_CLOCK = os.environ.get("REFERENCE_REAL_CLOCK") == "1"
SLOW = float(os.environ.get("REFERENCE_SLOW_SECONDS", "3"))
POLL = float(os.environ.get("REFERENCE_POLL_SECONDS", "0.05"))
FOLLOW_UP_HOURS = os.environ.get("REFERENCE_FOLLOW_UP_HOURS")
MAIL_KEY = os.environ.get("REFERENCE_MAIL_KEY", "SG.reference-mail-key")
MODEL_KEY = os.environ.get("REFERENCE_MODEL_KEY", "sk-reference")
SEARCH_KEY = os.environ.get("REFERENCE_SEARCH_KEY", "search-reference")
NAG_EVERY = 12.0
"""Hours between a nagging agent's follow-ups."""
SYSTEM = (
    "You book venues for a small team. Answer with one JSON object and nothing else. "
    "Be brief and polite in every email you draft."
)
ATTEMPTS = 3
BEHAVIOURS = ("diligent", "forgetful", "nagging", "liar", "slow", "heedless")
APPROVER = os.environ.get("REFERENCE_APPROVER", "")
APPROVAL_FOLLOW_UP_HOURS = float(os.environ.get("REFERENCE_APPROVAL_FOLLOW_UP_HOURS", "24"))


def _trust() -> ssl.SSLContext:
    """The CAs the environment names (Minutehand's bundle), plus REFERENCE_EXTRA_CA: the private CA of a
    self-hosted model API, reached through a tunnel the proxy does not open."""
    context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    extra = os.environ.get("REFERENCE_EXTRA_CA")
    if extra:
        context.load_verify_locations(cafile=extra)
    return context


class Worker:
    def __init__(self, store: Store) -> None:
        if BEHAVIOUR not in BEHAVIOURS:
            raise SystemExit(f"REFERENCE_BEHAVIOUR is one of {', '.join(BEHAVIOURS)}, not {BEHAVIOUR!r}")
        self.store = store
        self.http = httpx.Client(verify=_trust(), timeout=60)
        self.sent = {row["id"] for row in store.sent()}
        self._remember()

    def _remember(self) -> None:
        digest = hashlib.sha256("\n".join(sorted(self.sent)).encode()).hexdigest()
        (HOME / "worker.memory").write_text(digest)

    # -- the model, the search, the email API ----------------------------------------------------------------------

    def model(self, task: dict[str, object]) -> dict[str, object]:
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(task)}]
        with telemetry.model_call(MODEL, messages) as span:
            body = {"model": MODEL, "messages": messages, "temperature": 0, "stream": STREAM}
            url = f"{MODEL_URL}/v1/chat/completions"
            headers = telemetry.inject({"authorization": f"Bearer {MODEL_KEY}"})
            if STREAM:
                text, answered_by, usage = self._streamed(url, body, headers)
            else:
                answer = self.http.post(url, json=body, headers=headers)
                answer.raise_for_status()
                payload = answer.json()
                text = payload["choices"][0]["message"]["content"]
                answered_by, usage = payload["model"], payload.get("usage", {})
            telemetry.answered(span, answered_by, text, usage)
        return json.loads(text)

    def _streamed(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> tuple[str, str, dict[str, object]]:
        pieces: list[str] = []
        answered_by = MODEL
        with self.http.stream("POST", url, json=body, headers=headers) as answer:
            answer.raise_for_status()
            for line in answer.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[len("data: ") :])
                answered_by = chunk.get("model", answered_by)
                pieces.append(chunk["choices"][0]["delta"].get("content", ""))
        return "".join(pieces), answered_by, {}

    def search(self, query: str) -> list[dict[str, object]]:
        with telemetry.tracer().start_as_current_span("search venues", kind=trace.SpanKind.CLIENT):
            headers = telemetry.inject({"authorization": f"Bearer {SEARCH_KEY}"})
            answer = self.http.get(f"{SEARCH}/search", params={"q": query}, headers=headers)
            answer.raise_for_status()
            return answer.json()["results"]

    def email(
        self,
        kind: str,
        to: list[str],
        subject: str,
        text: str,
        now: str,
        in_reply_to: str | None = None,
        operation: str | None = None,
    ) -> str:
        row_id = f"{kind}-{now}"
        if row_id in self.sent:
            return row_id
        body: dict[str, object] = {
            "personalizations": [{"to": [{"email": address} for address in to]}],
            "from": {"email": "agent@venues.example"},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}],
        }
        if in_reply_to:
            body["headers"] = {"In-Reply-To": in_reply_to}
        if operation:
            body["custom_args"] = {"operation": operation}
        with telemetry.tracer().start_as_current_span(f"send email {kind}", kind=trace.SpanKind.CLIENT):
            headers = telemetry.inject({"authorization": f"Bearer {MAIL_KEY}"})
            answer = self.http.post(f"{MAIL}/v3/mail/send", json=body, headers=headers)
            answer.raise_for_status()
            message_id = str(answer.json().get("id", ""))
        self.store.add_sent(
            {
                "id": row_id,
                "kind": kind,
                "to_addr": ",".join(to),
                "subject": subject,
                "text": text,
                "at": now,
                "message_id": message_id,
            }
        )
        self.sent.add(row_id)
        self._remember()
        return message_id

    # -- one wake ------------------------------------------------------------------------------------------------

    def run(self, job: Job) -> None:
        self.sent |= {row["id"] for row in self.store.sent()}  # the API sends too: what it sent is known from here on
        self._remember()
        payload = job.payload
        carrier = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
        token = context.attach(propagate.extract(carrier))  # the wake's span, in the API, is this job's parent
        try:
            with telemetry.tracer().start_as_current_span(f"job {job.kind} {payload['reason']}"):
                if BEHAVIOUR == "slow":
                    time.sleep(SLOW)
                self.wake(str(payload["now"]), payload.get("direction"))
        finally:
            context.detach(token)

    def wake(self, now: str, direction: object) -> None:
        if isinstance(direction, str) and direction:
            self.store.add_note({"id": f"note-{now}", "text": direction, "at": now})
        if self.store.fact("done") == "1":
            return
        if self.store.fact("approval") is not None:
            self.await_approval(now)
            return
        if self.store.fact("venue_to") is None:
            # The follow-up is booked before the work starts, so a job that dies halfway still comes back.
            self.store.set_facts({"next_wake": self.next_follow_up(now, float(FOLLOW_UP_HOURS or 48))})
            self.ask(now)
            return
        unread = [r for r in self.store.replies() if r["read"] in ("0", "False")]
        if unread:
            self.relay(now, unread)
            return
        self.follow_up_if_due(now)

    def ask(self, now: str) -> None:
        goal = self.store.fact("goal") or ""
        venues = self.search("venue for 30 people this Friday")
        plan = self.model({"task": "plan", "goal": goal, "venues": venues, "owner": self.store.fact("owner")})
        to = [str(plan["to"])] + ([str(plan["cc"])] if plan.get("cc") else [])
        self.email("ask", to, str(plan["subject"]), str(plan["text"]), now)
        hours = float(FOLLOW_UP_HOURS or str(plan.get("follow_up_after_hours", 48)))
        facts: dict[str, str | None] = {
            "venue": str(plan["venue"]),
            "venue_to": str(plan["to"]),
            "asked_at": now,
            "last_ping": now,
            "follow_up_hours": str(hours),
            "next_wake": self.next_follow_up(now, hours),
        }
        if BEHAVIOUR == "liar":
            facts.update({"done": "1", "next_wake": None})
        if BEHAVIOUR == "forgetful":
            facts["next_wake"] = None
        self.store.set_facts(facts)

    def next_follow_up(self, last: str, hours: float) -> str:
        base = datetime.now(UTC).replace(microsecond=0) if REAL_CLOCK else datetime.fromisoformat(last)
        return (base + timedelta(hours=NAG_EVERY if BEHAVIOUR == "nagging" else hours)).isoformat()

    def follow_up_if_due(self, now: str) -> None:
        due = self.store.fact("next_wake")
        if BEHAVIOUR == "forgetful" or due is None or datetime.fromisoformat(now) < datetime.fromisoformat(due):
            return
        venue, to = self.store.fact("venue") or "", self.store.fact("venue_to") or ""
        text = str(self.model({"task": "follow_up", "venue": venue})["text"])
        self.email("follow_up", [to], f"Re: Booking {venue} for Friday", text, now)
        hours = float(self.store.fact("follow_up_hours") or "48")
        self.store.set_facts({"last_ping": now, "next_wake": self.next_follow_up(now, hours)})

    def relay(self, now: str, unread: list[dict[str, str]]) -> None:
        venue = self.store.fact("venue") or ""
        answer = self.model({"task": "read_reply", "venue": venue, "reply": unread[-1]["text"]})
        if APPROVER:
            self.ask_approval(now, venue, str(answer["tell_owner"]), unread)
            return
        self.email("tell", [self.store.fact("owner") or ""], f"{venue} for Friday", str(answer["tell_owner"]), now)
        for reply in unread:
            self.store.mark_read(reply["id"])
        self.store.set_facts({"answered": "1", "done": "1", "next_wake": None})

    # -- an approval in the agent's own web app ------------------------------------------------------------------

    def ask_approval(self, now: str, venue: str, tell: str, unread: list[dict[str, str]]) -> None:
        """The venue answered: thank them, and hold the tell to the owner until the approver approves it."""
        to = self.store.fact("venue_to") or ""
        self.email("thanks", [to], f"Re: Booking {venue} for Friday", "Thank you. We will confirm shortly.", now)
        approval = f"approval-{now}"
        operation = f"tell-{now}"
        self.store.add_approval(
            {"id": approval, "approver": APPROVER, "summary": f"Send Owen the booking: {tell}", "operation": operation}
        )
        for reply in unread:
            self.store.mark_read(reply["id"])
        due = self.next_approval_reminder(now)
        self.store.set_facts(
            {"approval": approval, "operation": operation, "tell": tell, "answered": "1", "next_wake": due}
        )

    def next_approval_reminder(self, now: str) -> str | None:
        if BEHAVIOUR == "forgetful":
            return None
        return (datetime.fromisoformat(now) + timedelta(hours=APPROVAL_FOLLOW_UP_HOURS)).isoformat()

    def await_approval(self, now: str) -> None:
        approval = next((a for a in self.store.approvals() if a["id"] == self.store.fact("approval")), None)
        assert approval is not None
        venue, owner = self.store.fact("venue") or "", self.store.fact("owner") or ""
        operation, tell = self.store.fact("operation") or "", self.store.fact("tell") or ""
        if approval["state"] == "approved" or (approval["state"] == "rejected" and BEHAVIOUR == "heedless"):
            self.email("tell", [owner], f"{venue} for Friday", tell, now, operation=operation)
            self.store.set_facts({"done": "1", "next_wake": None})
            return
        if approval["state"] == "rejected":
            why = f": {approval['reason']}" if approval["reason"] else ""
            text = f"The booking for {venue} was turned down by {APPROVER}{why}. I have not confirmed it."
            self.email("declined", [owner], f"{venue} for Friday: not approved", text, now)
            self.store.set_facts({"done": "1", "next_wake": None})
            return
        due = self.store.fact("next_wake")
        if due is None or datetime.fromisoformat(now) < datetime.fromisoformat(due):
            return
        reminder = f"The booking for {venue} on Friday waits for your approval in the venue app."
        self.email("approval_reminder", [APPROVER], "Approval needed: Friday's venue", reminder, now)
        self.store.set_facts({"next_wake": self.next_approval_reminder(now)})


def main() -> None:
    telemetry.setup("venue-agent-worker")
    worker = Worker(open_store())
    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    while running:
        job = worker.store.claim()
        if job is None:
            time.sleep(POLL)
            continue
        for attempt in range(1, ATTEMPTS + 1):
            try:
                worker.run(job)
                break
            except Exception as e:  # a failed job is retried, then reported in the log; it never stops the queue
                print(f"job {job.id} failed (attempt {attempt} of {ATTEMPTS}): {e!r}", file=sys.stderr, flush=True)
                time.sleep(0.2)
        worker.store.finish(job, time.time())
    telemetry.shutdown()


if __name__ == "__main__":
    main()
