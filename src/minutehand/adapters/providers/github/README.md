# GitHub provider

Written from GitHub's public REST and GraphQL reference, by reading it. No vendor code, client library or example
payload was copied; every body and error text here is this provider's own. `CLAIMS.md` lists the vendor
behaviours it is pinned to, where each comes from, and the test that holds it.

## Scope

It answers what two kinds of client need: a code-reading service that holds a user's personal access token and reads
repositories to answer questions about them, and an agent that works a repository's tracker and its changes: issues,
their comments and labels, pull requests with their files, commits, reviews and merges, and commits to files. People
(a scenario's) act on that tracker through the transitions port, and the agent is told of what they do by webhooks.
It does not stand in for GitHub at large.

Host: `api.github.com` (REST and `/graphql`). Not claimed: `github.com`, `raw.githubusercontent.com`,
`codeload.github.com`; a `download_url` or `html_url` an answer carries is refused by the proxy if followed.

| Call | Answered |
|---|---|
| `GET /user` | the token's user |
| `GET /user/repos?per_page&page&sort` | owned, collaborated and organization repositories; a fine-grained token's selection; `Link` paging |
| `GET /repos/{o}/{r}` | metadata, `permissions`, `language` |
| `GET /repos/{o}/{r}/languages` | bytes of code per language, no prose, data, docs or vendored paths |
| `GET /repos/{o}/{r}/branches?per_page&page` | each branch, the commit it points at, `protected: false` |
| `GET /repos/{o}/{r}/contents[/{path}]?ref` | a file as wrapped base64; past 1 MiB `"content": ""`, `"encoding": "none"`; a directory capped at 1,000 entries with nothing said |
| `GET /repos/{o}/{r}/git/blobs/{sha}` | the bytes the contents endpoint refused |
| `GET /repos/{o}/{r}/git/trees/{sha}?recursive` | one level, or every object when `recursive` has any value; `truncated` past the entry limit |
| `GET /repos/{o}/{r}/commits?sha&path&per_page&page` | newest first, `Link` paging; 409 on an empty repository |
| `GET /search/code?q&per_page&page` | see `search.py` for the qualifiers answered and refused; `text_matches` under `Accept: application/vnd.github.text-match+json` |
| `GET /rate_limit` | every budget of the caller; spends none |
| `POST /app/installations/{installation_id}/access_tokens` | 201: a `ghs_` token, `expires_at` an hour on, `permissions` as the request sent them |
| `POST /graphql` | `viewer` and `repository` with the fields listed in `graphql.py` |
| `GET /repos/{o}/{r}/issues?state&labels&assignee&creator&milestone&type&sort&direction&since` | issues and pull requests (marked by `pull_request`), `Link` paging; 404 for a repository the caller cannot see |
| `POST /repos/{o}/{r}/issues` | 201; title, body, label names as sent; labels and assignees dropped without push access |
| `GET`, `PATCH /repos/{o}/{r}/issues/{n}` | state (`closed_at`, `closed_by`, `state_reason`), title, body, labels, assignees; 403 for one who may not edit |
| `PUT`, `DELETE /repos/{o}/{r}/issues/{n}/lock` | 204; `lock_reason` of the four the reference lists |
| `GET`, `POST /repos/{o}/{r}/issues/{n}/comments`, `GET /repos/{o}/{r}/issues/comments` | comments by ascending id (or `sort` and `direction`), `since` |
| `GET`, `PATCH`, `DELETE /repos/{o}/{r}/issues/comments/{id}` | one comment; delete is 204 |
| `GET`, `POST /repos/{o}/{r}/labels`, `GET /repos/{o}/{r}/labels/{name}` | labels by name, case aside; 422 `already_exists` |
| `GET`, `POST`, `PUT`, `DELETE /repos/{o}/{r}/issues/{n}/labels`, `DELETE .../labels/{name}` | list, add, set, remove all (204), remove one (the rest) |
| `GET /repos/{o}/{r}/pulls?state&head&base&sort&direction` | the simple form, `Link` paging |
| `POST /repos/{o}/{r}/pulls`, `GET`, `PATCH .../pulls/{n}` | 201; title, body, draft kept as sent; the full form with `mergeable`, `mergeable_state`, `merge_commit_sha` and the counts of its diff |
| `GET .../pulls/{n}/files`, `GET .../pulls/{n}/commits` | the diff entries (patch where git can write only one), the commits oldest first |
| `PUT`, `GET .../pulls/{n}/merge` | a merge commit of two parents (`merge_method` merge); 405, 409; 204 once merged |
| `GET`, `POST .../pulls/{n}/reviews`, `GET .../reviews/{id}`, `GET .../reviews/{id}/comments` | APPROVE, REQUEST_CHANGES, COMMENT; the three states; no pending review |
| `GET`, `POST .../pulls/{n}/comments` | comments on a line of the diff, with `diff_hunk`, `position`, `line`, `side`; replies |
| `PUT`, `DELETE /repos/{o}/{r}/contents/{path}` | one commit on `branch` (the default branch's, or a branch of its own): 201 or 200 with the `content` and the `commit`; `sha` to replace or delete; 409 on a wrong one |

Every other method and path is refused by name: 501, "minutehand's github fake does not implement <METHOD>
<path>", never a 404 a client would take for GitHub's answer. So is `X-GitHub-Api-Version: 2026-03-10`, a version
GitHub answers and this provider does not; any other unknown version is GitHub's observed 400.

Every REST answer carries every field the reference's description requires of it
(`tests/providers/github/test_github_coverage.py`). What a seed gives (names, emails, descriptions, topics, file
bytes, commit messages) is returned exactly as given; what the provider makes up is only what GitHub assigns or
computes: ids, `node_id`s, shas, URLs, timestamps, counts, `size`, `language`, `permissions`, and GitHub's defaults
for a new repository's features (`has_issues` and the rest, from the create-repository reference).

## Coverage

`tests/providers/github/openapi/api.github.com.subset.json` is the part of GitHub's published REST description
(github/rest-api-description at `2eba8c3b`, 2026-10-08) for the reference sections this provider touches or
would be asked for next: repositories, contents, webhooks, branches, commits, git blobs, trees, commits, refs and
tags, search, users, rate limit, apps and installations, issues, issue comments, labels, pulls, review comments
and reviews. It holds 169 operations: 48 served, 121 refused by name. The coverage test fails if one is neither.
GraphQL is not in that description; `graphql.py` validates every query against its own schema and refuses what
it does not answer.

The unserved operations a GitHub client most often calls, to serve next in this order:

1. `GET /users/{username}` and `GET /orgs/{org}/members`: reading accounts (the conformance suite's accounts
   property asks for both).
2. `GET /repos/{owner}/{repo}/commits/{ref}` and `GET /repos/{owner}/{repo}/compare/{basehead}`: one commit with
   its files, and a diff.
3. `GET /repos/{owner}/{repo}/readme`, `GET /repos/{owner}/{repo}/branches/{branch}`,
   `GET /repos/{owner}/{repo}/git/ref/{ref}`, `GET /repos/{owner}/{repo}/tags`.
4. `GET /repos/{owner}/{repo}/installation` and `GET /app/installations`: finding an app's installation before
   the token exchange. After them, `POST /repos/{owner}/{repo}/git/refs` and `POST /repos/{owner}/{repo}/pulls/{n}/requested_reviewers`
   (the latter is not in the committed description yet): an agent that makes its own branch and asks for a review.

## Webhooks

When the agent declares an inbound target for GitHub (`inbound: - provider: github`, a `url`, and a `secret`), each move a
person makes is pushed to it as GitHub pushes it: `issues` (closed, reopened), `issue_comment` (created), `pull_request`
(closed, merged or not) and `pull_request_review` (submitted), a POST of JSON with `X-GitHub-Event`,
`X-GitHub-Delivery` and a `GitHub-Hookshot/` user agent, and, only where the target declares a secret,
`X-Hub-Signature-256` and `X-Hub-Signature` (`hooks.py`). A delivery the agent does not answer with 2xx fails the run,
and is not sent again. The agent's own writes through the API push nothing, and GitHub's webhook routes
(`/repos/{o}/{r}/hooks`) are refused by name.

## The tracker in a seed

A repository's seed may hold `labels` (name, color, description), `issues` and `pulls`, each numbered by the seed
(`number`: GitHub numbers issues and pull requests from one sequence, and a fragment added to an open world must not
move what the world holds), with their `comments` and, for a pull request, `reviews`, every moment an offset before
the scenario's start. `diverged_branches` gives branches commits of their own, made at the default branch's head as
the seed leaves it: each commit names the paths it changes (text, bytes or a delete), and a pull request's `head` is
one of them. Ids come from names (`seed.number`), so the same seed has the same ids in every run; what the agent makes
takes ids from counters kept in the store, from 10,000,000,000, above every id a seed derives.

## Credentials

Minutehand deliberately does not enforce credentials. Any `Authorization`, or none, is accepted, under any scheme: a token
the world holds (a classic `ghp_` or fine-grained `github_pat_` personal access token in the seed) acts as its
user; anything else (an unseeded token, an app's JWT, an installation token from the exchange above, `Basic` with
a password, an empty token, no `Authorization` at all) acts as the seed's `unknown_credentials_act_as`, by default the first user the seed lists. What a
token was issued for is never checked: a classic token's scopes are only echoed in `X-OAuth-Scopes`, and a
fine-grained token selects no repositories. Removed in October 2026, each once a refusal here:

- an unknown token: 401 "Bad credentials";
- `Basic` authentication, or `Bearer` with nothing after it: 401 "Bad credentials";
- a classic token without the `repo` scope: every private repository a 404;
- a fine-grained token's selected repositories: every other repository a 404, and left out of `/user/repos`;
- no `Authorization`: `/user`, `/user/repos`, code search and GraphQL 401, on the address's 60 an hour. Only a world
  that seeds no user still reads a call as nobody's.

What a user may see is world data and stays: a private repository its user neither owns, collaborates on nor
reaches through an organization is a 404, as one that does not exist. In a world that seeds no user, nobody can
be stood in for: a call there reads public repositories only and is refused `/user`, code search and GraphQL
(401), as GitHub refuses an unauthenticated call.

Primary rate limits are counted. Every call spends one from its budget: the user's, shared by every token acting
as them, or the address's when the world has no user to act as; core 5,000 an hour, search 30 and code search 10 a minute, GraphQL
5,000 an hour (one point a query), 60 an hour for core on the address's. `X-RateLimit-*` on every answer says
what is left and when the window ends. The call after the last is refused 403 "API rate limit exceeded" with
`X-RateLimit-Remaining: 0` (a 200 with `RATE_LIMITED` errors on GraphQL) and spends nothing; the budget is whole
again once the run's clock reaches `X-RateLimit-Reset`. The run's clock stands still inside a wake, so a budget
spent there stays spent until the clock passes the reset. An unsupported API version (400) spends
nothing. `GET /rate_limit` reports every budget and spends none. Budgets live in the store
(`budget/<login>/<resource>`, `-` for the address), and the seed's `budgets` can start one part spent.

Faults, armed in the seed and spent in order: `rate_limited` (403 or 429, `X-RateLimit-Remaining: 0` and the reset;
a 200 with `RATE_LIMITED` on GraphQL), `secondary_rate_limited` (403 or 429 with `Retry-After`), `server_error`.

## What it does not do

- Milestones, issue types, reactions, issue events and the timeline, review requests, pending and dismissed reviews,
  repository webhooks and their deliveries, the OAuth web flow, and every app route but the token exchange: refused by name (above).
- A repository's labels are what its seed and the agent's `POST .../labels` define: a label name an issue is given
  that the repository has not defined is refused by name, since the reference does not say what GitHub does with one.
  Labels list alphabetically by name, case aside (recorded). An assignee must be a user with a role on the repository.
- An issue's `updated_at` moves with its own changes and with a comment on it (recorded), not with a lock.
- A GraphQL query costs one point, whatever its size; GitHub prices a query by the nodes it may return.
- Every branch and commit shows the default branch's head files, except a branch with commits of its own (a seed's
  `diverged_branches`), which shows its tree at its own commits; history before a commit made here is a list of
  commits, not a sequence of trees.
- A pull request is from a branch with commits of its own, within the repository, into its default branch. Its diff is
  the difference between the tree the branch was made at and the tree it has, counted by a minimal edit script; its
  `patch` is written only where git has one way to write it (a file added or deleted whole, or a change of lines that
  share nothing with the lines they replace and cannot slide), and otherwise refused by name. Whether it merges is
  worked out from the three trees where no path is changed on both sides, and is `null` where one is: merging the
  lines of one file is not done. Only the merge commit is a merge method here.
- `ETag` and `If-None-Match` are answered (a 304 to a call with an `Authorization` spends nothing); `Last-Modified`
  and `If-Modified-Since` are not, nor `Accept: application/vnd.github.raw`. A text match's fragment is the first
  line holding a term, not GitHub's wider snippet.
- A repository listing's `Link` header points at `/repositories/{id}/…`, as GitHub's does (recorded), and every
  repository route answers there too. Lists page by `per_page` and `page`, in GitHub's order (`prev`, `next`,
  `last`, `first`).
- A seed repository with files declares the commits that made them: every file is named in some commit's `paths`,
  in the seed and in any fragment that grows it on an open world, or the seed is refused naming the repository and
  the file. A commit has its author, and a committer and a commit time when they differ from the author's (the
  default GitHub's create-a-commit reference gives). Its names are the accounts' declared names; an account with
  no name refuses the seed, naming the commit.
- `GET /user` answers GitHub's public view of the user (`user_view_type: "public"`): disk usage, two-factor and
  private gist counts are nothing the world holds.
- Code search answers no `score`: the description requires one and documents nothing of how it is computed, and
  recording it needs a credential, so it is left out rather than made up (`CLAIMS.md`, "Pending a recording").
  Hits are ordered by repository and path, not GitHub's undocumented "best match".
- `HEAD` is accepted as a ref everywhere a ref is. GraphQL documents it; the REST reference does not mention it,
  so it is kept as accepted there (`CLAIMS.md`, "`HEAD` as a ref"). Getting one commit by ref and compare are
  not served.
- The seed comes beside the scenario (`GitHubProvider.seed_with`) until a scenario carries a provider's own seed.
