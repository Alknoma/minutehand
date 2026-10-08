"""Every behaviour a fake keeps is its vendor's: each row of a provider's `CLAIMS.md` is **documented**, citing the
vendor's page, or **observed**, citing the recorded evidence of the real service (a public report holding the real
answer, or a capture committed under `tests/data/`). A test of the fake itself is not evidence of the service, so a
row whose only pointer is its own test is unsourced. A row of any other class (chosen, simplified, a mix), a table
of behaviours with no class, a bullet under a heading that says "unsourced", and the words "not verified" are
unsourced too.

`OPEN` is the ratchet: the unsourced rows found today, provider by provider, each to be fixed to match the vendor or
turned into an explicit refusal. It may only shrink: a row fixed and still listed fails, and so does a new one."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ROOT / "src" / "minutehand" / "adapters" / "providers"
URL = re.compile(r"https?://[^\s|)>\]]+")
CAPTURE = re.compile(r"`?(tests/data/[^\s`|]+)`?")
NAMED_PAGE = re.compile(r"^- ([^:]+): (https?://\S+)\s*$")
NOT_KEPT = ("not carried over",)
"""A section listing an older stand-in's behaviours that were dropped describes nothing the fake does."""


@dataclass(frozen=True)
class Row:
    provider: str
    claim: str
    why: str


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split(" | ")]


def _sourced(cells: list[str], pages: dict[str, str]) -> bool:
    """Whether the row names a vendor page, a recorded capture that is in the repository, or a page its file names."""
    text = " ".join(cells)
    if URL.search(text):
        return True
    if any((ROOT / path).is_file() for path in CAPTURE.findall(text)):
        return True
    return any(cell in pages for cell in cells)


def unsourced(claims: Path) -> list[Row]:
    """Each row of `claims` that is neither documented with its page nor observed with its evidence."""
    provider = claims.parent.name
    lines = claims.read_text(encoding="utf-8").splitlines()
    pages = {m.group(1).strip(): m.group(2) for line in lines if (m := NAMED_PAGE.match(line))}
    found: list[Row] = []
    section = ""
    header: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            section, header = line.lstrip("#").strip().lower(), None
            continue
        if "not verified" in line.lower():
            found.append(Row(provider, line.strip()[:120], "says it is not verified"))
        elif "unsourced" in section and line.startswith("- "):
            found.append(Row(provider, line[2:].strip()[:120], "listed as unsourced"))
        if not line.startswith("|"):
            header = None
            continue
        if header is None:
            header = _cells(line)
            continue
        if set(line) <= set("|-: "):
            continue
        if any(section.startswith(s) for s in NOT_KEPT):
            continue
        cells = _cells(line)
        if "Class" not in header:
            found.append(Row(provider, cells[0], "a behaviour in a table with no class"))
            continue
        kind = cells[header.index("Class")]
        claim = cells[header.index("Claim")]
        if kind not in ("documented", "observed"):
            found.append(Row(provider, claim, f"class {kind!r}"))
        elif not _sourced(cells, pages):
            found.append(Row(provider, claim, f"{kind} with no page or recorded evidence"))
    return found


