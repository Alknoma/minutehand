# Notion: where the fake's behaviour comes from

Each row is a fact about the Notion API that this fake holds, and the test that holds it. **Documented** facts cite
the page they are read from; **observed** facts cite a recording of the real service under `tests/data/notion_api/`
or a public report quoting its answer; **unsourced** rows were kept from an earlier stand-in and no page, recording or
report found on 2026-10-08 states them. `claims` is `tests/providers/notion/test_notion_vendor_claims.py`,
`fidelity` is `test_notion_fidelity.py`, `refusals` is `test_notion_refusals.py`.

The version answered is `2022-06-28`. Notion publishes an OpenAPI document only for its latest version
(https://developers.notion.com/openapi.json, `2026-03-11`); its subset for the resources this fake claims, with the
one 2022-06-28 operation it no longer lists, is under `tests/data/notion_api/`: 33 operations, 20 served and 13
answered 501 `invalid_request` naming them (`app.UNSERVED`, `test_notion_surface.py`).

## Credentials

Minutehand deliberately does not enforce credentials or scopes. Notion answers a call with no token, or a token that
is not one, 401 `unauthorized` (`tests/data/notion_api/real-service-without-a-token-2026-10-08.txt`); this fake answers
it, as the agent's integration (the first the seed declares, or the `Agent` integration the seed adds when it
declares none). A token the seed holds or `/v1/oauth/token` minted acts as its integration. Removed and never refused:
the 401 for a missing, unknown, used or non-access token; the 403 `restricted_resource` for a capability the
integration lacks; and at the token endpoint the `invalid_client` for an unknown client id or wrong secret and the
`invalid_grant` for an unknown, reused or another client's code or refresh token and another redirect URI. What the
world holds still decides what a caller sees: a page not shared with the integration is `object_not_found`, and a
person's email is answered only to an integration with the `read_users_with_email` capability
(https://developers.notion.com/reference/capabilities).

| Claim | Class | Test | Source |
|---|---|---|---|
| A call with no bearer token, or one that names no integration, is answered as the agent's integration | Minutehand's own: credentials are not enforced | `claims::test_a_call_with_no_bearer_token_is_answered_as_the_agents_integration`, `refusals::test_a_call_with_no_token_or_an_unknown_one_is_made_as_the_agents_integration` | `tests/data/notion_api/real-service-without-a-token-2026-10-08.txt` (what Notion answers instead) |
| No capability refuses a call | Minutehand's own: scopes are not enforced | `refusals::test_an_integration_missing_a_capability_is_not_refused` | https://developers.notion.com/reference/capabilities (what Notion answers instead) |
| No client secret, code, redirect URI or refresh token is refused at `/v1/oauth/token` | Minutehand's own: credentials are not enforced | `refusals::test_no_code_client_secret_redirect_or_refresh_token_is_refused` | `tests/data/notion_api/real-service-without-a-token-2026-10-08.txt` (`invalid_client`, what Notion answers instead) |
| An error is `{"object": "error", "status", "code", "message", "request_id"}` | observed | `notion_world.refusal` (every refusal test) | `tests/data/notion_api/real-service-without-a-token-2026-10-08.txt`, https://developers.notion.com/reference/status-codes |
| A path or method with no endpoint is 400 `invalid_request_url` "Invalid request URL." | observed | `refusals::test_an_unknown_path_and_a_wrong_method_are_refused_invalid_request_url` | `tests/data/notion_api/real-service-without-a-token-2026-10-08.txt` |
| A body that is not JSON is 400 `invalid_json` "Error parsing JSON body." | observed | `refusals::test_a_body_that_is_not_json_is_refused_invalid_json` | `tests/data/notion_api/real-service-without-a-token-2026-10-08.txt`, https://developers.notion.com/reference/status-codes |
| No `Notion-Version` header is a 400 `missing_version` with Notion's documented message | documented | `claims::test_a_call_with_no_notion_version_header_is_refused_missing_version`, `fidelity::test_a_missing_version_carries_notions_documented_message` | https://developers.notion.com/reference/status-codes |
| A version other than 2022-06-28 is 501 `invalid_request` naming it | Minutehand's own: refused by name, never answered in another version's shapes | `refusals::test_a_version_this_fake_does_not_serve_is_refused_501_naming_it` | https://developers.notion.com/reference/changes-by-version |
| Something Notion takes and this fake does not build (a block type, a property type, a mention, an image not `external`, a date mention with a time or an end) is 501 `invalid_request` "Unsupported request: ..." | Minutehand's own, in the shape of Notion's `invalid_request` | `refusals::test_a_block_type_this_fake_does_not_build_is_refused_501_naming_it`, `fidelity::test_a_mention_notion_takes_and_this_fake_does_not_build_is_refused_501_naming_it`, `fidelity::test_an_image_that_is_not_external_is_refused_501_naming_it`, `fidelity::test_a_property_type_notion_takes_and_this_fake_does_not_build_is_refused_501_naming_it`, `fidelity::test_a_date_mention_with_an_end_or_a_time_is_refused_501_naming_it` | https://developers.notion.com/reference/status-codes, `blockObjectRequest`, `propertyConfigurationRequest`, `mentionRichTextItemRequest` in the OpenAPI subset |
| Rich text and block content come back as sent, with only `plain_text`, `href` and default annotations filled in | documented | `fidelity::test_rich_text_and_block_content_come_back_as_they_were_sent` | https://developers.notion.com/reference/rich-text |
| A code block takes every language Notion lists | documented | `fidelity::test_rich_text_and_block_content_come_back_as_they_were_sent` | `languageRequest` in the OpenAPI subset |
| A page's `url` is `https://app.notion.com/p/<Title>-<id>`; a database's and a mention's `href` `https://app.notion.com/p/<id>` | documented | `fidelity::test_a_page_a_database_and_a_mention_link_to_app_notion_com` | https://developers.notion.com/reference/versioning, https://developers.notion.com/reference/page, https://developers.notion.com/reference/database, https://developers.notion.com/reference/rich-text |
| A date mention reads as its date | documented | `fidelity::test_a_date_mention_reads_as_its_date` | https://developers.notion.com/reference/rich-text |
| Something out of the integration's reach is `object_not_found` "Could not find page with ID: ... shared with your connection \"<name>\"." | documented | `fidelity::test_an_object_out_of_reach_is_refused_with_notions_documented_message` | https://developers.notion.com/reference/status-codes |
| `rate_limited` and `conflict_error` carry Notion's documented messages | documented | `fidelity::test_a_rate_limit_and_a_conflict_carry_notions_documented_messages` | https://developers.notion.com/reference/status-codes |
| `created_time` and `last_edited_time` are rounded down to the minute | documented | `test_notion_sdk.py` (every timestamp read) | https://developers.notion.com/guides/resources/historical-changelog (June 28, 2021) |
| A list is `{"object": "list", "results", "next_cursor", "has_more", "type", "<type>": {}, "request_id"}` | documented | `claims::test_a_children_listing_stops_at_a_hundred_and_hands_a_cursor_for_the_rest` | https://developers.notion.com/reference/intro, https://developers.notion.com/reference/pagination |
| A page created with 101 children is a 400 `validation_error`, and nothing is written | documented | `claims::test_a_page_created_with_a_hundred_and_one_children_is_refused_validation_error` | https://developers.notion.com/reference/post-page, https://developers.notion.com/reference/request-limits |
| A page created with exactly 100 children keeps all of them | documented | `claims::test_a_page_created_with_exactly_a_hundred_children_keeps_them_all` | https://developers.notion.com/reference/post-page |
| A `text.content` over 2000 characters is a 400 `validation_error` | documented | `claims::test_a_text_run_over_two_thousand_characters_is_refused_validation_error` | https://developers.notion.com/reference/request-limits |
| A `text.content` of exactly 2000 characters is kept whole | documented | `claims::test_a_text_run_of_exactly_two_thousand_characters_is_kept_whole` | https://developers.notion.com/reference/request-limits |
| A toggle created with children answers `has_children: true` and lists them as its own children | documented | `claims::test_a_toggle_created_with_children_reports_and_lists_them` | https://developers.notion.com/reference/block |
| An append whose `after` names a direct child lands right behind it, not at the end | documented | `claims::test_an_append_after_a_direct_child_lands_right_behind_it` | https://developers.notion.com/reference/patch-block-children |
| An update that carries another block type's body is a 400, and the block keeps its type | documented (the 400) | `claims::test_an_update_that_names_another_block_type_is_refused` | https://developers.notion.com/reference/update-a-block |
| A children listing stops at 100 with `has_more` and a `next_cursor` that fetches the rest | documented | `claims::test_a_children_listing_stops_at_a_hundred_and_hands_a_cursor_for_the_rest` | https://developers.notion.com/reference/intro |
| An append whose `after` names a grandchild, not a direct child, is a 400 and writes nothing | unsourced | `claims::test_an_append_after_a_grandchild_is_refused_validation_error` | |
| The words of every other `validation_error` (an unknown body key, a page size out of range, a cursor not in the list, ...) | unsourced: Notion's form is "body failed validation: body.<path> should be ..., instead was ..." (https://developers.notion.com/reference/status-codes); these messages are this fake's own | `test_notion_refusals.py` | |

## Not carried over

- **Credential and scope enforcement** (see Credentials).
- **`https://www.notion.so/...` links**: Notion's own links moved to `https://app.notion.com/p/...` on every version.
- **A date mention's `plain_text` of `start → end`**: undocumented; refused by name.
- **400 `validation_error` for what Notion takes and this fake does not build**, and `invalid_request` for a method a
  path does not take: now 501 by name, and Notion's recorded `invalid_request_url`.
- The stand-in's `/seed/page` route for making a parent page. It is not part of Notion's API; here parents come from
  the scenario's seed.
- Three tests that drove the product's own Notion code against the stand-in. Their subject is the caller, not
  Notion; the limits they lean on are the rows above.
