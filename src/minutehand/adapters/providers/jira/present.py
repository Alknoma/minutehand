"""What Jira alone can say of a run, for its export (`minutehand export`, docs/exporting.md): the key each issue
shows (`ENG-12`), which a person reads on it and the agent writes in its messages.

Read from the site as the run's store holds it at its head; nothing here writes.
"""

from __future__ import annotations

from minutehand.adapters.providers.jira.state import JiraWorld
from minutehand.domain.presentation import PresentedReference
from minutehand.ports.store import Store


def references(store: Store) -> list[PresentedReference]:
    """Every issue's key, by the id the run's events name it by; none when the run's world has no Jira site."""
    jira = JiraWorld(store)
    if not jira.seeded():
        return []
    return [PresentedReference(item=issue.id, reference=issue.key) for issue in jira.every_issue()]
