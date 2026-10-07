"""What the Google Cloud Tasks provider claims. Data only: nothing else in the provider is imported to read it.

`FINISHED` covers what its tests exercise through Google's own client (`google-cloud-tasks` with its REST
transport): queues created, read, listed and deleted; HTTP tasks created, read, listed and deleted; a task's
`scheduleTime` booked as a wake and its delivery made as an HTTP call to the task's URL, with Cloud Tasks'
headers and its retries. Everything else of the API answers 501. The gRPC transport, the client's default,
never reaches the proxy as HTTP and cannot be answered (see `CLAIMS.md`).
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="google_cloud_tasks",
    tier=Tier.FINISHED,
    hosts=["cloudtasks.googleapis.com"],
    kinds=[EntityKind.RECORD],
    books_wakes=True,
)
