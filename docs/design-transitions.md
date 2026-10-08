# Transitions: one model for everything people, systems and time do

The design the owner approved. It replaces the per-service draft (`docs/services.md` on the services prototype
branch), which becomes one case of it. Built in four phases (section 6).

## The model

Everything a person, the agent, a system or time does to the world is a **transition** of one item's state:

```python
class Transition(Model):
    provider: ProviderKey  # jira, google_workspace, slack, a declared service ...
    item: EntityRef  # the ticket, invitation, conversation, request, comment
    name: str  # the provider's own name for it: "Start Progress", "accepted", "reply", "approve"
    from_state: str | None  # None when the transition creates the item
    to_state: str
    by: Actor  # AGENT | PERSON | SYSTEM | TIMER (SCENARIO stays for seeding)
    who: str | None  # a person's key, the system actor's name ("warehouse"), None for the agent or time
    content: str  # JSON: the comment, the reply's words, the reasons, the chosen option
    at: AwareDatetime
```

A transition is recorded once, in the run's log, as an event of `EntityKind.TRANSITION` beside the provider's own
entity versions, whoever made it: the agent through the provider's API, a person through the people engine, a
system actor or a timer through the dispatch table. Minutehand states it; rules judge it.

## 1. One provider port

```python
@runtime_checkable
class ProvidesTransitions(Protocol):
    def items_for(self, person: Person, world: Store) -> list[EntityRef]:
        """What waits on this person in this provider now: assigned, invited, addressed, mentioned, requested."""

    def legal(self, item: EntityRef, by: Actor, who: str | None, world: Store) -> list[Offer]:
        """The transitions this actor may take on the item now, by the provider's own semantics: Jira's workflow
        and screens, an attendee's response states, a conversation's reply, a declared machine. Each offer says
        what content it takes (its fields, required or not)."""

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: str | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """Take it through the provider's OWN code path: the same validation, history, side effects (events
        pushed, webhooks, subscriptions notified, mail landed) as when the service does it. Refuses an offer no
        longer legal, as the service refuses it."""
```

- Every provider implements it: Jira, YouTrack, Asana (tickets: workflow transitions, assignee, comments); GitHub
  (issues and reviews, once served); Google Calendar and Outlook (an invitation: needsAction → accepted, tentative,
  declined, with a comment); Slack, Teams, Gmail and Outlook mail (a conversation awaiting a reply: awaiting → replied,
  with words, a control pressed or a form filled); Drive, Docs, Notion, SharePoint (a comment thread: open → replied,
  resolved); and **declared services**, the event log and state machine of the draft, for a host no provider fakes.
- `legal` comes from what the provider already holds: Jira's workflow (`transitions` API), Google's attendee
  `responseStatus` values, Slack's controls on a message. Nothing is invented per provider beyond its documented
  semantics (CLAIMS.md cites each).
- The existing ports this subsumes are deleted in the last phase: `HoldsTickets`, `EditsTickets`, `ActsOnTickets`,
  `DeletesTickets`, `LandsReplies`, `PushesInteractions`, and the person half of `PushesEvents` and `ChangesDocuments`.
  `PushesEvents.say` (the owner's goal by message), `NotifiesChanges`, `BooksWakes` and seeding stay.

## 2. One people engine

`application/people.py` replaces the replier's reply path, the inbox decider, ticket fates and the services draft's
responses:

1. After every wake (and every `advance` of a standing world) it asks each provider `items_for(person)` for every
   scenario person and diffs against what it already holds: a new item is **pending on that person** (the ledger's
   wait), whatever it is: a ticket assigned, an invitation, a message, an approval request, a mention.
2. It books a moment for it: seeded from the run's seed, the person and the item, in their available time (working
   hours, absences), by the person's `within` (or a step's), as `moments.draw` does today. An absence with a delegate
   sends the automatic reply where the provider has one, and routes the item to the delegate. A follow-up on a
   pending item may bring it sooner (`reminded`). A fork keeps every booking before the checkpoint and draws afresh
   after it with its seed; a pin fixes one.
