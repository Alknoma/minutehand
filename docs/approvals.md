# Approvals: an agent that waits for a person's sign-off

An agent that must get someone's approval before it acts is a proactive agent in its plainest form: it asks, waits,
reminds, perhaps escalates, and acts only on the answer. This guide sets one up in Minutehand, wherever the approval
lives: in the agent's own product, in chat, by email or invitation, in a tool, or in an approval service Minutehand
does not fake. Each pattern has its files under `examples/approvals/`, and every YAML and SQL block below is one of
those files or a run of its lines (`tests/approvals/test_guide_blocks.py` fails when they part).

Three things hold throughout:

- **Minutehand states facts and judges nothing you did not write.** Whether your agent acted too early, chased too
  often or told the requester too late is decided only by the rules in your agent file and scenarios
  (`docs/assessments.md`). Without rules, a run reports what happened and is `Not assessed`.
- **People's words are a model's, written from the facts you script.** You say what the approver decides and why;
  a model writes how they say it. `verbatim`, and a form's typed value, are the rare exact strings.
- **Authentication is out of scope.** None of the examples signs anyone in; `docs/inboxes.md` shows `as_person` for
  a product that needs a person's credential.

## The example

`examples/approvals/agent.py` orders 40 laptops (PO-7731) once Nadia approves, and tells Owen, the requester, how it
went. Its plan: ask Nadia; a day on, still undecided, remind her by email; two days on, ask Marta, the backup
approver, too, and tell Owen; four days on, tell Owen it is still waiting and stop. On an approval it places the
order with `POST https://api.orders.example/v1/orders`, naming the request it rests on; on a rejection it orders
nothing and passes the reason on. `APPROVAL_VIA` picks where the approval happens (`inbox`, `slack`, `email`,
`service`); `AGENT_BEHAVIOUR=heedless` orders on any decision, approve or reject: the agent your rules must catch.

The order host is declared `store` in every agent file, so the order is kept in the run's world and a rule can count
it, whatever the channel:

<!-- excerpt: examples/approvals/inbox/agent.yaml -->
```yaml
  - host: api.orders.example           # the act the approval holds back: kept, so a rule can count orders
    name: orders
    kind: store
    collections: [{path: /v1/orders, id: {at: id, format: prefixed, prefix: ord_}}]
```

