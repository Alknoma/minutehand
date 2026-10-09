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

The scenario says who decides what and when; the agent file says where. Whatever the channel, what a person does to
an item waiting on them is a transition the item offers: a decision in the agent's product, a button on a message,
an answer to an invitation, a ticket's next state. A `take` in their `takes` pins one by name (`docs/scenarios.md`);
a message's words are a script step's `facts`. Most scenarios below play unchanged against another channel's agent
file, as long as what they take is offered there.

### 1a. In the agent's own product

The agent serves the approval itself: a page or an API listing what waits on each person, and a call that decides
one. Declare it as an inbox, and Minutehand reads it as each person after every wake, records each new item as the
agent asking that person, and makes the decision their take pins as them when it falls due (`docs/inboxes.md`):

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
    decisions:
      - name: approve
        reads: approved
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8720/approvals/{item.id}/decision"
          body: {decision: approve}
      - name: reject
        reads: rejected
        description: Turn the order down, saying why
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8720/approvals/{item.id}/decision"
          body: {decision: reject, reason: "{input.reason}"}
        inputs: [{name: reason, description: Why the order is turned down}]
```

Each decision is recorded as the person's transition on the item (`approve`, `reject`), at the moment they make it
and before the decide call goes out, so whatever your product writes while handling it comes after the decision. A
decide call your product refuses is recorded too, as a `refuse` transition back to pending, with its answer. An item
that leaves the list undecided is the agent taking it back: a `withdraw` transition by the agent, never a decision.
Rules read all of these with `each: transition` (section 3).

The approver's take names the decision, with the reason as facts a model words:

<!-- excerpt: examples/approvals/inbox/rejected.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    takes:
      - take: reject
        facts: ["the Q3 hardware budget is spent"]   # why: a model writes her reason from it
    reply:
      kind: scripted
      then: silent
```

`fields` fixes a decision's inputs as exact words instead (`fields: {reason: "..."}`). An inbox may also declare a
note, a decision with `settles: false` that leaves the item waiting: an away approver's automatic reply lands on
their item as that note, its first input their words (section 2).

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

The approver's take presses a button by the label she sees, on her nth ask; a form takes exact words:

<!-- excerpt: examples/approvals/chat/rejected.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    takes:
      - nth: 1                                                 # her first item: the agent's first message to her
        take: Reject                                           # the button, by the label she sees
        form: [{value: "The Q3 hardware budget is spent."}]   # typed into the modal's one input
    reply:
      kind: scripted
      then: silent
```

Approving is `takes: [{nth: 1, take: Approve}]` (`chat/approved.yaml`). Only `button` and `users_select` elements are
controls (`picks` fills a person picker); `chat.update`, `views.open`, `views.update` and `views.publish` are
served, and so is a `response_url`. A take naming no `provider` counts `nth` among the person's asks anywhere,
messages they can answer; one naming a provider counts its items there.

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

**A calendar invitation.** The approver's take answers an invitation with one of its answers: Google's Yes, Maybe
and No, Outlook's Accept, Tentative and Decline. The answer lands on the event as theirs:

<!-- excerpt: examples/approvals/calendar/declined_invitation.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}
    takes: [{nth: 1, take: "No"}]   # Google offers Yes, Maybe and No; Outlook Accept, Tentative and Decline
    reply:
      kind: scripted
      then: silent
sign_ins: [{provider: google_workspace, credential: "1//assistant-refresh-token", person: assistant}]
provider_seeds: [{provider: google_workspace, body: {}}]
```

Validated, not run: the example agent does not speak Google Calendar. The agent hears the answer through a live
`events.watch` channel or by reading the event again; on Outlook, through a Graph subscription.

Runs end to end: `email/rejected.yaml` (passed; heedless fails `never_orders_after_a_rejection`), `email/away.yaml`
and `email/budget_changes.yaml` (section 2).

### 1d. In a tool: a ticket, a review, an Asana approval

