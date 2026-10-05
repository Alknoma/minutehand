# Notion: where the fake's behaviour comes from

Each row is a fact about the Notion API that an earlier stand-in for it was built or tested to hold, and the test in
`tests/providers/notion/test_notion_vendor_claims.py` that holds this fake to it. **Documented** facts cite the page
they are read from; **observed** facts are what callers of the real service reported and no public page states.

| Claim | Class | Test | Source |
|---|---|---|---|
| No bearer token is a 401 `unauthorized` | documented | `test_a_call_with_no_bearer_token_is_refused_unauthorized` | https://developers.notion.com/reference/status-codes |
| No `Notion-Version` header is a 400 `missing_version` | documented | `test_a_call_with_no_notion_version_header_is_refused_missing_version` | https://developers.notion.com/reference/status-codes |
| A page created with 101 children is a 400 `validation_error`, and nothing is written | documented | `test_a_page_created_with_a_hundred_and_one_children_is_refused_validation_error` | https://developers.notion.com/reference/post-page, https://developers.notion.com/reference/request-limits |
| A page created with exactly 100 children keeps all of them | documented | `test_a_page_created_with_exactly_a_hundred_children_keeps_them_all` | https://developers.notion.com/reference/post-page |
| A `text.content` over 2000 characters is a 400 `validation_error` | documented | `test_a_text_run_over_two_thousand_characters_is_refused_validation_error` | https://developers.notion.com/reference/request-limits |
| A `text.content` of exactly 2000 characters is kept whole | documented | `test_a_text_run_of_exactly_two_thousand_characters_is_kept_whole` | https://developers.notion.com/reference/request-limits |
| A toggle created with children answers `has_children: true` and lists them as its own children | documented | `test_a_toggle_created_with_children_reports_and_lists_them` | https://developers.notion.com/reference/block |
| An append whose `after` names a grandchild, not a direct child, is a 400 and writes nothing | observed | `test_an_append_after_a_grandchild_is_refused_validation_error` | |
| An append whose `after` names a direct child lands right behind it, not at the end | documented | `test_an_append_after_a_direct_child_lands_right_behind_it` | https://developers.notion.com/reference/patch-block-children |
| An update that carries another block type's body is a 400, and the block keeps its type | observed | `test_an_update_that_names_another_block_type_is_refused` | https://developers.notion.com/reference/update-a-block says only that a wrong type is a 400 |
| A children listing stops at 100 with `has_more` and a `next_cursor` that fetches the rest | documented | `test_a_children_listing_stops_at_a_hundred_and_hands_a_cursor_for_the_rest` | https://developers.notion.com/reference/intro |
| A person user an integration may not read the email of carries `"person": {}` | documented | `test_a_person_with_no_email_is_a_member_whose_person_object_holds_none`, `test_an_account_that_hides_its_email_reads_without_one_under_its_own_id_and_name` | https://developers.notion.com/reference/user |
| An id may be given with or without its dashes, and is served dashed | documented | `test_a_declared_page_id_is_the_id_the_api_serves_the_document_under` | https://developers.notion.com/reference/intro#conventions |

## Not carried over

- The stand-in's `/seed/page` route for making a parent page. It is not part of Notion's API; here parents come from
  the scenario's seed.
- Three tests that drove the product's own Notion code against the stand-in: splitting a long document into
  requests of at most 100 children, splitting a long paragraph into runs of at most 2000 characters, and following
  a cursor to a second page. Their subject is the caller, not Notion; the limits they lean on are the rows above.
