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

from minutehand.domain.provider import AccountFact, Manifest, PersonFact, Tier

MANIFEST = Manifest(
    key="github",
    tier=Tier.FINISHED,
    hosts=["api.github.com"],
    account_facts=[AccountFact.LOGIN, AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[PersonFact.WITHOUT_EMAIL],
)