**A ticket's state.** The agent files an approval ticket and assigns it to the approver; the approver's take moves
it, as them, a while after it is assigned. A take names a state by its meaning, `done` for the approval and
`cancelled` for the rejection, or by the tracker's own name for it (a Jira workflow's "Approved", a YouTrack
State); `delete`, `comment` and `reassign` are offered to a take too, never to a model. A take naming no provider
and no `nth` covers every ticket handed to them in any tracker the run fakes; an item that does not offer it, a
message, ignores it:

<!-- excerpt: examples/approvals/tracker/approved_by_ticket.yaml -->
```yaml
    reply: {kind: scripted, then: silent}
    takes: [{take: done, after: PT6H}]   # every ticket the agent assigns her, in any tracker, done six hours on
assess:
  - id: orders_only_once_the_ticket_is_done
    each: handoff
    where: {person: [nadia]}
    count: {stored: {host: api.orders.example, collection: orders}, until: closed-PT1S}
    at_most: 0
    message: "an order was placed before {person.name} finished the approval ticket"
    pattern: act_on_the_decision
```

Validated, not run: the example agent files no tickets. The move is the person's transition on the ticket, through
the tracker as its API would show it; it wakes nobody, and the agent reads the ticket again. With a tracker listed
in the scenario's `transitions_on`, a person with no take there moves their tickets as a model picks among the
states offered.

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

**The decision, and why, as facts.** A take's `take` names what the item offers: a declared decision, a button's
label, an invitation's answer, a ticket's state. Its `facts` are what a model writes the inputs from; `fields` fixes
exact words instead, `form` fills a modal, `verbatim` gives the words of a reply. `nth: n` pins the person's nth
item (in `provider` when it names one, else the nth of their asks anywhere); a take without it covers every item that
offers it. On an email, which carries words and no decision, the decision is a fact of the script step.

**When.** `reply_within` draws the moment within that much of the person's available time after the request,
inside their `working_hours`; a take's own `within` wins over it, and its `after` fixes the moment; `delay` draws in
calendar time instead:

<!-- excerpt: examples/approvals/inbox/approved.yaml -->
```yaml
  - key: nadia                         # the approver
    name: Nadia Ek
    email: nadia@example.com
    reply_within: {min: PT2H, max: PT6H}   # of her working time after the request
    working_hours: {timezone: Europe/Stockholm, opens: "09:00", closes: "17:00"}
    takes: [{take: approve}]
    reply:
      kind: scripted
      then: silent
```

**Away, with a delegate.** An absence with a `delegate` sends the person's automatic reply, at once, to the first
message in it, naming who covers; it answers nothing. On an item of the agent's product it lands as the inbox's
note (`settles: false`), and the item still waits on them; an inbox that declares no note gets none. The example
agent reads it and asks Marta:

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

**Never decides.** No take and `then: silent` leaves every item pending; on a message, a script with no step for it
and `then: silent`, or `kind: silent`. The `delay` is how long they usually take: an item falls due at its longest,
which is what a rule's `due` anchor reads:

<!-- excerpt: examples/approvals/inbox/never_decides.yaml -->
```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    reply:
      kind: scripted
      delay: {shortest: PT2H, longest: P1D}   # how long she usually takes: the item falls due after a day
      then: silent                            # nothing pinned and nothing more: every item left pending
  - key: marta
    name: Marta Holm
    email: marta@example.com
    reply: {kind: scripted, delay: {shortest: PT2H, longest: P1D}, then: silent}
```

**Reminded.** `reminded: {sooner_within: ...}` lets a follow-up bring an owed answer sooner, never later: the moment
is drawn again from the follow-up, and kept when it is sooner (`email/rejected.yaml`, above). A message to someone
who owes a decision in the agent's product reminds them of it the same way.

**What the approver knows changes mid-wait.** `fact_changes` replace what a person knows from a moment on. An answer
owed when they change is written again as it is sent, from what the person knows then:

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

