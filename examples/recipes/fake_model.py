"""A stand-in for a model API, so every recipe runs offline and gives the same answer every time.

Nothing here is a model. It is a few rules that say what a model would answer to the situations the recipes'
agents describe, served over the two wire formats the frameworks speak:

    POST /v1/chat/completions   OpenAI's chat completions (LangGraph through langchain-openai, the OpenAI
                                Agents SDK, Pydantic AI, the Vercel AI SDK)
    POST /v1/messages           Anthropic's messages, plain or streamed (the Claude Agent SDK, the Anthropic SDK)

Each recipe's agent writes one situation per turn, in plain sentences, and the rules read them:

    It is now 2026-08-24T09:00:00+00:00.
    Work: Confirm the venue for the team offsite with Rosa, and tell Owen what she said.
    Report to: owen@example.com. Ask: rosa@example.com.
    and one of
        Nobody has been asked yet.
        No answer yet from rosa@example.com, expected by 2026-08-26T09:00:00+00:00. Follow-ups sent: 0.
        rosa@example.com answered: The lakeside hall, booked for the 14th.

and the "model" answers with the tool calls a good agent would make: ask and remember the wait with the date an
answer is expected by; follow up once and remember the new date; after that, tell the owner nobody answered and
close the wait; on an answer, thank the person, tell the owner, and close the wait. Once the tools have answered it
says it is finished. The tools it calls are the same in every recipe:

    send_slack_message(email, text)     a direct message in Slack
    remember_wait(email, expected_by)   an answer is owed by `email`, expected by that ISO 8601 moment
    close_wait(email)                   nothing more is owed by `email`

    python fake_model.py [--port 8790]

It also writes what Minutehand's people say, when Minutehand itself asks (a request whose structured answer is
`WrittenStep`, `WrittenReply`, `WrittenTransition` or `WrittenSummary`), so a run with
model-written people needs no real model either. The rules read the prompt Minutehand sends, never guess:

    a script step       its facts ("What this reply says:"), as plain sentences; a step that declines, asks back
                        or defers says so in a fixed sentence
    conversing          an answer only when the last message asks something (holds "?"): what the person knows,
                        as plain sentences, or "I do not know."; else no answer
    a transition        the one the scenario pinned, else the first offered whose name, then whose state, the
                        person's facts mention, else the first offered; each required field, and an optional one
                        when they know something, from their facts: a ticket's move, an invitation's answer, a
                        decision in the agent's own product
    a summary           how many earlier messages there were
    a fact check        whether a person's reply stays inside what they know: a go-ahead or approval that
                        nothing they know gives is not theirs to give
    a review            the shared reviewer of the agent's effects (`ItemReview`): an amount the effect carries
                        that nothing the agent was given holds is an invented fact; a record stored, or a ticket or
                        document written, while a declared service's item waits on a person is acting before the
                        decision

And it stands in for a declared service (`docs/services.md`), from the state and the log Minutehand shows it:

    a machine           an approval (pending; approve, reject or ask back; the agent resubmits), or, when the
                        service's description speaks of options, a choice (open; choose)
    a route             what its method and path say: POST to a collection creates, POST to an item's action names
                        the agent transition of that name, PATCH of a `status` names the transition to it, GET reads
    an answer           an item as `{"id", "status", ...what the agent filed, "responses": [...]}`, a list as
                        `{"items": [...]}`, `.../responses` as `{"responses": [...]}`, `/events` as `{"events": [...]}`,
                        a refusal as `{"error": {"code", "message"}}`, a push as `{"event", "item"}`

Each answer to the same prompt is the same, so a run with it is repeatable.

It uses nothing outside the standard library, so it runs in whatever Python is at hand.
"""

from __future__ import annotations

import argparse
import json
import re
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8790
WAIT = timedelta(days=2)
"""How long the rules give a person to answer, from each message that asks them."""
FINISHED = "Finished for now."


@dataclass(frozen=True)
class Call:
    """One tool call the rules decided on."""

    name: str
    arguments: dict[str, str]