OPEN: dict[str, tuple[str, ...]] = {
    "asana": (
        'Every write body sits inside `data`; one that does not is a 400 "Missing input: data"',
        "Completing a task moves it to no section and rewrites no custom field",
        "A listing too large to answer without `limit` is a 400, never a truncated list; paged, it works",
        "An assignee that is not a user identifier, or names nobody, is a 400",
        "`setParent` refuses the task itself or its own subtask as the parent",
        "`setParent` naming a task that does not exist is a 400",
        'A comment with no text, or only spaces, is a 400 "Missing input: text"',
        "A subtask never added to a project has no membership",
        "`addTask` on another project's section adds that project and keeps the first",
        'A project name missing or blank is "name: Missing input"',
        "A project in a workspace that is not there is a 400 naming the gid",
        'A new project has one "Untitled section" and no custom fields',
        "A field already on the project is refused 400",
        "A project in a team that is not there is a 400 naming the gid",
        'In a workspace that is not an organization, asking for the caller\'s teams is a 400 "Not an organization"',
    ),
    "github": (
        '`Bearer` with no token is 401 "Bad credentials"',
        "`/languages` leaves out prose and vendored paths",
        'Contents at a ref naming no commit is 404 "No commit found for the ref …"',
        'Commits from a sha naming nothing are 404 "No commit found for SHA: …"',
        "A listed commit has author email and `html_url`, and no `files`",
        "Search matches whole tokens, never a substring",
        "Every bare term must match",
        'An unreachable `repo:` is 422 "cannot be searched"',
        "An empty `q` is 422 `missing`",
        "An unresolvable repository is `null` with a NOT_FOUND error",
        "…and the message says a query attribute must be specified",
        "`GET /repos/{owner}/{repo}/commits?sha=`",
        "`GET /repos/{owner}/{repo}/contents/{path}?ref=`",
        "`GET /repos/{owner}/{repo}/git/trees/{tree_sha}`",
        "GraphQL `Repository.object(expression:)`",
        "`GET /repos/{owner}/{repo}/commits/{ref}`, compare (`BASE...HEAD`)",
    ),
    "google_cloud_tasks": (
        "The REST routes: `POST/GET v2/{parent}/queues`, `GET/DELETE v2/{queue}`, `POST/GET v2/{queue}/tasks`, `GET/DELETE v2/{task}`",
        "The client asks for enums as numbers (`$alt=json;enum-encoding=int`): `httpMethod: 1` is POST",
        "| The exact wording of each refusal message | chosen | the refusal tests | not verified against the service |",
        "The exact wording of each refusal message",
        "Backoff starts at `minBackoff` and doubles up to `maxBackoff`; past `maxDoublings` it doubles no more",
        "gRPC: `google.cloud.tasks.v2.CloudTasks` `CreateQueue`, `ListQueues`, `GetQueue`, `DeleteQueue`, `CreateTask`, `ListTasks`, `GetTask`, `DeleteTask`, answered by the operations the REST routes answer by, over the same world",
        "| A resource name a gRPC request carries that is not a location, queue or task name is refused `INVALID_ARGUMENT` | chos",
        "A resource name a gRPC request carries that is not a location, queue or task name is refused `INVALID_ARGUMENT`",
    ),
    "google_workspace": (
        "A Doc's plain-text export and its Docs body hold the same characters",
        "A plain-text export opens with a byte-order mark (observed); it ends lines CRLF (seen only indirectly)",
        "A Doc created with no text is a section break and one newline ending at 2, without its name in it",
        "A chunk that does not start where the received bytes end, or names a total other than the one declared, is a 400 `badContent`",
        "A new presentation holds one slide and its title",
        "A `From` that is not the account's own address is replaced (observed); a send without `Date` or `Message-ID` is given them (unsourced)",
        "That refusal's status and reason (400 `channelIdNotUnique`, kept from the Drive fake)",
        "Stopping a channel already stopped is that 404 too; a Drive channel named to Calendar's `channels.stop`, or the reverse, is not found",
        'A push answered 500, 502, 503 or 504 is retried "with exponential backoff"; this fake records it and does not retry, since no schedule is documented or recorded',
        "`sendUpdates` and `acl.insert`'s `sendNotifications` are served without writing the emails Google sends to guests",
        "A history id never expires. Gmail answers a `startHistoryId` outside the range it keeps with a 404 and documents",
        "A part's bytes are served inline in `body.data`, never as an attachment id.",
    ),
    "jira": (
        "An empty body reports all four fields in one 400",
        "A key already held is a 400 on `projectKey` that names the holding project",
        "A name already held (any case) is a 400 on `projectName`",
        "An email address in `leadAccountId` is a 400 on that field",
        "The caller's own account (`/myself`) is a valid lead",
        "Exactly the keys asked for come back, each with `havePermission`",
        "One unknown key refuses the whole call, and the message names it",
        "No credentials at all is a 401",
        "`GET` and `POST /rest/api/3/search` answer 410, pointing at `/search/jql`, and write nothing",
        "An edit refused for one field (an unknown priority beside a valid summary) is a 400 and writes none of its fields",
    ),
    "microsoft": (
        "Details of an unknown team are a 404",
        'An update carrying text and a card together is 400 `BadSyntax` ("multiple skype activities"), activity unchanged',
        "The same update with the card alone is accepted",
        "A proactive create naming another bot is 400",
        "A reply inside a channel thread arrives with `;messageid=<root>` on its conversation id",
    ),
    "notion": ("An append whose `after` names a grandchild, not a direct child, is a 400 and writes nothing",),
    "slack": (
        "`as_user=1` from a bot token leaves the app as the author, with its `bot_id`",
        "History holds the roots; thread replies come from `conversations.replies`",
        "Slackbot's id is `USLACKBOT`, the same in every workspace, with no email in its profile",
        "Three seconds to acknowledge; the same `envelope_id` on each retry; a URL's ticket good for one connection",
    ),
    "youtrack": (
        "A call with no bearer token is a 401 `Unauthorized`",
        'The project slot takes a database id: any other shape is a 400 "Invalid structure of entity id" before any lookup, a well-shaped id naming nothing a 404 — [issues](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html)',
        "An issue created by project id lands in that project",
        "A body property YouTrack's issue has not got (`title`, `assignee`, `labels`…) is refused, naming it",
        "`state` at the top level of an update is refused",
        'A bundle value outside this project\'s bundle is "Value is not allowed"; one inside is written',
        "A field the project does not carry is absent from its issues and a write to it is a 404",
        "Clearing State is refused and the issue keeps its state",
        "A readable key in a link body is refused by its shape",
        "A link reads from both ends",
        "A tag is added by id: a name there is a 400, an id naming nothing a 404",
        "A search naming a value the field has not got is a 400 `invalid_query`, not an empty answer",
        "A field search leaves out an issue holding another value, a just-created one too",
        "The leader is a database id (a login is refused by shape); a short name in use is refused — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html)",
        "Hub's permissions cache lists Create Project as global",
        "The field register answers each field's type id",
        "A project made with no template carries the default template's fields, no Due Date",
        "No stock template carries Due Date",
        "Any other `template` (a project's id or short name included) is a 400 and creates nothing — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html)",
        "Attaching a field needs Update Project, which the maker of a project made through the API does not hold",
        "An attached field reaches the project's issues and takes writes",
        "An attachment's `$type` must be the field's own",
        "The field to attach is named by id; a name is refused by shape",
        "Attaching a field already present is refused",
        "A project's fields answer with the Assignee bundle's users; an unknown project's are a 404",
        "A project made through the API is teamed by its leader alone: the leader can be assigned, nobody else",
        "A team is its own entity, named after the project, counting its users, `ringId` null; members carry Hub ids",
        "An unknown project's team is a 404",
        "YouTrack's team route takes no write (405), whatever is held, and changes nothing",
        "Hub's team id is not YouTrack's",
        "Hub answers `total: 0` for a project the token may not read",
        "Hub refuses a project query it cannot read",
        "Hub lists All Users, Registered Users, then only the teams the caller may update",
        "A user joins a team through its Hub group, by Hub id",
        "A second add of the same user leaves one membership",
        "A YouTrack user id names nobody in Hub (404)",
        "A Hub add with no user id is a 400",
        "A Hub add to a group naming nothing is a 404",
        "All Users is not a project's team: an add to it is a 403",
        "An assignee off the team is a 400 in YouTrack's four-field refusal",
        "Once the user joins the team, the assignment works",
        "The same refusal reaches the create body's `customFields`",
        "The Assignee bundle and the team name the same users",
        "Reading a team needs no Update Project",
        "Reading a team without Read Project Basic is a 403 naming it",
        "A Hub add without Update Project is a 403",
        "Update Project is held per project",
        "Permissions are a user's: one taken from another user leaves the caller's",
        "The permissions cache lists a per-project permission as not global, by Hub id and key, leaving out a project it was taken from and one made through the API",
    ),
}


