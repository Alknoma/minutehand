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
    Holds,
    Manifest,
    Tier,
)
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="asana",
    tier=Tier.FINISHED,
    hosts=["app.asana.com"],
    path_prefix="/api/1.0",
    kinds=[EntityKind.TICKET, EntityKind.COMMENT],
    holds=Holds(
        person_without_email=True,
        person_title=False,
        person_guest=False,
        person_deactivated=True,
        person_bot=False,
        person_working_hours=False,
        person_absences=False,
        account_login=False,
        account_id=True,
        account_name=True,
        account_email_hidden=True,
        ticket_key=False,
        ticket_labels=True,
        ticket_comments=True,
        ticket_number=False,
        ticket_id=True,
        document_spreadsheet=False,
        document_presentation=False,
        document_file=False,
        document_folder=False,
        document_owner=False,
        document_space=False,
        document_shared_with=False,
        document_modified_before_start=False,
        document_modified_by=False,
        document_id=False,
        channel_named=False,
        channel_direct=False,
        channel_private=False,
        channel_archived=False,
        channel_topic=False,
        channel_purpose=False,
        channel_without_agent=False,
        channel_history=False,
        channel_threads=False,
        channel_files=False,
        channel_id=False,
        channel_post_id=False,
        space_spaces=False,
        space_id=False,
        ticket_action_moves=True,
        ticket_action_reassigns=True,
        ticket_action_comments=True,
        ticket_action_deletes=True,
        messaging_posts=False,
        messaging_edits=False,
        messaging_deletes=False,
        messaging_reacts=False,
        messaging_joins=False,
        messaging_adds_agent=False,
        messaging_opens_agent=False,
        messaging_commands=False,
        document_change_edited=False,
        document_change_renamed=False,
        document_change_moved=False,
        document_change_shared=False,
        document_change_trashed=False,
        document_change_commented=False,
        document_change_field_set=False,
        people_change_removed=True,
        people_change_deactivated=False,
        people_change_reactivated=False,
        sign_ins=False,
        faults=True,
    ),
)