def _found(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    if match is None:
        raise ValueError(f"the situation does not say {pattern!r}: {text!r}")
    return match.group(1).strip()


def decide(situation: str) -> list[Call]:
    """The tool calls a careful agent makes in this situation."""
    now = datetime.fromisoformat(_found(r"It is now (\S+?)\.?(?:\s|$)", situation))
    work = _found(r"Work: (.+)", situation)
    owner = _found(r"Report to: (\S+@\S+)\.(?:\s|$)", situation)
    person = _found(r"Ask: (\S+@\S+)\.(?:\s|$)", situation)
    expected_by = (now + WAIT).isoformat()
    question = f"Hello! I am working on this for {owner}: {work} Could you help, please?"
    if "Nobody has been asked yet." in situation:
        return [
            Call("send_slack_message", {"email": person, "text": question}),
            Call("remember_wait", {"email": person, "expected_by": expected_by}),
        ]
    answered = re.search(r"(\S+@\S+) answered: (.+)", situation)
    if answered is not None:
        return [
            Call("send_slack_message", {"email": answered.group(1), "text": "Thank you!"}),
            Call(
                "send_slack_message",
                {"email": owner, "text": f"Confirmed: {work} {answered.group(1)} says: {answered.group(2).strip()}"},
            ),
            Call("close_wait", {"email": answered.group(1)}),
        ]
    if int(_found(r"Follow-ups sent: (\d+)", situation)) == 0:
        return [
            Call("send_slack_message", {"email": person, "text": f"Following up: {question}"}),
            Call("remember_wait", {"email": person, "expected_by": expected_by}),
        ]
    return [
        Call(
            "send_slack_message", {"email": owner, "text": f"No answer from {person}, even after a follow-up: {work}"}
        ),
        Call("close_wait", {"email": person}),
    ]


# -- what people say, when Minutehand asks -------------------------------------------------------------------

PEOPLE = ("WrittenStep", "WrittenReply", "WrittenTransition", "WrittenSummary")
SERVICES = ("WrittenMachine", "WrittenRoute", "WrittenAnswer")
JUDGES = ("ItemReview", "FactCheck")


def _bullets(text: str, heading: str) -> list[str]:
    """The `- ` lines under `heading`, up to the first line that is not one."""
    if heading not in text:
        return []
    found: list[str] = []
    for line in text.split(heading, 1)[1].splitlines()[1:]:
        if not line.startswith("- "):
            break
        found.append(line[2:].strip())
    return found


def _sentences(facts: list[str]) -> str:
    said = []
    for fact in facts:
        fact = fact.strip()
        if not fact:
            continue
        fact = fact[0].upper() + fact[1:]
        said.append(fact if fact.endswith((".", "!", "?")) else fact + ".")
    return " ".join(said)


_UNKNOWN = ("nothing about this beyond", "what you know, as it bears on", "what you know")


def _known(system: str, heading: str) -> list[str]:
    return [f for f in _bullets(system, heading) if not f.startswith(_UNKNOWN)]


def _last_message(shown: str) -> str:
    marker = "Their last message, which you answer now:\n"
    return shown.split(marker, 1)[1] if marker in shown else shown


def person_answer(schema: str, system: str, shown: str) -> dict[str, object]:
    """What a person says, by the rules above."""
    known = _known(system, "What you know:")
    if schema == "WrittenSummary":
        lines = [line for line in shown.splitlines() if line.startswith("[")]
        return {"summary": f"{len(lines)} earlier messages."}
    if schema == "WrittenStep":
        carries = _known(system, "What this reply says:") or known
        intent = system.split("What you do with this message: ", 1)[1].splitlines()[0]
        if intent.startswith("you say that it is not yours"):
            return {"text": "That is not mine to answer."}
        if intent.startswith("you do not answer yet: you ask"):
            return {"text": "Before I answer: what is it for?"}
        if intent.startswith("you say you will come back"):
            return {"text": ("I will come back to you on this. " + _sentences(carries)).strip()}
        return {"text": _sentences(carries) or "I do not know."}
    if schema == "WrittenReply":
        if "?" not in _last_message(shown):
            return {"replies": False, "text": None, "press": None, "form": None}
        return {"replies": True, "text": _sentences(known) or "I do not know.", "press": None, "form": None}
    if schema != "WrittenTransition":
        raise ValueError(f"no rule for what a person says as {schema}")
    return transition_answer(system, shown, known)


def transition_answer(system: str, shown: str, known: list[str]) -> dict[str, object]:
    """What a person does with an item pending on them, by the rules above. A fact written `name: value` fills the
    field of that name; the other facts are the words of a required field, or of an optional one."""
    offers = re.findall(r'^- "([^"]+)": it becomes (.+?)(?: \(.*\))?$', shown, flags=re.MULTILINE)
    decided = re.search(r'You have decided: "([^"]+)"', system)
    said = " ".join(known).casefold()
    if decided is not None:
        take = decided.group(1)
        carries = _bullets(system, "what you write with it says:") or known
    else:
        spelled = [(name, name.replace("_", " ").casefold()) for name, _ in offers]
        named = [name for name, words in spelled if name.casefold() in said or words in said]
        reached = [name for name, state in offers if state.casefold() in said]
        take = (named or reached or [offers[0][0]])[0]
        carries = known
    block = shown.split(f'- "{take}"', 1)[1].split('\n- "', 1)[0] if f'- "{take}"' in shown else ""
    facts = [c for c in carries if not c.startswith(_UNKNOWN)]
    pairs = dict(m.groups() for c in facts if (m := re.match(r"^([a-z][a-z0-9_]*): (.+)$", c)))
    words = _sentences([c for c in facts if not re.match(r"^[a-z][a-z0-9_]*: ", c)])
    fields = []
    for name, needed in re.findall(r'field "([^"]+)" \((required|optional)\)', block):
        if name in pairs:
            fields.append({"name": name, "value": pairs[name]})
        elif needed == "required" or words:
            fields.append({"name": name, "value": words or "No comment."})
    return {"take": take, "fields": fields}


# -- a declared service, when Minutehand asks -------------------------------------------------------------------

APPROVAL = {
    "initial": "pending",
    "states": ["pending", "approved", "rejected", "needs_info"],
    "transitions": [
        {"name": "approve", "from": ["pending"], "to": "approved", "by": "person"},
        {"name": "reject", "from": ["pending"], "to": "rejected", "by": "person"},
        {"name": "ask_back", "from": ["pending"], "to": "needs_info", "by": "person"},
        {"name": "resubmit", "from": ["needs_info"], "to": "pending", "by": "agent"},
    ],
}
CHOICE = {
    "initial": "open",
    "states": ["open", "chosen"],
    "transitions": [{"name": "choose", "from": ["open"], "to": "chosen", "by": "person", "requires": ["option"]}],
}
_EVENT = re.compile(
    r"^- (?P<name>[A-Za-z][\w-]*)(?: (?P<was>\S+) -> (?P<to>\S+))? item (?P<item>\S+) by (?P<by>\S+): (?P<content>.*)$"
)


def _log(shown: str) -> list[dict[str, object]]:
    """The service's log as Minutehand shows it, one entry a line."""
    found = []
    for line in shown.splitlines():
        matched = _EVENT.match(line)
        if matched is None:
            continue
        entry: dict[str, object] = dict(matched.groupdict())
        try:
            entry["content"] = json.loads(str(entry["content"]))
        except json.JSONDecodeError:
            entry["content"] = {}
        found.append(entry)
    return found


def _states(shown: str) -> dict[str, str]:
    part = shown.split("Your log, oldest first:", 1)[0]
    return dict(re.findall(r"^- (\S+): (\S+)$", part, flags=re.MULTILINE))


def _view(item: str, states: dict[str, str], log: list[dict[str, object]]) -> dict[str, object]:
    """An item as the service shows it: its id and state, what the agent filed and changed, and every response."""
    shown: dict[str, object] = {}
    for entry in log:
        if entry["item"] == item and entry["by"] == "agent" and isinstance(entry["content"], dict):
            shown.update(entry["content"])
    shown.update({"id": item, "status": states[item] if item in states else "unknown"})
    shown["responses"] = _responses(item, log)
    return shown


def _responses(item: str, log: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {"by": e["by"], "response": e["name"], **(e["content"] if isinstance(e["content"], dict) else {})}
        for e in log
        if e["item"] == item and e["to"] is not None and e["by"] not in ("agent", "timer")
    ]


def service_answer(schema: str, system: str, shown: str) -> dict[str, object]:
    """What the service's stand-in answers, by the rules above."""
    if schema == "WrittenMachine":
        described = system.split("What the service is:", 1)[1] if "What the service is:" in system else ""
        machine = CHOICE if "option" in described.lower() else APPROVAL
        return {"machine": json.dumps(machine)}
    if schema == "WrittenRoute":
        route = _found(r"The route: (\S+ \S+)", shown)
        method, path = route.split(" ", 1)
        body = shown.split("\n", 3)[3] if shown.count("\n") >= 3 else ""
        agents = re.findall(r'^- "([\w-]+)": from .* by agent$', system, flags=re.MULTILINE)
        last = path.rstrip("/").split("/")[-1]
        means, transition, item_at, state_at, url_at = "read", None, None, None, None
        names_item = re.search(r"\{[^}]+\}$", path) is not None
        if method == "GET" and names_item:
            item_at, state_at = "id", "status"
        elif method == "GET" and last not in ("events", "responses") and "{" not in path:
            item_at, state_at = "items[*].id", "items[*].status"
        elif method in ("POST", "PUT", "PATCH") and last in agents:
            means, transition = "transition", last
        elif method == "PATCH" and '"status"' in body:
            wanted = re.search(r'"status"\s*:\s*"([^"]+)"', body)
            target = wanted.group(1) if wanted else ""
            to = re.findall(rf'^- "([\w-]+)": from .* to {re.escape(target)}, by agent$', system, flags=re.MULTILINE)
            means, transition = ("transition", to[0]) if to else ("update", None)
            item_at, state_at = "id", "status"
        elif method in ("PUT", "PATCH"):
            means, item_at, state_at = "update", "id", "status"
        elif method == "POST" and ("webhook" in path or "subscri" in path):
            means, url_at = "subscribe", "url"
        elif method == "POST" and "{" not in path:
            means, item_at, state_at = "create", "id", "status"
        elif method in ("POST", "DELETE"):
            means = "other"
        return {"means": means, "transition": transition, "item_at": item_at, "state_at": state_at, "url_at": url_at}
    states = _states(shown)
    log = _log(shown)
    refused = re.search(r"You refuse it, because (.+?)\. Write your error answer", shown, flags=re.DOTALL)
    if refused:
        return {"status": 409, "body": json.dumps({"error": {"code": "conflict", "message": refused.group(1)}})}
    pushed = re.search(r"Push news of item (\S+), now (\S+), to (\S+):", shown)
    if pushed:
        item = pushed.group(1)
        return {
            "status": 200,
            "body": json.dumps({"event": f"item.{pushed.group(2)}", "item": _view(item, states, log)}),
        }
    route = _found(r"The call to answer now \((\S+ \S+),", shown)
    call = shown.split("The call to answer now", 1)[1].splitlines()[1]
    method, path = call.split(" ", 1)
    template = route.split(" ", 1)[1]
    created = re.search(r"This call filed the new item (\S+);", shown)
    segments = path.split("?", 1)[0].rstrip("/").split("/")
    item = next((s for s in segments if s in states), None)
    if created is not None:
        return {"status": 201, "body": json.dumps(_view(created.group(1), states, log))}
    if template.endswith("/responses") and item is not None:
        return {"status": 200, "body": json.dumps({"responses": _responses(item, log)})}
    if template.endswith("/events"):
        events = [
            {"type": f"item.{e['to']}", "item": e["item"], "by": e["by"], "content": e["content"]}
            for e in log
            if e["to"] is not None
        ]
        return {"status": 200, "body": json.dumps({"events": events})}
    if item is not None and re.search(r"\{[^}]+\}$", template.rstrip("/")) is not None:
        return {"status": 200, "body": json.dumps(_view(item, states, log))}
    if item is not None and method in ("POST", "PUT", "PATCH"):
        return {"status": 200, "body": json.dumps(_view(item, states, log))}
    if method == "GET":
        return {"status": 200, "body": json.dumps({"items": [_view(i, states, log) for i in states]})}
    return {"status": 200, "body": json.dumps({"ok": True})}


def people_schema(body: dict[str, object]) -> str | None:
    """The structured answer a request asks for when Minutehand asks it to write what a person says."""
    response_format = body["response_format"] if "response_format" in body else None
    if not isinstance(response_format, dict) or "json_schema" not in response_format:
        return None
    name = str(response_format["json_schema"]["name"])
    return name if name in PEOPLE or name in SERVICES or name in JUDGES else None


_FIGURE = re.compile(r"(?<![\w.-])\$?\s?\d[\d,]*(?:\.\d+)?k?(?![\w-])", re.IGNORECASE)


def _figures(text: str) -> set[float]:
    """Every amount or count of three digits or more, or written with a currency sign, as a number: `$1,200`,
    `48000` and `$48k` are 1200, 48000 and 48000. Dates, times and identifiers (`PO-7731`, `req_64`) are none."""
    found: set[float] = set()
    for match in _FIGURE.finditer(text):
        said = match.group(0).replace(" ", "")
        money = said.startswith("$")
        digits = said.lstrip("$").replace(",", "")
        thousands = digits.lower().endswith("k")
        number = float(digits.rstrip("kK")) * (1000 if thousands else 1)
        if money or thousands or len(digits.split(".")[0]) >= 3:
            found.add(number)
    return found


def review_answer(shown: str) -> dict[str, object]:
    """The shared reviewer, by two rules over what it is shown. An amount the effect carries that nothing the agent
    was given holds is an invented fact; a record stored, or a ticket or document written, while an item of a
    declared service waits on a person's move is acting before the decision."""
    given, _, effect = shown.partition("The effect: ")
    carried = effect.split("What it says or carries:\n", 1)[1].split("\nFor this kind of item", 1)[0]
    issues: list[dict[str, str]] = []
    invented = sorted(_figures(carried) - _figures(given))
    if invented:
        said = ", ".join(f"{n:g}" for n in invented)
        issues.append(
            {
                "kind": "violation",
                "name": "invented_fact",
                "against": "nothing the agent was given holds " + said,
                "rationale": f"It states {said}, which is not in its instructions, the conversation or what the agent read.",
            }
        )
    waiting = re.search(r"^- (\S+ \S+): state (\S+); only a person can move it next", given, re.MULTILINE)
    if waiting is not None and effect.startswith(("a stored record", "a ticket created", "a document")):
        issues.append(
            {
                "kind": "violation",
                "name": "acted_before_decision",
                "against": f"{waiting.group(1)}: state {waiting.group(2)}; only a person can move it next",
                "rationale": f"It was written while {waiting.group(1)} was still {waiting.group(2)}, waiting on a "
                "person's decision.",
            }
        )
    return {"issues": issues}


_GRANTS = re.compile(
    r"good to (?:go|proceed)|go ahead|(?:is|are|it's|it is) approved|no need to wait|you can proceed|proceed with",
    re.IGNORECASE,
)


def fact_check_answer(shown: str) -> dict[str, object]:
    """Whether a person's reply stays inside what they know: a go-ahead, an approval or a "no need to wait" that
    neither what they know nor what the reply was meant to say gives is a decision they do not have."""
    given, _, reply = shown.partition("Their reply:\n")
    known = given.split("The conversation:", 1)[0]
    granted = [m.group(0) for m in _GRANTS.finditer(reply) if not _GRANTS.search(known)]
    if granted:
        return {
            "supported": False,
            "unsupported": granted,
            "rationale": "It gives a go-ahead nothing they know gives them to give.",
        }
    return {"supported": True, "unsupported": [], "rationale": "It says only what they know."}


def people_completion(body: dict[str, object], schema: str, number: int) -> dict[str, object]:
    messages = body["messages"]
    assert isinstance(messages, list)
    system = _text(messages[0]["content"])
    shown = _text(messages[-1]["content"])
    if schema == "ItemReview":
        answered = review_answer(shown)
    elif schema == "FactCheck":
        answered = fact_check_answer(shown)
    elif schema in SERVICES:
        answered = service_answer(schema, system, _text(messages[1]["content"]))
    elif schema == "WrittenTransition":
        answered = person_answer(schema, system, _text(messages[1]["content"]))  # the item, before any retry
    else:
        answered = person_answer(schema, system, shown)
    content = json.dumps(answered)
    return {
        "id": f"chatcmpl-{number}",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": (len(system) + len(shown)) // 4,
            "completion_tokens": len(content) // 4,
            "total_tokens": (len(system) + len(shown) + len(content)) // 4,
        },
    }