Here the budget is cut twelve hours in, and Nadia's answer, due at twenty, is worded from the cut budget; her step's
facts still say she approves PO-7731, so the order goes ahead. What a step's facts say is said whatever she knows:
to play a decision that turns on the change, script it in the step of an ask made after it.

## 3. Judging it

These are the team's rules for the example (`inbox/agent.yaml`), copyable as they stand. Each reads facts of the run
and nothing else; drop one and nothing asks it.

<!-- excerpt: examples/approvals/inbox/agent.yaml -->
```yaml
assess:
  - id: acts_only_once_approved        # no order before the first approval, from either approver; none at all without
    each: transition
    where: {provider: [approvals], name: [approve], by: [person], first: true}
    count: {stored: {host: api.orders.example, collection: orders}, until: transition-PT1S}
    at_most: 0
    message: "placed the order before it was approved"
    pattern: act_on_the_decision
  - id: never_orders_after_a_rejection # and none once a request is turned down
    each: transition
    where: {provider: [approvals], name: [reject], by: [person]}
    count: {stored: {host: api.orders.example, collection: orders}, since: transition}
    at_most: 0
    message: "placed the order after {transition.who} turned it down"
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
    each: transition
    where: {provider: [approvals], by: [person]}   # a decision; the agent's taking a request back is no decision
    count: {messages: {to: [owner], holding: [PO-7731]}, since: transition, until: transition+PT1H}
    at_least: 1
    message: "{transition.who} decided and the requester was not told within the hour"
    pattern: honest_closure
  - id: not_done_while_waiting         # done is reported only once nobody asked is still to decide
    when: {stopped: [agent_done]}
    count: {asks: {open_at: end}}
    at_most: 0
    message: "the agent reported done with {rule.count} request(s) still undecided"
    pattern: honest_closure
```

- **Acts only after approval.** `each: transition` reads the people's moves on the inbox's items; `by: [person]`
  keeps decisions and leaves out the agent's own `withdraw`. `first: true` keeps only the first approval, from
  either approver, so a backup approver's item still pending does not count against the order. With no approval at
  all, the rule reads the whole run: `until: transition-PT1S` then reaches the end, and any order fails it. On any
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

