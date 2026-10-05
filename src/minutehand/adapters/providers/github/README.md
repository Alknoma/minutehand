# GitHub provider

Written from GitHub's public REST and GraphQL reference, by reading it. No vendor code, client library or example
payload was copied; every body and error text here is this provider's own.

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
| `GET /repos/{o}/{r}/contents[/{path}]?ref` | a file as wrapped base64; past 1 MiB `"content": ""`, `"encoding": "none"`; a directory capped at 1,000 entries with nothing said |
| `GET /repos/{o}/{r}/git/blobs/{sha}` | the bytes the contents endpoint refused |
| `GET /repos/{o}/{r}/git/trees/{sha}?recursive` | one level, or every object when `recursive` has any value; `truncated` past the entry limit |
| `GET /repos/{o}/{r}/commits?sha&path&per_page&page` | newest first, `Link` paging; 409 on an empty repository |
| `GET /search/code?q&per_page&page` | see `search.py` for the qualifiers answered and refused |
| `POST /graphql` | `repository` with the fields listed in `graphql.py` |

Credentials: classic (`ghp_`, scoped by `repo`) and fine-grained (`github_pat_`, scoped to selected repositories)
personal access tokens, under `Authorization: Bearer` or `token`. An unknown one is 401 `Bad credentials`; a
repository the token may not see is 404, as one that does not exist. No token reads public repositories only.

Faults, armed in the seed and spent in order: `rate_limited` (403 or 429, `X-RateLimit-Remaining: 0` and the reset;
a 200 with `RATE_LIMITED` on GraphQL), `secondary_rate_limited` (403 or 429 with `Retry-After`), `server_error`.

## What it does not do

- Issues, pull requests, comments, labels, webhooks, OAuth web flow, GitHub App installation tokens, `/rate_limit`.
- Budgets are not counted: a successful answer carries no `X-RateLimit-*` headers, and a limit is reached only
  when the seed arms one. A run's clock stands still inside a wake, so a counted window would never reset while a
  client waited one out.
- Every branch and commit shows the head's files; history is a list of commits, not a sequence of trees.
- No `ETag` or conditional requests, no `text_matches`, no `Accept: application/vnd.github.raw`.
- Code search scores every hit 1.0 and orders by repository and path.
- `HEAD` is accepted as a ref by every endpoint; whether GitHub's REST endpoints accept it has not been checked.
- The seed comes beside the scenario (`GitHubProvider.seed_with`) until a scenario carries a provider's own seed.