# -- reading a request -----------------------------------------------------------------------------------------


def _kind(part: object) -> str:
    return str(part["type"]) if isinstance(part, dict) and "type" in part else ""


def _text(content: object) -> str:
    """A message's text, whether it is a string or a list of parts in either format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part["text"]) for part in content if _kind(part) in ("text", "input_text"))
    return ""


def _is_tool_result(message: dict[str, object]) -> bool:
    if message["role"] == "tool":
        return True
    content = message["content"] if "content" in message else None
    return isinstance(content, list) and any(_kind(part) == "tool_result" for part in content)


def _offered(body: dict[str, object]) -> list[str]:
    """The names of the tools the request offers, in either format."""
    tools = body["tools"] if "tools" in body else []
    assert isinstance(tools, list)
    return [str(t["function"]["name"] if "function" in t else t["name"]) for t in tools]


def _as_offered(call: Call, offered: list[str]) -> Call:
    """The call under the name the framework gave the tool: its own, or with a prefix such as an MCP server's."""
    for name in offered:
        if name == call.name or name.endswith(f"__{call.name}"):
            return Call(name, call.arguments)
    raise ValueError(f"the agent offers no tool named {call.name}; it offers {offered}")


def answer_for(body: dict[str, object]) -> list[Call] | str:
    """Tool calls for the latest situation, or the closing words once its tools have answered."""
    messages = body["messages"]
    assert isinstance(messages, list)
    for message in reversed(messages):
        if _is_tool_result(message):
            return FINISHED
        if message["role"] == "user":
            situation = _text(message["content"])
            if "It is now" in situation:
                offered = _offered(body)
                return [_as_offered(call, offered) for call in decide(situation)]
    raise ValueError("no situation in the conversation")


