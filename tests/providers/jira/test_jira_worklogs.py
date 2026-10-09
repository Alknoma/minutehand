"""Worklogs (`/issue/{key}/worklog`), through the proxy: logged, read, changed and deleted as the reference describes,
and the issue's time spent and remaining estimate moving as `adjustEstimate` says."""

from __future__ import annotations

from typing import Any

from tests.providers.jira.jira_site import AGENT, API, IRIS, IRIS_TOKEN, Site, basic, ok, refused

STARTED = "2026-08-24T09:00:00.000+0000"
LOG = f"{API}/issue/LAUNCH-1/worklog"


def _doc(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def _tracking(site: Site) -> dict[str, Any]:
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "timetracking"}))
    return issue["fields"]["timetracking"]


async def _log(site: Site, **body: Any) -> dict[str, Any]:
    params = body.pop("params", {})
    return ok(await site.http.post(LOG, json={"started": STARTED, **body}, params=params), 201)


async def test_a_worklog_is_kept_as_sent_and_read_back_by_id_and_in_the_issues_list(site: Site) -> None:
    comment = _doc("Pairing on the notes")
    made = await _log(site, timeSpent="1h 30m", comment=comment)
    assert made["timeSpentSeconds"] == 5400 and made["timeSpent"] == "1h 30m"
    assert made["started"] == STARTED and made["comment"] == comment
    assert made["author"]["accountId"] == AGENT and made["updateAuthor"]["accountId"] == AGENT
    assert made["issueId"] == "1000" and made["self"] == f"{API}/issue/1000/worklog/{made['id']}"
    assert made["created"] == made["updated"] == "2026-08-24T10:50:03.000+0000"

    assert ok(await site.http.get(f"{LOG}/{made['id']}")) == made
    listed = ok(await site.http.get(LOG, params={"maxResults": "50"}))
    assert listed["worklogs"] == [made] and (listed["startAt"], listed["maxResults"], listed["total"]) == (0, 50, 1)


async def test_time_spent_in_seconds_is_taken_as_it_is(site: Site) -> None:
    made = await _log(site, timeSpentSeconds=7200)
    assert made["timeSpentSeconds"] == 7200 and made["timeSpent"] == "2h" and "comment" not in made


async def test_a_worklog_adds_to_the_issues_time_spent_and_auto_reduces_the_remaining_estimate(site: Site) -> None:
    assert await _tracking(site) == {
        "originalEstimate": "1d",
        "originalEstimateSeconds": 28800,
        "remainingEstimate": "5h",
        "remainingEstimateSeconds": 18000,
        "timeSpent": "3h",
        "timeSpentSeconds": 10800,
    }
    await _log(site, timeSpent="1h")
    after = await _tracking(site)
    assert (after["timeSpentSeconds"], after["remainingEstimateSeconds"]) == (14400, 14400)
    assert after["originalEstimateSeconds"] == 28800


async def test_adjust_estimate_leave_keeps_the_remaining_estimate(site: Site) -> None:
    await _log(site, timeSpent="1h", params={"adjustEstimate": "leave"})
    after = await _tracking(site)
    assert (after["timeSpentSeconds"], after["remainingEstimateSeconds"]) == (14400, 18000)
    await _log(site, timeSpent="1h")
    again = await _tracking(site)
    assert again["remainingEstimateSeconds"] == 14400, "the kept estimate is what auto reduces next"


async def test_adjust_estimate_new_sets_the_remaining_estimate(site: Site) -> None:
    await _log(site, timeSpent="1h", params={"adjustEstimate": "new", "newEstimate": "2d"})
    assert (await _tracking(site))["remainingEstimateSeconds"] == 57600


async def test_adjust_estimate_manual_reduces_the_remaining_estimate_by_the_amount(site: Site) -> None:
    await _log(site, timeSpent="1h", params={"adjustEstimate": "manual", "reduceBy": "30m"})
    assert (await _tracking(site))["remainingEstimateSeconds"] == 16200


async def test_the_remaining_estimate_never_goes_below_nothing(site: Site) -> None:
    await _log(site, timeSpent="2d")
    assert (await _tracking(site))["remainingEstimateSeconds"] == 0


async def test_adjust_estimate_new_without_a_new_estimate_is_refused_400(site: Site) -> None:
    body = refused(
        await site.http.post(LOG, json={"started": STARTED, "timeSpent": "1h"}, params={"adjustEstimate": "new"}), 400
    )
    assert body["errorMessages"] == [
        "`adjustEstimate` is set to `new` but `newEstimate` is not provided or is invalid."
    ]
    assert (await _tracking(site))["timeSpentSeconds"] == 10800, "nothing was logged"