def test_every_claim_cites_the_vendors_page_or_recorded_evidence_of_the_real_service() -> None:
    found: dict[str, list[str]] = {}
    for claims in sorted(PROVIDERS.glob("*/CLAIMS.md")):
        for row in unsourced(claims):
            found.setdefault(row.provider, []).append(row.claim)
    stale = {p: [c for c in OPEN[p] if c not in found.get(p, [])] for p in OPEN}
    new = {p: [c for c in claims if c not in OPEN.get(p, ())] for p, claims in found.items()}
    assert not {p: c for p, c in new.items() if c} and not {p: c for p, c in stale.items() if c}, (
        f"unsourced and not listed: {new}; listed but now sourced (drop them from OPEN): {stale}"
    )


def test_a_row_with_only_its_own_test_as_evidence_is_unsourced(tmp_path: Path) -> None:
    claims = tmp_path / "fake" / "CLAIMS.md"
    claims.parent.mkdir()
    claims.write_text(
        "| Claim | Class | Test | Source |\n|---|---|---|---|\n"
        "| A thing seen | observed | `test_a_thing_seen` | |\n"
        "| A thing read | documented | `test_a_thing_read` | https://vendor.example/page |\n"
        "| A thing picked | chosen | `test_a_thing_picked` | it seemed better |\n"
        "| A thing captured | observed | `test_x` | `tests/data/google_calendar_v3/discovery-retrieved-2026-10-08.json` |\n"
        "| A thing captured elsewhere | observed | `test_x` | `tests/data/nowhere.json` |\n",
        encoding="utf-8",
    )
    assert [(r.claim, r.why) for r in unsourced(claims)] == [
        ("A thing seen", "observed with no page or recorded evidence"),
        ("A thing picked", "class 'chosen'"),
        ("A thing captured elsewhere", "observed with no page or recorded evidence"),
    ]