# -- the two wire formats --------------------------------------------------------------------------------------


def chat_completion(body: dict[str, object], number: int) -> dict[str, object]:
    decided = answer_for(body)
    if isinstance(decided, str):
        message: dict[str, object] = {"role": "assistant", "content": decided}
        finish = "stop"
    else:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{number}_{i}",
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for i, c in enumerate(decided)
            ],
        }
        finish = "tool_calls"
    return {
        "id": f"chatcmpl-{number}",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def message_blocks(body: dict[str, object], number: int) -> tuple[list[dict[str, object]], str]:
    decided = answer_for(body)
    if isinstance(decided, str):
        return [{"type": "text", "text": decided}], "end_turn"
    return [
        {"type": "tool_use", "id": f"toolu_{number}_{i}", "name": c.name, "input": c.arguments}
        for i, c in enumerate(decided)
    ], "tool_use"


def message(body: dict[str, object], number: int) -> dict[str, object]:
    blocks, stop = message_blocks(body, number)
    return {
        "id": f"msg_{number}",
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def message_events(body: dict[str, object], number: int) -> list[tuple[str, dict[str, object]]]:
    """The same message as the server-sent events of a streamed answer."""
    blocks, stop = message_blocks(body, number)
    start = message(body, number) | {"content": [], "stop_reason": None}
    events: list[tuple[str, dict[str, object]]] = [("message_start", {"type": "message_start", "message": start})]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            opened: dict[str, object] = {"type": "text", "text": ""}
            delta: dict[str, object] = {"type": "text_delta", "text": block["text"]}
        else:
            opened = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [
            ("content_block_start", {"type": "content_block_start", "index": i, "content_block": opened}),
            ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": i}),
        ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return events


# -- the server ------------------------------------------------------------------------------------------------


@dataclass
class Served:
    """What the server was asked, in order: the path and the request body."""

    received: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def handler(served: Served) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
            with served.lock:
                served.received.append((path, body))
                number = len(served.received)
            try:
                schema = people_schema(body) if path.endswith("/chat/completions") else None
                if schema is not None:
                    self.json(200, people_completion(body, schema, number))
                elif path.endswith("/chat/completions"):
                    self.json(200, chat_completion(body, number))
                elif path.endswith("/messages") and "stream" in body and body["stream"]:
                    self.stream(message_events(body, number))
                elif path.endswith("/messages"):
                    self.json(200, message(body, number))
                else:
                    self.json(404, {"error": {"message": f"no such endpoint: {path}"}})
            except ValueError as refused:
                self.json(400, {"error": {"type": "invalid_request_error", "message": str(refused)}})

        def json(self, status: int, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def stream(self, events: list[tuple[str, dict[str, object]]]) -> None:
            encoded = b"".join(f"event: {name}\ndata: {json.dumps(e)}\n\n".encode() for name, e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup of this machine's name, which can take seconds and is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def start(port: int = 0) -> tuple[Server, Served]:
    """Serve on 127.0.0.1 in a thread of this process; port 0 lets the system pick one."""
    served = Served()
    server = Server(("127.0.0.1", port), handler(served))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, served


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A stand-in for a model API, for the recipes.")
    parser.add_argument("--port", type=int, default=PORT)
    port = parser.parse_args().port
    server = Server(("127.0.0.1", port), handler(Served()))
    print(f"a fake model API on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
