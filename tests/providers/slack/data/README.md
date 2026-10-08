# Slack vendor data

## `slack_web_openapi_v2_without_examples.json`

Slack's OpenAPI 2.0 description of the Web API, byte for byte as Slack published it.

- Source: https://github.com/slackapi/slack-api-specs/blob/bc08db49625630e3585bf2f1322128ea04f2a7f3/web-api/slack_web_openapi_v2_without_examples.json
- Retrieved: 2026-10-08, from
  `https://raw.githubusercontent.com/slackapi/slack-api-specs/bc08db49625630e3585bf2f1322128ea04f2a7f3/web-api/slack_web_openapi_v2_without_examples.json`
- SHA-256: `8b92da26a3c5b11d20042a9f36d81f1fa6fc9382c5ddc471babb68b91936bc3a`
- Slack archived the repository in September 2021 (its last commit, bc08db4, is 2021-09-07), so the file lists the
  174 methods of that date, retired families (`channels.*`, `groups.*`, `im.*`, `mpim.*`) among them. Methods added
  since (for example `apps.connections.open`) are in the methods index below and in the pinned `slack_sdk`'s
  `WebClient`; `test_slack_method_coverage.py` reads all three.

## `slack_methods_index.md`

Slack's index of every Web API method (340 at retrieval), as the Markdown copy of the page Slack serves.

- Source: https://docs.slack.dev/reference/methods (its Markdown twin, `https://docs.slack.dev/reference/methods.md`,
  which `https://docs.slack.dev/llms.txt` advertises)
- Retrieved: 2026-10-08
- SHA-256: `c8eb8bffac20cfe749325310ebbdd0ba99fa9cd90093177f470a261e380fa38a`

## `observed/`

Real answers from `https://slack.com/api/…`, recorded on 2026-10-08 with `curl -sS -i` and no Slack credential of
anyone's (each call is one an unauthenticated stranger can make):

| File | Request |
|---|---|
| `unknown_method.http` | `GET https://slack.com/api/foo.bar` |
| `not_authed.http` | `POST https://slack.com/api/chat.postMessage` with `channel=C1&text=hi` and no token |
| `invalid_auth.http` | `GET https://slack.com/api/auth.test` with `Authorization: Bearer xoxb-1-2-3` |

`not_authed` and `invalid_auth` are what Slack answers a missing or unknown credential; Minutehand deliberately
answers neither (the provider's `CLAIMS.md`), and they are kept as the record of what it departs from.
