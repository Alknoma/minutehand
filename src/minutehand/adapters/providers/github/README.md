# GitHub provider

Written from GitHub's public REST and GraphQL reference, by reading it. No vendor code, client library or example
payload was copied; every body and error text here is this provider's own. `CLAIMS.md` lists the vendor
behaviours it is pinned to, where each comes from, and the test that holds it.

## Scope

It answers what one client needs: a code-reading service that holds a user's personal access token and reads
repositories to answer questions about them. It does not stand in for GitHub at large.

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
and reviews. It holds 169 operations: 12 served, 157 refused by name. The coverage test fails if one is neither.
GraphQL is not in that description; `graphql.py` validates every query against its own schema and refuses what
it does not answer.

The unserved operations a GitHub client most often calls, to serve next in this order:

1. `GET /users/{username}` and `GET /orgs/{org}/members`: reading accounts (the conformance suite's accounts
   property asks for both).
2. `PUT /repos/{owner}/{repo}/contents/{path}`: committing a file, the one write a code-reading world changes by.
3. `GET /repos/{owner}/{repo}/commits/{ref}` and `GET /repos/{owner}/{repo}/compare/{basehead}`: one commit with
   its files, and a diff.
4. `GET /repos/{owner}/{repo}/issues`, `GET|POST /repos/{owner}/{repo}/issues/{issue_number}/comments`,
   `POST /repos/{owner}/{repo}/issues`: the issue tracker.
5. `GET /repos/{owner}/{repo}/pulls`, `GET /repos/{owner}/{repo}/pulls/{pull_number}/files`,
   `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`: pull requests and review.
6. `GET /repos/{owner}/{repo}/readme`, `GET /repos/{owner}/{repo}/branches/{branch}`,
   `GET /repos/{owner}/{repo}/git/ref/{ref}`, `GET /repos/{owner}/{repo}/tags`.
7. `GET /repos/{owner}/{repo}/installation` and `GET /app/installations`: finding an app's installation before
   the token exchange; and webhook delivery (`X-GitHub-Event`, `X-Hub-Signature-256`) if a scenario needs GitHub to
   push.

## Credentials

Minutehand deliberately does not enforce credentials. Any `Authorization` is accepted, under any scheme: a token
the world holds (a classic `ghp_` or fine-grained `github_pat_` personal access token in the seed) acts as its
user; anything else (an unseeded token, an app's JWT, an installation token from the exchange above, `Basic` with
a password) acts as the seed's `unknown_credentials_act_as`, by default the first user the seed lists. What a
token was issued for is never checked: a classic token's scopes are only echoed in `X-OAuth-Scopes`, and a
fine-grained token selects no repositories. Removed in October 2026, each once a refusal here:

- an unknown token: 401 "Bad credentials";
- `Basic` authentication, or `Bearer` with nothing after it: 401 "Bad credentials";
- a classic token without the `repo` scope: every private repository a 404;
- a fine-grained token's selected repositories: every other repository a 404, and left out of `/user/repos`.

What a user may see is world data and stays: a private repository its user neither owns, collaborates on nor
reaches through an organization is a 404, as one that does not exist, and a call with no `Authorization` at all
reads public repositories only and is refused `/user`, code search and GraphQL (401), as GitHub refuses them.

Primary rate limits are counted. Every call spends one from its budget: the user's, shared by every token acting
as them, or the address's with no credential; core 5,000 an hour, search 30 and code search 10 a minute, GraphQL
5,000 an hour (one point a query), 60 an hour for core without a credential. `X-RateLimit-*` on every answer says
what is left and when the window ends. The call after the last is refused 403 "API rate limit exceeded" with
`X-RateLimit-Remaining: 0` (a 200 with `RATE_LIMITED` errors on GraphQL) and spends nothing; the budget is whole
again once the run's clock reaches `X-RateLimit-Reset`. The run's clock stands still inside a wake, so a budget
spent there stays spent until the clock passes the reset. An unsupported API version (400) spends
nothing. `GET /rate_limit` reports every budget and spends none. Budgets live in the store
(`budget/<login>/<resource>`, `-` for the address), and the seed's `budgets` can start one part spent.

Faults, armed in the seed and spent in order: `rate_limited` (403 or 429, `X-RateLimit-Remaining: 0` and the reset;
a 200 with `RATE_LIMITED` on GraphQL), `secondary_rate_limited` (403 or 429 with `Retry-After`), `server_error`.

## What it does not do

- Issues, pull requests, comments, labels, webhooks, the OAuth web flow, and every app route but the token
  exchange: refused by name (above).
- A GraphQL query costs one point, whatever its size; GitHub prices a query by the nodes it may return.
- Every branch and commit shows the head's files; history is a list of commits, not a sequence of trees.
- `ETag` and `If-None-Match` are answered (a 304 to a call with an `Authorization` spends nothing); `Last-Modified`
  and `If-Modified-Since` are not, nor `Accept: application/vnd.github.raw`. A text match's fragment is the first
  line holding a term, not GitHub's wider snippet.
- A `Link` header points at `/repos/{owner}/{repo}/…`; GitHub's (observed) points at `/repositories/{id}/…`, which
  this provider does not serve. Both page by `per_page` and `page`, in GitHub's order (`prev`, `next`, `last`,
  `first`).
- `GET /user` answers GitHub's public view of the user (`user_view_type: "public"`): disk usage, two-factor and
  private gist counts are nothing the world holds.
- Code search scores every hit 1.0 and orders by repository and path.
- `HEAD` is accepted as a ref everywhere a ref is. GraphQL documents it; the REST reference does not mention it,
  so it is kept as accepted there (`CLAIMS.md`, "`HEAD` as a ref"). Getting one commit by ref and compare are
  not served.
- The seed comes beside the scenario (`GitHubProvider.seed_with`) until a scenario carries a provider's own seed.