async def test_adjust_estimate_manual_without_reduce_by_is_refused_400(site: Site) -> None:
    answer = await site.http.post(
        LOG, json={"started": STARTED, "timeSpent": "1h"}, params={"adjustEstimate": "manual"}
    )
    assert "`reduceBy` is not provided or is invalid" in refused(answer, 400)["errorMessages"][0]


async def test_an_issue_without_estimates_has_none_to_adjust_and_auto_leaves_it_so(site: Site) -> None:
    made = ok(await site.http.post(f"{API}/issue/LAUNCH-2/worklog", json={"started": STARTED, "timeSpent": "1h"}), 201)
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-2", params={"fields": "timetracking"}))
    assert made["issueId"] == "1001"
    assert issue["fields"]["timetracking"] == {"timeSpent": "1h", "timeSpentSeconds": 3600}


async def test_a_worklog_without_started_or_without_a_time_or_with_both_times_is_refused_400(site: Site) -> None:
    for body in (
        {"timeSpent": "1h"},
        {"started": STARTED},
        {"started": STARTED, "timeSpent": "1h", "timeSpentSeconds": 3600},
    ):
        answer = await site.http.post(LOG, json=body)
        assert refused_bare(answer, 400)
    assert ok(await site.http.get(LOG, params={"maxResults": "50"}))["worklogs"] == []


def refused_bare(answer: Any, status: int) -> bool:
    """Jira's invalid-payload body: `errorMessages` alone, as recorded for a body of the wrong shape."""
    assert answer.status_code == status, answer.text
    assert answer.json() == {
        "errorMessages": ["Invalid request payload. Refer to the REST API documentation and try again."]
    }
    return True


async def test_a_time_in_weeks_is_refused_501_because_the_reference_names_only_days_hours_and_minutes(
    site: Site,
) -> None:
    body = refused(await site.http.post(LOG, json={"started": STARTED, "timeSpent": "1w"}), 501)
    assert "timeSpent '1w'" in body["errorMessages"][0]


async def test_a_worklog_comment_that_is_not_a_document_is_refused_400(site: Site) -> None:
    body = refused(await site.http.post(LOG, json={"started": STARTED, "timeSpent": "1h", "comment": "plain"}), 400)
    assert body["errors"] == {"comment": "Comment body is not valid!"}


async def test_visibility_and_properties_and_expand_are_refused_501_naming_them(site: Site) -> None:
    sent = {"started": STARTED, "timeSpent": "1h"}
    visible = await site.http.post(LOG, json=sent | {"visibility": {"type": "group", "value": "staff"}})
    assert "'visibility' property" in refused(visible, 501)["errorMessages"][0]
    props = await site.http.post(LOG, json=sent | {"properties": [{"key": "k", "value": 1}]})
    assert "'properties' property" in refused(props, 501)["errorMessages"][0]
    expanded = await site.http.post(LOG, json=sent, params={"expand": "properties"})
    assert "'expand' parameter of POST" in refused(expanded, 501)["errorMessages"][0]
    assert (await _tracking(site))["timeSpentSeconds"] == 10800, "none of them logged anything"


async def test_the_override_editable_flag_is_refused_501_when_it_is_true(site: Site) -> None:
    answer = await site.http.post(
        LOG, json={"started": STARTED, "timeSpent": "1h"}, params={"overrideEditableFlag": "true"}
    )
    assert "overrideEditableFlag=true" in refused(answer, 501)["errorMessages"][0]


async def test_listing_worklogs_without_max_results_is_refused_501_for_want_of_a_documented_default(site: Site) -> None:
    body = refused(await site.http.get(LOG), 501)
    assert "without maxResults" in body["errorMessages"][0]


async def test_worklogs_list_oldest_created_first_and_filter_on_when_they_started(site: Site) -> None:
    early = await _log(site, timeSpent="1h", started="2026-08-20T09:00:00.000+0000")
    late = await _log(site, timeSpent="2h", started="2026-08-26T09:00:00.000+0000")
    page = ok(await site.http.get(LOG, params={"maxResults": "1", "startAt": "1"}))
    assert [w["id"] for w in page["worklogs"]] == [late["id"]] and page["total"] == 1, (
        "total is the results on the page"
    )
    cut = "1787562000000"  # 2026-08-24T09:00:00Z in milliseconds
    after = ok(await site.http.get(LOG, params={"maxResults": "50", "startedAfter": cut}))
    before = ok(await site.http.get(LOG, params={"maxResults": "50", "startedBefore": cut}))
    assert [w["id"] for w in after["worklogs"]] == [late["id"]]
    assert [w["id"] for w in before["worklogs"]] == [early["id"]]