3. At the moment it asks the provider for the `legal` offers for that person, and the model (`person-transition/1`)
   picks one and writes its content from what the person can see (the item, their conversations, as `seen_by` does
   today), their facts at that moment, voice, helpfulness and the scenario's bias or odds. The script's role
   shrinks to what it says best: facts, a pinned offer (`take: decline`), exact words (`verbatim`), silence.
4. It validates the pick against the offers (retry once, then the failure is recorded and the item stays owed, tried
   again later), then calls `apply`. The provider does the rest exactly as its service would.
5. Every model call is kept and replayed by key, as `PersonCall` is now.

A person who **starts** something (a happening: posts, edits, opens a ticket) is a transition with no pending item,
fired from the dispatch table at its moment: the same `apply`, `by: PERSON`.

## 3. Concurrency and state flips

- All moves, the agent's included, go through the provider's legality check against the state in the log at that
  seq. An agent write against a state someone else just moved gets the service's own conflict (Jira's 400 on a stale
  transition, Google's 412 on an `If-Match`), recorded as the refused call it is.
- A person's pick is checked at the moment it is applied: if the agent moved the item in between, the offer is gone
  and the engine asks again from the new offers (or drops it when nothing is pending on the person any more).
- Timers and system actors are dispatch entries booked when an item enters a state; each fires only if the item is
  still in the state it was booked from.
- Every flip by anyone is one `Transition` in the log, so a fork at any checkpoint has every item's state.

## 4. Facts

The assessment language counts `transitions` (`provider`, `to`, `from`, `by`, `who`, `reached`, `not_reached`) and
reads a rule once per transition (`each: transition`, anchor `transition`). The read model gets `transitions` and
`items` views. Rules across providers:

```yaml
- id: orders_only_once_approved                 # a declared service
  each: transition
  where: {provider: [approvals], to: [approved]}
  count: {stored: {host: api.orders.example}, until: transition-PT1S}
  at_most: 0
- id: never_ships_unapproved                    # a declared state machine
  count: {transitions: {provider: [orders], to: [shipping_requested], not_reached: [approved]}}
  at_most: 0
- id: acts_on_a_declined_invitation             # Calendar or Outlook
  each: transition
  where: {to: [declined]}
  count: {messages: {to: [owner]}, since: transition, until: transition+P1D}
  at_least: 1
- id: no_work_on_a_ticket_moved_back            # Jira: someone reopened it
  each: transition
  where: {provider: [jira], to: [To Do], by: [person]}
  count: {transitions: {same_item: true, by: [agent], to: [Done]}, since: transition, until: transition+P1D}
  at_most: 0
```

## 5. What each mechanism becomes