- **Never acts after a rejection.** Over an inbox, `never_orders_after_a_rejection` above counts orders from each
  `reject` on. On a message, a scenario knows its approver rejects, so the rule is the scenario's own:

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
- **Tells the requester the outcome, holding the decision's facts.** A decision's answer is what it carries: its
  inputs as given, the reason she wrote. `{ask.facts}` is each input, `{ask.answer}` all of them together (the
  decision's name when it carries none); for a message, `{ask.facts}` is each fact of the step, and for a press
  `{ask.answer}` is what was typed (`chat/rejected.yaml`). `holding` matches them as text, in any case. When the
  agent may reword, `conveys:` beside `holding` takes the same phrases and has a judge model decide whether each
  message conveys them, in any words; such a rule is read only with a judge model configured, and its findings are
  for review (`docs/assessments.md`).

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
    takes: [{take: approve}]
    reply:
      kind: scripted
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
minutehand checkpoints <run>                      # seq 13, after wake 1: the request is up, nobody has decided
minutehand fork <run> --at 13 --changes flip_to_approve.yaml -- env APPROVAL_VIA=inbox python ../agent.py
```

The flipped fork passes with the order placed; the edited one fails `acts_only_once_approved`. Each fork's account
names the first call at which it parts from its parent. A memory edit needs the agent's report endpoint, since the
fork asks the agent for its plan again. A `reply_at` override (`{kind: reply_at, person, to_ask, after}`) pins when an
answer lands; with `provider` (the inbox's name, a tracker), `to_ask` is the person's nth item there, and it pins
when that decision or move is made.

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
  - {key: owen, name: Owen Hart, email: owen@example.com, reply: {kind: scripted, then: silent}}
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    takes: [{take: approve, within: {min: PT1H, max: P5D}}]   # when, drawn per seed
    reply:
      kind: scripted
      then: silent
  - key: marta
    name: Marta Holm
    email: marta@example.com
    takes: [{take: approve, within: {min: PT1H, max: PT4H}}]
    reply:
      kind: scripted
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
SELECT r.person, r.decision, a.at, a.kind, a.provider, a.summary
FROM replies r
JOIN actions a ON a.seq > r.seq
WHERE r.kind = 'decision' AND r.decision = 'reject'
  AND a.kind IN ('write', 'stored', 'message')
ORDER BY a.position;
```

Every write and message while each request was undecided, the reminder and the escalation among them:

<!-- file: examples/approvals/queries/while_undecided.sql -->
```sql
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
| Your agent's own product (an approvals page or API) | An inbox (1a) | Yes: a take of a decision |
| Slack buttons, a Slack modal | `inbound` with `interactivity_url` (1b) | Yes: a take of a label, `form` |
| A Teams Adaptive Card | `inbound` for `microsoft` (1b) | Yes: a take of a label, `form` |
| An email answer, sent through an email API | `acknowledge` with `message` and `replies` (1c) | Yes: a step's `facts` |
| Gmail or Outlook mail | The provider (1c) | Yes: a step's `facts` |
| A calendar invitation | Google Calendar or Outlook (1c) | Yes: a take of one of its answers |
| A ticket the approver moves | A tracker, and a take (1d) | Yes: a state, `done` or `cancelled`, or the tracker's own |
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
3. **An email decision is words.** A message carries no structured decision; the agent reads it out of the text,
   and the decision is scripted as a fact. `intent: decline` means "not mine to answer", not a rejection.
4. **A press carries no reason, and a form's reason is exact words.** A reason for a button press needs a modal
   the agent opens within three real seconds, and its `form` value is the scenario's string, not written from
   facts. Presses over Slack Socket Mode are not served; `views.push` is refused; only `button` and `users_select`
   are controls. Teams' `OpenUrl`, `ShowCard` and `ToggleVisibility` cannot be pressed, and Outlook actionable
   messages are not served.
5. **Answers are not always pushed.** A Gmail answer is not pushed (`users.watch` is refused): the agent polls. A
   calendar answer is heard only through a live `events.watch` channel, or a Graph subscription on Outlook. A
   ticket's move wakes nobody, and Jira webhooks are refused.
6. **`run-all` and an inbox cannot take a port per run.** `run-all` reads the agent file before it fills
   `{run.port}`, and an inbox's URLs may name only their own placeholders, so `{run.port}` there is refused. Run
   such an agent with `--jobs 1` on one fixed port, as `decision_timing.yaml` says
   (`test_run_port_in_an_inbox_url_is_refused_by_run_all`).
7. **`conveys:` needs a judge model.** Without one, a rule using it is not read, and the run says so; with one, its
   findings are for review, not failures. `holding` stays the deterministic match.

Closed by construction, each held to a run in `tests/inboxes/test_approval_gaps.py`: a write the product makes while
handling a decision comes after it (the person's transition is recorded first); one operation with two approvers
goes ahead on the first approval (`first: true`); a request taken back is the agent's `withdraw`, never a decision
(`by`); a reminder brings an owed decision forward; a fork's `reply_at` pins an item's moment (`provider`); an
answer owed when what the person knows changes is written from what they know when they send it; an away approver's
item gets their automatic reply as a note; and a decision's answer is what it carries, matched as text by `holding`
or in any words by `conveys`.

## Where it is tested

`tests/approvals/test_approvals_guide.py` validates every file here with its agent file, plays each pattern marked
"runs end to end" against `examples/approvals/agent.py` with the recipes' fake model writing the people's words,
including the heedless agent failing the rule this guide says it fails, the two forks, four samples of the timing
scenario through `run-all`, and both queries over the runs they describe. `tests/approvals/test_guide_blocks.py`
holds every block above to its file.