async def test_a_worklog_logged_by_another_account_names_it_as_author(site: Site) -> None:
    async with site.client(basic("iris@example.com", IRIS_TOKEN)) as iris:
        made = ok(await iris.post(LOG, json={"started": STARTED, "timeSpent": "15m"}), 201)
    assert made["author"]["accountId"] == IRIS


async def test_a_worklog_update_changes_what_is_sent_and_auto_moves_the_estimate_by_the_difference(site: Site) -> None:
    made = await _log(site, timeSpent="1h", comment=_doc("first"))
    changed = ok(await site.http.put(f"{LOG}/{made['id']}", json={"timeSpent": "3h", "comment": _doc("second")}))
    assert changed["timeSpentSeconds"] == 10800 and changed["comment"] == _doc("second")
    assert changed["started"] == STARTED and changed["created"] == made["created"]
    after = await _tracking(site)
    assert (after["timeSpentSeconds"], after["remainingEstimateSeconds"]) == (21600, 7200)
    assert ok(await site.http.get(f"{LOG}/{made['id']}")) == changed


async def test_a_worklog_update_with_less_time_gives_estimate_back_under_auto(site: Site) -> None:
    made = await _log(site, timeSpent="3h")
    ok(await site.http.put(f"{LOG}/{made['id']}", json={"timeSpent": "1h"}))
    after = await _tracking(site)
    assert (after["timeSpentSeconds"], after["remainingEstimateSeconds"]) == (14400, 14400)


async def test_a_worklog_update_with_leave_or_new_sets_the_estimate_as_asked(site: Site) -> None:
    made = await _log(site, timeSpent="1h")
    ok(await site.http.put(f"{LOG}/{made['id']}", json={"timeSpentSeconds": 7200}, params={"adjustEstimate": "leave"}))
    assert (await _tracking(site))["remainingEstimateSeconds"] == 14400
    ok(await site.http.put(f"{LOG}/{made['id']}", json={}, params={"adjustEstimate": "new", "newEstimate": "1h"}))
    assert (await _tracking(site))["remainingEstimateSeconds"] == 3600


async def test_a_worklog_update_with_manual_is_refused_501_because_its_reference_does_not_document_it(
    site: Site,
) -> None:
    made = await _log(site, timeSpent="1h")
    answer = await site.http.put(f"{LOG}/{made['id']}", json={"timeSpent": "2h"}, params={"adjustEstimate": "manual"})
    assert "adjustEstimate=manual on this operation" in refused(answer, 501)["errorMessages"][0]


async def test_a_worklog_of_another_issue_or_none_is_404(site: Site) -> None:
    made = await _log(site, timeSpent="1h")
    elsewhere = await site.http.get(f"{API}/issue/LAUNCH-2/worklog/{made['id']}")
    assert refused(elsewhere, 404)["errorMessages"] == [
        "the worklog is not found or the user does not have permission to view it."
    ]
    assert (await site.http.get(f"{LOG}/99999")).status_code == 404
    assert (await site.http.put(f"{LOG}/99999", json={"timeSpent": "1h"})).status_code == 404
    assert (await site.http.delete(f"{LOG}/99999", params={"adjustEstimate": "leave"})).status_code == 404


async def test_a_worklog_on_an_issue_that_is_not_there_is_404(site: Site) -> None:
    answer = await site.http.post(f"{API}/issue/NOPE-1/worklog", json={"started": STARTED, "timeSpent": "1h"})
    assert refused(answer, 404)["errorMessages"] == ["Issue does not exist or you do not have permission to see it."]


async def test_deleting_a_worklog_takes_its_time_off_and_leave_keeps_the_estimate(site: Site) -> None:
    made = await _log(site, timeSpent="1h")
    ok(await site.http.delete(f"{LOG}/{made['id']}", params={"adjustEstimate": "leave"}), 204)
    after = await _tracking(site)
    assert (after["timeSpentSeconds"], after["remainingEstimateSeconds"]) == (10800, 14400)
    assert (await site.http.get(f"{LOG}/{made['id']}")).status_code == 404


