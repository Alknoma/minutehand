"""What the Asana provider claims. Data only: nothing else in the provider is imported to read it.

`FINISHED` covers what a tracker client built on Asana's REST API reaches: users and
their teams, the workspace, teams, projects (create, members, sections, custom field
settings), custom fields (enum, multi-enum, text, number, date, people) and their
values on tasks, tags, tasks (create, read, update, delete, list, search, typeahead,
subtasks, parent, section moves, tags) and comment stories; seeded tokens mapped to
users, the OAuth refresh at `/-/oauth_token`, the free-plan 402 and throttling 429. A
field or parameter Asana has and this provider does not is answered 501 by name, never
ignored, and so is `/webhooks`; a route not served at all (attachments, user task
lists, portfolios, goals) is answered Asana's 404 "No matching route for request".
"""

from __future__ import annotations

from minutehand.domain.provider import (
    AccountFact,
    Manifest,
    PersonChange,
    PersonFact,
    TicketActionKind,
    TicketField,
    Tier,
)
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="asana",
    tier=Tier.FINISHED,
    hosts=["app.asana.com"],
    path_prefix="/api/1.0",
    kinds=[EntityKind.TICKET, EntityKind.COMMENT],
    ticket_fields=[TicketField.LABELS, TicketField.COMMENTS, TicketField.ID],
    people_changes=[PersonChange.REMOVED],
    account_facts=[AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[PersonFact.WITHOUT_EMAIL, PersonFact.DEACTIVATED],
    ticket_actions=[
        TicketActionKind.MOVES,
        TicketActionKind.REASSIGNS,
        TicketActionKind.COMMENTS,
        TicketActionKind.DELETES,
    ],
)