| Today | Becomes | Fate |
|---|---|---|
| Scripted replies and steps (#73: `to_ask`, `facts`, `intent`, `verbatim`, `then`) | The person's facts and a per-item pin; a reply is the `reply` offer of a conversation | Kept as the script's vocabulary; the reply path is the engine |
| Inbox declarations (`inboxes`, `pending`, `decisions`) | A provider for the agent's own product, `legal` read from its declared decisions | Kept as a provider; its decider is the engine |
| Inbox `gates` / `permits` and `writes: {gated: true}` | `transitions` with `not_reached` against the item that holds it back | Fact kept until the rule shape covers it, then killed |
| `ticket_fates` | A person's transition on an assigned ticket, picked or pinned (`take: done, after: P3D`) | Killed (a fate is a pin) |
| Calendar invitation presses (Google Yes/Maybe/No, Outlook Accept/Tentative/Decline) | Invitation offers | `press` on invitations killed; the offer is the provider's |
| Slack and Teams buttons, `form`, `inputs`, `picks` | Offers of the message's controls; content fills the form | Script keys kept as pins |
| `acknowledge` + `replies` (an email API) | A conversation provider over the captured host | Kept; its delivery is the provider's `apply` |
| The `store` kind | Unchanged for what nobody responds to; a declared service when someone does | Kept |
| `--capture-unknown model` | A declared service whose machine and answers the model proposes | Folded into services (decision 5) |
| Provider happenings (`posts`, `moves`, `commented`, ...) | Person transitions with no pending item, from the dispatch table | Kept as YAML, one code path |
| The services prototype draft | The declared-service provider of this port | Its YAML stays; its engine is the people engine |

## 6. Migration, in phases

1. **Port and engine on two providers.** `ProvidesTransitions` and `application/people.py`; Jira (tickets) and Google
   Calendar (invitations) implement the port; `transitions` facts and views. Old paths stay for every other
   provider. Breaks nothing: both run side by side, selected per provider.
2. **Declared services.** The draft's code (state machine, event log, rendered answers, shapes) becomes a provider of
   the port. New YAML (`services:`); nothing breaks.
3. **The rest.** YouTrack, Asana, Outlook, Gmail, Slack, Teams, Drive, Notion, SharePoint, the inbox provider. Breaks:
   reply moments may move where a reply is now drawn per item rather than per ask; tests that pin moments are
   re-pinned. Scripts keep their keys.
4. **Removals.** `ticket_fates`, invitation `press`, the old ports, `writes: {gated}` once rules cover it. Breaks
   every scenario using them: a `minutehand migrate` writes the new YAML; the library scenarios and examples move.

## 7. Decisions

The owner approved this design with these answers:

1. **Scripts.** A scenario may pin a person's exact transition (`take: decline`) and when (`after: P3D`); without a
   pin the model picks among the legal offers.
2. **Granularity of a conversation.** One pending item per thread: the person answers the conversation, not each
   line. A follow-up in the thread is a reminder of the same item.
3. **Who responds** on a declared service: its `responders:` (scenario people) respond through a seeded draw; a
   service acting on its own is a timer or system transition.
4. **Rendered answers.** A declared service's reads may be model-rendered from its authoritative state, with a fixed
   shape per route and pinning to an OpenAPI document where one is given.
5. **`--capture-unknown model`** folds in: an undeclared host is a declared service with no description.
6. **Removal of `ticket_fates` and button `press`/form scripting** happens in phase 4; `minutehand migrate <file>`
   rewrites old YAML to the new form.
7. **Standing worlds.** `minutehand serve` gets a transitions route: the pending items and transitions of a world.

## 8. What is built

**Phase 1.** `domain/transitions.py` (`Transition`, `Offer`, `Waiting`, `transition_change`), `ports/transitions.py`
(`ProvidesTransitions`: `items_for`, `legal`, `apply`, and `heard_of`, whether the service tells the agent of a
person's move), `application/people.py` (the engine), running beside the old paths: a scenario opts a provider in
with `transitions_on`, and pins a person's move with `Person.takes` (`take`, `nth`, `after`, `verbatim` or `facts`).

- **Jira**: an issue assigned to a person and not done is pending on them; the offers are the workflow transitions
  from its status whose screen requires nothing, each with a comment; the move goes through `Desk.transition`, the
  path `POST /issue/{key}/transitions` takes. The agent's transitions and creates are recorded too.
- **Google Calendar**: an unanswered invitation is pending on its guest (the item is the event; `who` says which
  guest); the offers are `accepted`, `tentative` and `declined` with a comment; the answer is written as an answer
  always was, and tells a live `events.watch` channel, which is a wake.
- **The engine's own record** is `EntityKind.PENDING` (`PendingSnapshot`, actor SCENARIO), so a fork reads what was
  owed at its checkpoint; a booked move is a `PendingTransition` in the run loop's table (`DueKind.TRANSITION`). A
  person acts once per turn: after their own move an item waits on them again only once someone else moves it. A
  move whose words a model failed to write stays owed (`Checkpoint.untaken`) and is tried again on the next turn.
- **Facts**: `checks/facts.transitions`; the assessment language's `transitions` count and `each: transition`
  (`docs/assessments.md`); the ledger opens a wait on a guest for an invitation the engine holds, settled by their
  move. The read model's `transitions` and `items` views (`docs/querying.md`). `minutehand serve`:
  `GET /v1/worlds/{id}/transitions` (`docs/serve.md`).
- **Old paths**: a scenario without `transitions_on` plays as before. With it, the replier is not asked about an
  item the engine holds, and ticket fates beside an engine-played ticket provider are refused.

**Phase 2.** Declared services (`docs/services.md`): `domain/services.py` (the declaration and its machine),
`domain/shapes.py` (a route's fixed shape), `application/services.py` (`ServiceDesk`: the state, the routes'
meanings, rendered answers and refusals, timers and system actors, pushes; `ServiceItems`: one service as a provider
of the port), `ports/services.py`, `adapters/pushing.py`, the proxy's answer for a service's host. Every move is a
`Transition` (`EntityKind.TRANSITION`, items `EntityKind.SERVICE_ITEM`); the actors gain `system` and `timer`. A
service's responders act through the people engine (`Scenario.played()` is `transitions_on` and every service), a
provider steering them through `SteersPeople` (its `within`, `bias`, `odds`). A service's timer or system move is a
`PendingService` in the run loop's table. A rendered answer is kept with the state it was rendered in
(`ServiceRecordKind.ANSWER`), so polling an unchanged service calls no model. `--capture-unknown model` answers a
host nobody declared as a service with no description; the separate stand-in (`adapters/proxy/modeled.py`,
`CaptureMode.MODELED`) is gone.

**Phase 3.** Every other provider implements the port, and people's answers to messages are the engine's.

- **YouTrack**: an issue assigned to a person and not resolved is pending on them; the offers are every value of its
  `State` field but the current one, with a comment; the agent's and people's State changes are transitions.
- **Asana**: a task assigned to a person and not completed is pending on them; the offers are completing it and each
  section or status option it can move to, each named as Asana words it; every such change is a transition.
- **Conversations** (decision 2: one item per conversation). Each message the agent sent a person that they can
  answer where it went is an ask (`Waiting.conversation`): Slack and Teams messages (pushed to the agent, bound to its
  inbound target and signing secret through `TalksToAgent` and `application/conversations.py`), Gmail and Outlook
  email and meeting requests (landed where the agent reads them), Google invitations, a captured channel's sends
  (`adapters/agent/replies.py`), and every item of the agent's own product (`application/inboxes.py`, which offers
  the item's decisions with their inputs). The offers are `reply`, with its words, and each control on the message
  by its id; an invitation's are `reply` (a response comment) and each `responseStatus` but the one held, with a
  comment, carried as `text`. Every answer is a transition, and is kept as said (`Store.replies`) when it lands.
- **Who decides.** A take pins the nth item; a provider the scenario names in `transitions_on` has the model pick
  among the offers (`person-transition/1`); otherwise the person's script and voice plan and word the answer as they
  always did (`application/replier.py`), and the engine holds the plan and the words on its record
  (`PendingSnapshot.plan`, `.answer`) until the answer's moment. A follow-up on an answer owed is no new ask: it moves
  the answer sooner when the person is `reminded`. An ask edited before its answer is planned and worded again; one
  that, so read, needs no answer passes (`PendingStatus.PASSED`) and nobody hears of anything. A person away while a
  delegate covers sends their automatic reply at once, and the ask stays theirs. A fork that changes a person, or
  pins a moment, plans their open asks again (`People.replan`).
- **What moved for users.** The run loop's own reply path (`PendingReply`, `Checkpoint.withdrawn`,
  `Checkpoint.unwritten`) and the standing world's are gone: an answer is a `PendingTransition` due as a reply
  (`DueKind.PERSON_REPLY`). Only what landed is in the `replies` view, whose `withdrawn` and `landed` columns are
  gone (read model version 2), and a run's checkpoints sit one seq later than before, since the engine records each
  ask it holds. Answers land at the moments they did; what can shift is a reply whose words a model failed to write
  (written again on the next look, or at its moment, where the old path waited a turn), and a standing world now puts
  an edited ask to its person again, as a run does. An invitation's answer carries its comment as `text`.
- **Not built.** GitHub pull request reviews (the fake serves none) and Drive or Docs comment replies (the fake
  serves no `comments.replies`): a person cannot answer where the service does not, and nothing is invented for it.