async def test_deleting_with_new_and_manual_sets_or_raises_the_remaining_estimate(site: Site) -> None:
    first, second = await _log(site, timeSpent="1h"), await _log(site, timeSpent="1h")
    ok(await site.http.delete(f"{LOG}/{first['id']}", params={"adjustEstimate": "new", "newEstimate": "6h"}), 204)
    assert (await _tracking(site))["remainingEstimateSeconds"] == 21600
    ok(await site.http.delete(f"{LOG}/{second['id']}", params={"adjustEstimate": "manual", "increaseBy": "1h"}), 204)
    assert (await _tracking(site))["remainingEstimateSeconds"] == 25200


async def test_deleting_under_auto_from_an_issue_with_an_estimate_is_refused_501_because_the_reference_contradicts_itself(
    site: Site,
) -> None:
    made = await _log(site, timeSpent="1h")
    body = refused(await site.http.delete(f"{LOG}/{made['id']}"), 501)
    assert "adjustEstimate=auto when deleting" in body["errorMessages"][0]
    assert (await site.http.get(f"{LOG}/{made['id']}")).status_code == 200, "the worklog is still there"


async def test_deleting_under_auto_from_an_issue_with_no_estimate_has_none_to_move(site: Site) -> None:
    made = ok(await site.http.post(f"{API}/issue/LAUNCH-2/worklog", json={"started": STARTED, "timeSpent": "1h"}), 201)
    ok(await site.http.delete(f"{API}/issue/LAUNCH-2/worklog/{made['id']}"), 204)
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-2", params={"fields": "timetracking"}))
    assert issue["fields"]["timetracking"] == {}


async def test_bulk_deleting_worklogs_removes_them_all_or_names_the_stranger_404(site: Site) -> None:
    one, two = await _log(site, timeSpent="1h"), await _log(site, timeSpent="30m")
    other = ok(await site.http.post(f"{API}/issue/LAUNCH-2/worklog", json={"started": STARTED, "timeSpent": "1h"}), 201)
    stranger = await site.http.request(
        "DELETE", LOG, json={"ids": [int(one["id"]), int(other["id"])]}, params={"adjustEstimate": "leave"}
    )
    assert "not associated with the provided issue" in refused(stranger, 404)["errorMessages"][0]
    assert len(ok(await site.http.get(LOG, params={"maxResults": "50"}))["worklogs"]) == 2, "none was deleted"
    ok(
        await site.http.request(
            "DELETE", LOG, json={"ids": [int(one["id"]), int(two["id"])]}, params={"adjustEstimate": "leave"}
        ),
        204,
    )
    assert ok(await site.http.get(LOG, params={"maxResults": "50"}))["worklogs"] == []
    assert (await _tracking(site))["timeSpentSeconds"] == 10800


async def test_bulk_deleting_with_an_adjustment_the_reference_does_not_list_is_refused_501(site: Site) -> None:
    answer = await site.http.request("DELETE", LOG, json={"ids": [1]}, params={"adjustEstimate": "manual"})
    assert "adjustEstimate=manual on this operation" in refused(answer, 501)["errorMessages"][0]


async def test_deleting_an_issue_takes_its_worklogs_with_it(site: Site) -> None:
    made = ok(await site.http.post(f"{API}/issue/LAUNCH-2/worklog", json={"started": STARTED, "timeSpent": "1h"}), 201)
    ok(await site.http.delete(f"{API}/issue/LAUNCH-2"), 204)
    assert site.jira.worklog(made["id"]) is None


async def test_started_after_includes_a_worklog_started_at_that_moment_and_started_before_leaves_it_out(
    site: Site,
) -> None:
    on = await _log(site, timeSpent="1h", started=STARTED)
    cut = "1787562000000"  # STARTED in milliseconds since the epoch
    after = ok(await site.http.get(LOG, params={"maxResults": "50", "startedAfter": cut}))
    before = ok(await site.http.get(LOG, params={"maxResults": "50", "startedBefore": cut}))
    assert [w["id"] for w in after["worklogs"]] == [on["id"]]
    assert before["worklogs"] == []


async def test_bulk_deleting_more_than_5000_worklogs_is_refused_400_and_deletes_none(site: Site) -> None:
    made = await _log(site, timeSpent="1h")
    ids = [int(made["id"]), *range(1, 5001)]
    answer = await site.http.request("DELETE", LOG, json={"ids": ids}, params={"adjustEstimate": "leave"})
    assert refused(answer, 400)["errorMessages"] == ["the number of worklogs being deleted exceeds the limit"]
    assert (await site.http.get(f"{LOG}/{made['id']}")).status_code == 200
