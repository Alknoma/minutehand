"""What the GitHub provider claims. Data only: nothing else in the provider is imported to read it.

GitHub serves its REST API and its GraphQL endpoint (`/graphql`) at the root of `api.github.com`, so nothing is
stripped. Only the reads a code-reading client makes are answered (see `README.md`), so the provider maps no
entity kind: everything it holds is a record, and a seeded ticket, document, space, sign-in or channel on it is refused.

A person is a GitHub user only when their account entry (`Person.accounts`) or the GitHub seed's `users` declares
one. Of such a user it holds the login, the id, the display name and a private email (`email: null`), and the
absence of any email. GitHub has no job title, guest, deactivated account, working hours or absence a person's
facts could become (a GitHub user's status and suspension are not served), so those facts show nothing here.
"""

from __future__ import annotations

from minutehand.domain.provider import Holds, Manifest, Tier

MANIFEST = Manifest(
    key="github",
    tier=Tier.FINISHED,
    hosts=["api.github.com"],
    holds=Holds(
        person_without_email=True,
        person_title=False,
        person_guest=False,
        person_deactivated=False,
        person_bot=False,
        person_working_hours=False,
        person_absences=False,
        account_login=True,
        account_id=True,
        account_name=True,
        account_email_hidden=True,
        ticket_key=False,
        ticket_labels=False,
        ticket_comments=False,
        ticket_number=False,
        ticket_id=False,
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
        ticket_action_moves=False,
        ticket_action_reassigns=False,
        ticket_action_comments=False,
        ticket_action_deletes=False,
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
        people_change_removed=False,
        people_change_deactivated=False,
        people_change_reactivated=False,
        sign_ins=False,
        faults=True,
    ),
)