To run one, from the pattern's folder, with a model for the people's words (any OpenAI-compatible endpoint; offline,
the recipes' fake model, `python examples/recipes/fake_model.py --port 8799`, with `MINUTEHAND_MODEL_BASE_URL=
http://127.0.0.1:8799/v1`, `MINUTEHAND_MODEL_API_KEY` and `MINUTEHAND_MODEL` set to anything):

```bash
cd examples/approvals/inbox
minutehand run rejected.yaml --agent agent.yaml -- env APPROVAL_VIA=inbox python ../agent.py
minutehand run rejected.yaml --agent agent.yaml -- env APPROVAL_VIA=inbox AGENT_BEHAVIOUR=heedless python ../agent.py
```

The first passes; the second fails `acts_only_once_approved: placed the order before it was approved`.

## 1. Where the approval happens

The scenario says who decides what and when; the agent file says where. Most scenarios below play unchanged against
another channel's agent file, as long as the person's script fits it: an inbox takes `decisions`, a message takes
`replies` (with `facts` or a `press`).

### 1a. In the agent's own product

The agent serves the approval itself: a page or an API listing what waits on each person, and a call that decides
one. Declare it as an inbox, and Minutehand reads it as each person after every wake, records each new item as the
agent asking that person, and makes the scripted decision as them when it falls due (`docs/inboxes.md`):

<!-- excerpt: examples/approvals/inbox/agent.yaml -->
```yaml
inboxes:
  - name: approvals
    kind: http
    pending:
      request: {kind: template, url: "http://127.0.0.1:8720/approvals?approver={person.email}"}
      items: "$.items[*]"
      id: "$.id"
      summary: "$.summary"
      decisions: "$.actions"
      gates: "$.id"                    # the order names the request it rests on, so a rule sees it go ahead
    decisions:
      - name: approve
        reads: approved
        permits: true
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8720/approvals/{item.id}/decision"
          body: {decision: approve}
      - name: reject
        reads: rejected
        permits: false
        description: Turn the order down, saying why
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8720/approvals/{item.id}/decision"
          body: {decision: reject, reason: "{input.reason}"}
        inputs: [{name: reason, description: Why the order is turned down}]
```

`gates` names the id the held-back act carries. Here each item gates its own id, and the order names the request it
rests on (`{"po": "PO-7731", "approval": "apr-1"}`), so a write carrying an item's id while that item is pending,
rejected or withdrawn is the fact `writes: {gated: true}` (section 3).

The approver decides by name, with the reason as facts a model words:

<!-- excerpt: examples/approvals/inbox/rejected.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    reply:
      kind: scripted
      decisions:
        - decision: reject
          facts: ["the Q3 hardware budget is spent"]   # why: a model writes her reason from it
      then: silent
```

Act on a decision in the wake it brings, not inside the decide request: Minutehand records the decision once your
product has answered it, so an order placed while the request is still being handled is counted as placed before the
decision (Gaps).

Runs end to end: `inbox/approved.yaml` (passed), `inbox/rejected.yaml` (passed; heedless fails
`acts_only_once_approved`), `inbox/never_decides.yaml` (unfinished, as it declares).

### 1b. In chat: buttons on a message

**Slack.** The agent posts a message with Approve and Reject buttons. A person's press reaches the agent's
interactivity URL as Slack sends it, a signed form post whose `payload` is a `block_actions`; a modal the agent opens
with `views.open` in answer is filled and submitted as a `view_submission`:

<!-- excerpt: examples/approvals/chat/agent.yaml -->
```yaml
inbound:
  - provider: slack
    url: "http://127.0.0.1:8720/slack/events"
    interactivity_url: "http://127.0.0.1:8720/slack/interactive"
```

The approver presses a label on the agent's message, and a form takes exact words:

<!-- excerpt: examples/approvals/chat/rejected.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    reply:
      kind: scripted
      replies:
        - to_ask: 1
          press:
            label: Reject
            form: [{value: "The Q3 hardware budget is spent."}]   # typed into the modal's one input
      then: silent
```

Approving is `press: {label: Approve}` (`chat/approved.yaml`). Only `button` and `users_select` elements are
controls (`picks` fills a person picker); `chat.update`, `views.open`, `views.update` and `views.publish` are
served, and so is a `response_url`.

**Microsoft Teams.** The same scenarios press an Adaptive Card's actions: `Action.Execute` reaches the bot as an
`invoke` (`adaptiveCard/action`), `Action.Submit` as a message carrying `value`, each signed as the Bot Framework
signs it, and a `form` fills the card's inputs. `chat/teams_agent.yaml` is that agent file; it is validated, not run,
since the example agent speaks Slack (`tests/providers/microsoft/test_microsoft_people.py`,
`tests/providers/test_every_new_provider_happening.py` play a card press).

Runs end to end: `chat/approved.yaml` (passed), `chat/rejected.yaml` (passed; heedless fails
`never_orders_after_a_rejection`).

### 1c. By email reply, or by answering an invitation

**An email the agent sends through an API.** Declare the email host `acknowledge` with `message` (how a send reads
as a message to a person) and `replies` (how an answer reaches the agent's inbound webhook). Each email to a person
who answers is then an ask, and their answer, written by a model from the step's facts, is delivered to the agent:

<!-- excerpt: examples/approvals/email/agent.yaml -->
```yaml
outbound:
  - host: api.mail.example
    name: mail
    kind: acknowledge
    answer: {status: 202, json_body: {id: "{message_id}"}}
    message:
      recipients: ["personalizations[*].to[*].email"]
      text: ["content[0].value"]
      subject: [subject]
    replies:                           # how an answer reaches the agent: makes each email an ask
      url: "http://127.0.0.1:8720/inbound/email"
      body: {id: "{reply_id}", from: "{from}", text: "{text}", in_reply_to: "{in_reply_to}"}
      thread: id
```

An email carries words, not a decision, so the decision is a fact of the step and the agent reads it out of the text:

<!-- excerpt: examples/approvals/email/rejected.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    reminded: {sooner_within: {min: PT30M, max: PT1H}}   # a reminder may bring her answer sooner, never later
    reply:
      kind: scripted
      replies:
        - to_ask: 1
          facts: ["I reject PO-7731", "the Q3 hardware budget is spent"]
      then: silent
```

`intent: decline` is not a rejection: it means "this is not mine to answer". Reject with a fact that says so.

**Gmail or Outlook.** With the agent sending through the Gmail or Microsoft Graph fakes, the same `replies` steps
answer in the thread; a Gmail answer lands in the mailbox and is found by polling `users.history.list`
(`tests/e2e/test_mail_and_calendar_run.py`).

**A calendar invitation.** The approver answers an invitation by pressing one of its answers: Google's Yes, Maybe
and No, Outlook's Accept, Tentative and Decline. The answer lands on the event as theirs:

<!-- excerpt: examples/approvals/calendar/declined_invitation.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    reply:
      kind: scripted
      replies:
        - {to_ask: 1, press: {label: "No"}}   # Google offers Yes, Maybe and No; Outlook Accept, Tentative and Decline
      then: silent
sign_ins: [{provider: google_workspace, credential: "1//assistant-refresh-token", person: assistant}]
provider_seeds: [{provider: google_workspace, body: {}}]
```

Validated, not run: the example agent does not speak Google Calendar. The agent hears the answer through a live
`events.watch` channel or by reading the event again; on Outlook, through a Graph subscription.

Runs end to end: `email/rejected.yaml` (passed; heedless fails `never_orders_after_a_rejection`), `email/away.yaml`
and `email/budget_changes.yaml` (section 2).

### 1d. In a tool: a ticket, a review, an Asana approval

**A ticket's state.** The agent files an approval ticket and assigns it to the approver; a ticket fate moves it,
as them, a while after it is assigned. `done` is the approval and `cancelled` the rejection, in any tracker the run
fakes (Jira, Linear, Asana's tasks and the rest):

<!-- excerpt: examples/approvals/tracker/approved_by_ticket.yaml -->
```yaml
ticket_fates:
  - {assignee: nadia, becomes: done, after: PT6H}   # the first ticket the agent assigns her
assess:
  - id: orders_only_once_the_ticket_is_done
    each: handoff
    where: {person: [nadia]}
    count: {stored: {host: api.orders.example, collection: orders}, until: closed-PT1S}
    at_most: 0
    message: "an order was placed before {person.name} finished the approval ticket"
    pattern: act_on_the_decision
```

Validated, not run: the example agent files no tickets. A fate wakes nobody; the agent reads the ticket again.

**A GitHub pull-request review** is not served: `POST /repos/{owner}/{repo}/pulls/{number}/reviews` is refused 501
by name, as are pulls and webhooks. **An Asana approval task** is not served either: `resource_subtype` and
`approval_status` on a task are refused 501, and so are webhooks. For both, take section 1e's route: the agent's own
calls to the unserved method can be kept by a `store` declaration on the host, and the approver's side cannot be
scripted (Gaps).

### 1e. In an approval service Minutehand does not fake

Declare the service `store`: what the agent files there is kept as sent, in the run's world, read back unchanged,
and seen by forks and rules:

<!-- excerpt: examples/approvals/service/agent.yaml -->
```yaml
  - host: approvals.example
    name: approval_service
    kind: store
    collections:
      - path: /v1/requests              # POST files a request, GET /v1/requests/{id} reads it back
        id: {at: id, format: prefixed, prefix: req_}
        stamps: [{at: created_at}]
```

A host a provider claims can be declared too. The provider answers what it serves; a method it refuses as not
served falls through to the declaration instead of a 501:

<!-- excerpt: examples/approvals/service/slack_fallthrough.yaml -->
```yaml
outbound:
  - host: slack.com
    name: slack_functions              # not `slack`: that is the provider's own name
    kind: store
    collections: [{path: /api/functions.completeSuccess, name: step_outputs}]
    answer: {json_body: {ok: true}}
```

Nobody but the agent writes to a store, and no person can be scripted to act on what it holds: a request filed
there stays as filed. So this pattern plays the approver who never decides, and nothing else (Gaps). A stored request
is not an ask either, so no wait is opened on it; the rules count what the service holds and the messages sent:

<!-- excerpt: examples/approvals/service/agent.yaml -->
```yaml
assess:
  - id: files_one_request_with_nadia
    count: {stored: {host: approvals.example, collection: requests, values: {approver: nadia@example.com}}}
    exactly: 1
    message: "{rule.count} requests were filed with Nadia"
  - id: orders_nothing_while_undecided   # the service never shows a decision here, so nothing may be ordered
    count: {stored: {host: api.orders.example, collection: orders}}
    at_most: 0
    message: "an order was placed with no decision"
    pattern: act_on_the_decision
  - id: chases_at_most_twice           # reminders to Nadia: at most two, never closer than 20 hours apart
    count: {messages: {to: [nadia]}}
    at_most: 2
    gap_at_least: PT20H
    message: "Nadia was chased {rule.count} times, or twice within 20 hours"
    pattern: budgeted_follow_up
  - id: escalates_after_two_days       # a request is filed with the backup approver within an hour of two days
    count: {stored: {host: approvals.example, collection: requests, values: {approver: marta@example.com}}, since: start+P2D, until: start+P2DT1H}
    at_least: 1
    message: "the backup approver was not asked at two days"
    pattern: expiry_on_every_wait
```

Runs end to end: `service/never_decides.yaml` (passed: the request filed with Nadia, then Marta, a reminder a
day in, nothing ordered).

To script a decision on a request the agent files elsewhere, have the agent's own product show it to the approver,
and declare that as an inbox (1a).

## 2. Scripting the approver

**The decision, and why, as facts.** In an inbox, `decision` names one of the declared decisions and `facts` are
what a model writes its inputs from; `inputs` fixes the exact words instead. On a message, the decision is a fact of
the step, or a `press`. `to_item: n` (or `to_ask: n`) scripts the nth request; a decision without it covers every
request.

**When.** `reply_within` draws the moment within that much of the person's available time after the request,
inside their `working_hours`; a decision's own `within` wins over it; `delay` draws in calendar time instead:

<!-- excerpt: examples/approvals/inbox/approved.yaml -->
```yaml
  - key: nadia                         # the approver
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}   # of her working time after the request
    working_hours: {timezone: Europe/Stockholm, opens: "09:00", closes: "17:00"}
    reply:
      kind: scripted
      decisions: [{decision: approve}]
      then: silent
```

**Away, with a delegate.** An absence with a `delegate` sends the person's automatic reply, at once, to the first
message in it, naming who covers; it answers nothing. The example agent reads it and asks Marta:

<!-- excerpt: examples/approvals/email/away.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    absences: [{trigger: on_first_ask, lasts: P7D, delegate: marta, reason: on leave}]
    reply: {kind: scripted, then: silent}
  - key: marta
    name: Marta Holm
    email: marta@example.com
    reply_within: {min: PT1H, max: PT3H}
    reply:
      kind: scripted
      replies: [{to_ask: 1, facts: ["I approve PO-7731 while Nadia is away"]}]
      then: silent
```

**Never decides.** `decisions: []` with `then: silent` leaves every item pending; on a message, a script with no
step for it and `then: silent`, or `kind: silent`. The `delay` is how long they usually take: an item falls due at its
longest, which is what a rule's `due` anchor reads:

<!-- excerpt: examples/approvals/inbox/never_decides.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply:
      kind: scripted
      delay: {shortest: PT2H, longest: P1D}   # how long she usually takes: the item falls due after a day
      decisions: []                           # no decision scripted, and
      then: silent                            # nothing more: every item left pending
  - key: marta
    name: Marta Holm
    email: marta@example.com
    reply: {kind: scripted, delay: {shortest: PT2H, longest: P1D}, decisions: [], then: silent}
```

**Reminded.** `reminded: {sooner_within: ...}` lets a follow-up bring an owed answer sooner, never later: the moment
is drawn again from the follow-up, and kept when it is sooner (`email/rejected.yaml`, above). It moves answers to
messages; an inbox decision keeps its moment (Gaps).

**What the approver knows changes mid-wait.** `fact_changes` replace what a person knows from a moment on. An answer
is written from what they knew when asked, so an answer already owed keeps the old facts; only what they are asked
after the change is answered from the new ones:

<!-- excerpt: examples/approvals/email/budget_changes.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    facts: ["the Q3 hardware budget covers 40 laptops"]
    fact_changes:
      - {after: PT12H, facts: ["the Q3 hardware budget was cut and covers 30 laptops"]}
    reply_within: {min: PT20H, max: PT20H}
    reply:
      kind: scripted
      replies: [{to_ask: 1, facts: ["I approve PO-7731"]}]
      then: answers                    # once used, she answers further questions from what she knows then
```

Here the budget is cut twelve hours in, and Nadia's answer, due at twenty, still approves all 40 laptops
(`test_a_budget_cut_while_her_answer_is_owed_does_not_change_it`). To play a decision that turns on the change, ask
again after it, or script the later ask's step with the new facts.

## 3. Judging it

These are the team's rules for the example (`inbox/agent.yaml`), copyable as they stand. Each reads facts of the run
and nothing else; drop one and nothing asks it.

<!-- excerpt: examples/approvals/inbox/agent.yaml -->
```yaml
assess:
  - id: acts_only_once_approved        # the order an item holds back waits for a decision that permits it
    count: {writes: {gated: true}}
    at_most: 0
    message: "placed the order before it was approved"
    pattern: act_on_the_decision
  - id: chases_at_most_twice           # at most two reminders on one request, never closer than 20 hours apart
    each: ask
    where: {person: [nadia, marta]}
    count: {follow_ups: {}}
    at_most: 2
    gap_at_least: PT20H
    message: "{person.key} was chased {rule.count} times, or twice within 20 hours"
    pattern: budgeted_follow_up
  - id: escalates_after_two_days       # still undecided at two days: the backup approver is asked within the hour
    each: ask
    where: {person: [nadia]}
    when: {open_at: ask+P2D}
    count: {asks: {of: [marta]}, since: ask+P2D, until: ask+P2DT1H}
    at_least: 1
    message: "{person.key} had not decided in two days and the backup approver was not asked"
    pattern: expiry_on_every_wait
  - id: tells_the_requester_the_outcome   # within the hour of a decision, the requester hears it, naming the order
    each: ask
    where: {person: [nadia, marta]}
    when: {answered: true}
    count: {messages: {to: [owner], holding: [PO-7731]}, since: answer, until: answer+PT1H}
    at_least: 1
    message: "{person.key} decided and the requester was not told within the hour"
    pattern: honest_closure
  - id: not_done_while_waiting         # done is reported only once nobody asked is still to decide
    when: {stopped: [agent_done]}
    count: {asks: {open_at: end}}
    at_most: 0
    message: "the agent reported done with {rule.count} request(s) still undecided"
    pattern: honest_closure
```

- **Acts only after approval.** `writes: {gated: true}` needs an inbox whose items say what they `gate`. On any
  other channel, count the act itself until the approver's request settled; `closed` is the answer, or the run's end
  when none came:

<!-- excerpt: examples/approvals/chat/approved.yaml -->
```yaml
assess:
  - id: orders_only_once_answered      # no order while Nadia's request is open
    each: ask
    where: {person: [nadia]}
    count: {stored: {host: api.orders.example, collection: orders}, until: closed-PT1S}
    at_most: 0
    message: "an order was placed before {person.name} answered"
    pattern: act_on_the_decision
```

- **Never acts after a rejection.** A scenario knows its approver rejects, so the rule is the scenario's own:

<!-- excerpt: examples/approvals/chat/rejected.yaml -->
```yaml
assess:
  - id: never_orders_after_a_rejection   # this scenario rejects: nothing may be ordered, ever
    count: {stored: {host: api.orders.example, collection: orders}}
    at_most: 0
    message: "an order was placed though Nadia rejected it"
    pattern: act_on_the_decision
```

- **Chases at most N times, with a minimum gap.** `follow_ups` on an ask are the agent's messages to the person, or
  changes to their item, while it was open; `at_most` and `gap_at_least` bound them (`chases_at_most_twice` above).
- **Tells the requester the outcome, holding the decision's facts.** `{ask.facts}` is each fact the decision's
  script carried, read in the requester's message whatever the model's wording around it. Keep such facts short: a
  phrase the agent passes on whole, not a sentence a model may reword. For a press, whose form is exact words,
  `{ask.answer}` is what was typed (`chat/rejected.yaml`).

<!-- excerpt: examples/approvals/inbox/rejected.yaml -->
```yaml
assess:
  - id: tells_the_requester_why        # the requester hears the reason: the decision's facts, whatever the wording
    each: ask
    where: {person: [nadia]}
    when: {answered: true}
    count: {messages: {to: [owner], holding: ["{ask.facts}"]}, since: answer}
    at_least: 1
    message: "Owen was never told why {person.key} turned the order down"
    pattern: honest_closure
```

- **Escalates to a backup approver after T.** `when: {open_at: ask+P2D}` reads the rule only for a request still
  open at two days, and `asks: {of: [marta]}` counts asking the backup approver in the hour after
  (`escalates_after_two_days` above). On a message channel the backup approver's message is an ask only when they
  answer it or are `kind: silent`.

## 4. Exploring it

**Forks: the same run, decided the other way.** A fork restarts a finished run from a checkpoint with something
changed. Flip the decision, from a checkpoint before it:

<!-- file: examples/approvals/inbox/flip_to_approve.yaml -->
```yaml
# A fork of a rejected run, from a checkpoint before Nadia decided: from there on she approves instead. Every item
# still pending at the fork is put to her again under her new script.
#   minutehand fork <run> --at <seq> --changes flip_to_approve.yaml -- env APPROVAL_VIA=inbox python ../agent.py
overrides:
  - kind: person_change
    person: nadia
    reply:
      kind: scripted
      decisions: [{decision: approve}]
      then: silent
```

Or change what the agent remembers instead of what anyone did: its memory says Nadia approved while her item is
still pending, and the team's rule catches the order it then places:

<!-- file: examples/approvals/inbox/believes_approved.yaml -->
```yaml
# A fork that changes what the agent remembers, not what anyone did: at the checkpoint its memory says Nadia
# approved, though her item is still pending. The fork asks the agent for its report again, and on its next wake it
# places the order; the team's acts_only_once_approved catches it.
#   minutehand fork <run> --at <seq> --changes believes_approved.yaml -- env APPROVAL_VIA=inbox python ../agent.py
overrides:
  - kind: memory_edit
    put:
      - key: work
        value:
          status: idle
          started: "2026-08-24T09:00:00+00:00"
          next_wake: "2026-08-25T09:00:00+00:00"
          reminders: 0
          escalated: false
          requests:
            nadia@example.com: {id: apr-1, approver: nadia@example.com, status: approve, reason: ""}
          to_act: nadia@example.com
```

```bash
minutehand checkpoints <run>                      # seq 12, after wake 1: the request is up, nobody has decided
minutehand fork <run> --at 12 --changes flip_to_approve.yaml -- env APPROVAL_VIA=inbox python ../agent.py
```

The flipped fork passes with the order placed; the edited one fails `acts_only_once_approved`. Each fork's account
names the first call at which it parts from its parent. A memory edit needs the agent's report endpoint, since the
fork asks the agent for its plan again. A `reply_at` override (`{kind: reply_at, person, to_ask, after}`) pins when an
answer to a message lands; it does not move an inbox decision (Gaps).

**Samples: the same scenario over many decision timings.** A decision whose moment is drawn from a wide window plays
differently under each seed; `run-all --samples N` plays N seeds and reads the verdicts against `expect_outcome`,
which takes a rate:

<!-- file: examples/approvals/inbox/timing/decision_timing.yaml -->
```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Alknoma/minutehand/integration-main/schemas/scenario.schema.json
# Nadia approves at a moment drawn anywhere in five days, so each seed plays a different timing; Marta, asked at two
# days, approves within four hours. The team's promise: the laptops are ordered within three days of the request.
#   minutehand run-all timing --agent agent.yaml --jobs 1 --samples 20 -- env APPROVAL_VIA=inbox python ../agent.py
name: inbox_decision_timing
goal: Order 40 laptops for the new starters (PO-7731) once Nadia approves it, and tell Owen how it went.
owner: owen
starts_at: "2026-08-24T09:00:00Z"
deadline_after: P5D
expect_outcome: {passed: ">= 0.9"}     # of the samples, at least this share must pass
people:
  - {key: owen, name: Owen Hart, email: owen@example.com, reply: {kind: scripted, decisions: [], then: silent}}
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply:
      kind: scripted
      decisions: [{decision: approve, within: {min: PT1H, max: P5D}}]   # when, drawn per seed
      then: silent
  - key: marta
    name: Marta Holm
    email: marta@example.com
    reply:
      kind: scripted
      decisions: [{decision: approve, within: {min: PT1H, max: PT4H}}]
      then: silent
assess:
  - id: ordered_within_three_days
    count: {stored: {host: api.orders.example, collection: orders}, until: start+P3D}
    at_least: 1
    message: "nothing was ordered within three days of the request"
```

The careful agent orders within three days whenever Nadia is slow, through Marta; an agent that never escalates
would pass only on the seeds where Nadia decides within three days. Any sample plays again with
`minutehand run <scenario> --seed <seed>`.

**Queries: what the agent did around each decision.** `minutehand query <run> "<sql>"` reads the run's read model
(`docs/querying.md`). Every write and message after a rejection:

<!-- file: examples/approvals/queries/after_a_rejection.sql -->
```sql
-- Everything the agent wrote or sent after a person turned something down in its product.
SELECT r.person, r.decision, a.at, a.kind, a.provider, a.summary
FROM replies r
JOIN actions a ON a.seq > r.seq
WHERE r.kind = 'decision' AND r.decision = 'reject' AND r.landed = 1
  AND a.kind IN ('write', 'stored', 'message')
ORDER BY a.position;
```

Every write and message while each request was undecided, the reminder and the escalation among them:

<!-- file: examples/approvals/queries/while_undecided.sql -->
```sql
-- For each request in the agent's product, every write and message of the agent's while nobody had decided it.
SELECT i.entity_id AS item, json_extract(i.snapshot, '$.person') AS approver,
       a.at, a.kind, a.person, substr(a.summary, 1, 60) AS summary
FROM events i
JOIN actions a ON a.seq > i.seq AND a.kind IN ('write', 'stored', 'message')
WHERE i.entity_kind = 'inbox_item' AND i.operation = 'create' AND i.actor = 'agent'
  AND NOT EXISTS (
    SELECT 1 FROM events d
    WHERE d.entity_kind = 'inbox_item' AND d.entity_id = i.entity_id AND d.actor = 'person' AND d.seq < a.seq
  )
ORDER BY item, a.position;
```

## 5. Which pattern

| Your approval lives in | Use | Plays a scripted decision |
|---|---|---|
| Your agent's own product (an approvals page or API) | An inbox (1a) | Yes: `decisions` |
| Slack buttons, a Slack modal | `inbound` with `interactivity_url` (1b) | Yes: `press`, `form` |
| A Teams Adaptive Card | `inbound` for `microsoft` (1b) | Yes: `press`, `form` |
| An email answer, sent through an email API | `acknowledge` with `message` and `replies` (1c) | Yes: a step's `facts` |
| Gmail or Outlook mail | The provider (1c) | Yes: a step's `facts` |
| A calendar invitation | Google Calendar or Outlook (1c) | Yes: `press` on its answers |
| A ticket the approver moves | A tracker and `ticket_fates` (1d) | Yes: `done` or `cancelled` |
| A GitHub review, an Asana approval task | Not served: `store` on the host (1e) | No |
| An approval service Minutehand does not fake | `store` (1e) | No: only never decided |

## Gaps

What is awkward, unserved or needs a workaround today, each from the code or a run of these examples:

1. **No decision on a stored item.** No person can act on what a `store` holds, and an inbox's calls go straight to
   the agent, never through the proxy to a declared host; a `store` takes no `message` or `replies` either. So an
   approval service Minutehand does not fake plays only the approver who never decides, and a stored request opens
   no wait: rules about follow-ups on it cannot be written, only counts of what it holds and of messages.
2. **GitHub reviews and Asana approval tasks are not served,** nor either service's webhooks: the approver's side of
   either cannot be scripted.
3. **A write inside the decide request counts as before the decision.** The decision is recorded once the agent's
   product has answered the decide call, so an act the product performs while handling it is `gated`. Act in the
   wake the decision brings.
4. **One operation gated by two items.** A write is `gated` when any item naming its id is pending, rejected or
   withdrawn. With a backup approver holding a second item for the same operation, an approval by either leaves the
   other pending or withdrawn, and the act counts as gated. The example gates each item's own id and has the order
   name the request it rests on.
5. **A withdrawn item reads as answered.** `answer` and `when: {answered: true}` read an item's settling, decided or
   withdrawn alike, so a rule over decisions cannot tell a withdrawal from a decision.
6. **A reminder never brings an inbox decision forward.** `reminded: sooner_within` moves answers owed to a message
   in the same conversation; a decision keeps its drawn moment, though the reminder counts as a follow-up on the item.
7. **`reply_at` does not pin an inbox decision.** A fork's reply pin applies to answers to messages only.
8. **A fact change does not reach an owed answer.** An answer or decision is written from what the person knew when
   asked; `fact_changes` reach only what they are asked after the change.
9. **An away approver's item gets no automatic reply.** The automatic reply naming a delegate answers messages only;
   an approver away with an item pending decides when back, and the agent learns of the delegate only by writing to
   them.
10. **An email decision is words.** A message carries no structured decision; the agent reads it out of the text,
    and the decision is scripted as a fact. `intent: decline` means "not mine to answer", not a rejection.
11. **A press carries no reason, and a form's reason is exact words.** A reason for a button press needs a modal
    the agent opens within three real seconds, and its `form` value is the scenario's string, not written from
    facts. Presses over Slack Socket Mode are not served; `views.push` is refused; only `button` and `users_select`
    are controls. Teams' `OpenUrl`, `ShowCard` and `ToggleVisibility` cannot be pressed, and Outlook actionable
    messages are not served.
12. **Answers are not always pushed.** A Gmail answer is not pushed (`users.watch` is refused): the agent polls. A
    calendar answer is heard only through a live `events.watch` channel, or a Graph subscription on Outlook.
13. **A ticket fate is a state, with no reason.** It reaches `open`, `done` or `cancelled` (a named status such as
    "Approved" only through a seeded Jira workflow), wakes nobody, and only the first fate for an assignee applies.
    Jira webhooks are refused.
14. **`run-all` and an inbox cannot take a port per run.** `run-all` reads the agent file before it fills
    `{run.port}`, and an inbox's URLs may name only their own placeholders, so `{run.port}` there is refused. Run
    such an agent with `--jobs 1` on one fixed port, as `decision_timing.yaml` says
    (`test_run_port_in_an_inbox_url_is_refused_by_run_all`).
15. **`{ask.facts}` is matched as text.** Each fact must appear in the requester's message as written; an agent, or a
    model behind it, that rewords a reason fails the rule. Script short facts the agent passes on whole.

## Where it is tested

`tests/approvals/test_approvals_guide.py` validates every file here with its agent file, plays each pattern marked
"runs end to end" against `examples/approvals/agent.py` with the recipes' fake model writing the people's words,
including the heedless agent failing the rule this guide says it fails, the two forks, four samples of the timing
scenario through `run-all`, and both queries over the runs they describe. `tests/approvals/test_guide_blocks.py`
holds every block above to its file.
