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
| `POST /graphql` | `viewer` and `repository` with the fields listed in `graphql.py` |

Credentials: classic (`ghp_`, scoped by `repo`) and fine-grained (`github_pat_`, scoped to selected repositories)
personal access tokens, under `Authorization: Bearer` or `token`. An unknown one is 401 `Bad credentials`; a
repository the token may not see is 404, as one that does not exist. No token reads public repositories only.

Primary rate limits are counted. Every call spends one from its budget: the user's, shared by every token acting
as them, or the address's with no credential; core 5,000 an hour, search 30 and code search 10 a minute, GraphQL
5,000 an hour (one point a query), 60 an hour for core without a credential. `X-RateLimit-*` on every answer says
what is left and when the window ends. The call after the last is refused 403 "API rate limit exceeded" with
`X-RateLimit-Remaining: 0` (a 200 with `RATE_LIMITED` errors on GraphQL) and spends nothing; the budget is whole
again once the run's clock reaches `X-RateLimit-Reset`. The run's clock stands still inside a wake, so a budget
spent there stays spent until the clock passes the reset. A refused credential (401) or API version (400) spends
nothing. `GET /rate_limit` reports every budget and spends none. Budgets live in the store
(`budget/<login>/<resource>`, `-` for the address), and the seed's `budgets` can start one part spent.

Faults, armed in the seed and spent in order: `rate_limited` (403 or 429, `X-RateLimit-Remaining: 0` and the reset;
a 200 with `RATE_LIMITED` on GraphQL), `secondary_rate_limited` (403 or 429 with `Retry-After`), `server_error`.

## What it does not do

- Issues, pull requests, comments, labels, webhooks, OAuth web flow, GitHub App installation tokens.
- A GraphQL query costs one point, whatever its size; GitHub prices a query by the nodes it may return.
- Every branch and commit shows the head's files; history is a list of commits, not a sequence of trees.
- No `ETag` or conditional requests, no `Accept: application/vnd.github.raw`. A text match's fragment is the first
  line holding a term, not GitHub's wider snippet.
- Code search scores every hit 1.0 and orders by repository and path.
- `HEAD` is accepted as a ref by every endpoint; whether GitHub's REST endpoints accept it has not been checked.
- The seed comes beside the scenario (`GitHubProvider.seed_with`) until a scenario carries a provider's own seed.
