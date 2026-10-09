"""Attachments, through the proxy: a multipart upload to an issue, the metadata, the bytes (whole, by range, and by
the redirect), and deletion, as Atlassian's attachment reference describes them."""

from __future__ import annotations

from tests.providers.jira.jira_site import AGENT, API, IRIS_TOKEN, Site, basic, ok, refused

UPLOAD = f"{API}/issue/LAUNCH-1/attachments"
NO_CHECK = {"X-Atlassian-Token": "no-check"}
BYTES = bytes(range(256)) + b"\r\n--not-a-boundary\r\n\x00\xff"


async def _upload(site: Site, *files: tuple[str, bytes, str], issue: str = "LAUNCH-1") -> list[dict[str, object]]:
    sent = [("file", (name, content, kind)) for name, content, kind in files]
    answer = await site.http.post(f"{API}/issue/{issue}/attachments", headers=NO_CHECK, files=sent)
    return ok(answer)


async def test_an_upload_answers_each_file_as_attachment_metadata(site: Site) -> None:
    [made] = await _upload(site, ("plan.bin", BYTES, "application/octet-stream"))
    assert made["filename"] == "plan.bin" and made["size"] == len(BYTES)
    assert made["mimeType"] == "application/octet-stream"
    assert isinstance(made["author"], dict) and made["author"]["accountId"] == AGENT
    assert made["created"] == "2026-08-24T10:50:03.000+0000"
    assert made["self"] == f"{API}/attachment/{made['id']}"
    assert made["content"] == f"{API}/attachment/content/{made['id']}"
    assert "thumbnail" not in made


async def test_several_files_in_one_upload_are_each_an_attachment_in_the_order_sent(site: Site) -> None:
    made = await _upload(site, ("a.txt", b"alpha", "text/plain"), ("b.txt", b"beta!", "text/plain"))
    assert [(m["filename"], m["size"]) for m in made] == [("a.txt", 5), ("b.txt", 5)]
    assert int(str(made[0]["id"])) < int(str(made[1]["id"]))


async def test_the_metadata_reads_back_by_id_and_the_issue_lists_its_attachments(site: Site) -> None:
    [made] = await _upload(site, ("notes.txt", b"hello", "text/plain"))
    assert ok(await site.http.get(f"{API}/attachment/{made['id']}")) == made
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "attachment"}))
    assert issue["fields"]["attachment"] == [made]


async def test_the_bytes_come_back_exactly_with_redirect_false(site: Site) -> None:
    [made] = await _upload(site, ("plan.bin", BYTES, "application/octet-stream"))
    answer = await site.http.get(f"{API}/attachment/content/{made['id']}", params={"redirect": "false"})
    assert answer.status_code == 200 and answer.content == BYTES
    assert answer.headers["content-type"] == "application/octet-stream"


async def test_the_default_is_a_redirect_to_the_download(site: Site) -> None:
    [made] = await _upload(site, ("notes.txt", b"hello", "text/plain"))
    answer = await site.http.get(f"{API}/attachment/content/{made['id']}")
    assert answer.status_code == 303
    assert answer.headers["location"] == f"{API}/attachment/content/{made['id']}?redirect=false"
    followed = await site.http.get(answer.headers["location"])
    assert followed.content == b"hello"


async def test_a_range_header_answers_206_with_the_part_and_where_it_sits(site: Site) -> None:
    [made] = await _upload(site, ("digits.txt", b"0123456789", "text/plain"))
    url = f"{API}/attachment/content/{made['id']}"
    part = await site.http.get(url, params={"redirect": "false"}, headers={"Range": "bytes=2-4"})
    assert (part.status_code, part.content, part.headers["content-range"]) == (206, b"234", "bytes 2-4/10")
    tail = await site.http.get(url, params={"redirect": "false"}, headers={"Range": "bytes=-3"})
    assert (tail.status_code, tail.content) == (206, b"789")
    open_ended = await site.http.get(url, params={"redirect": "false"}, headers={"Range": "bytes=8-"})
    assert (open_ended.status_code, open_ended.content) == (206, b"89")


async def test_an_unsatisfiable_range_is_416_and_a_malformed_one_is_400(site: Site) -> None:
    [made] = await _upload(site, ("digits.txt", b"0123456789", "text/plain"))
    url = f"{API}/attachment/content/{made['id']}"
    beyond = await site.http.get(url, params={"redirect": "false"}, headers={"Range": "bytes=50-60"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == "bytes */10"
    garbled = await site.http.get(url, params={"redirect": "false"}, headers={"Range": "bytes=oops"})
    assert refused(garbled, 400)["errorMessages"] == ["the range supplied in the Range header is malformed."]


async def test_several_ranges_in_one_header_are_refused_501_by_name(site: Site) -> None:
    [made] = await _upload(site, ("digits.txt", b"0123456789", "text/plain"))
    answer = await site.http.get(
        f"{API}/attachment/content/{made['id']}", params={"redirect": "false"}, headers={"Range": "bytes=0-1,3-4"}
    )
    assert "several ranges" in refused(answer, 501)["errorMessages"][0]


async def test_deleting_an_attachment_removes_its_metadata_and_its_bytes(site: Site) -> None:
    [made] = await _upload(site, ("notes.txt", b"hello", "text/plain"))
    ok(await site.http.delete(f"{API}/attachment/{made['id']}"), 204)
    gone = await site.http.get(f"{API}/attachment/{made['id']}")
    assert refused(gone, 404)["errorMessages"] == ["the attachment is not found."]
    assert (await site.http.get(f"{API}/attachment/content/{made['id']}")).status_code == 404
    assert site.jira.blob(str(made["id"])) is None
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "attachment"}))
    assert issue["fields"]["attachment"] == []


async def test_an_upload_without_the_no_check_header_is_refused_501_and_stores_nothing(site: Site) -> None:
    answer = await site.http.post(UPLOAD, files={"file": ("a.txt", b"x", "text/plain")})
    assert "X-Atlassian-Token: no-check" in refused(answer, 501)["errorMessages"][0]
    assert site.jira.attachments("1000") == []


async def test_a_part_not_named_file_is_refused_501_naming_it(site: Site) -> None:
    answer = await site.http.post(UPLOAD, headers=NO_CHECK, files={"upload": ("a.txt", b"x", "text/plain")})
    assert "the multipart parameter 'upload'" in refused(answer, 501)["errorMessages"][0]
    assert site.jira.attachments("1000") == []


async def test_an_upload_that_is_not_multipart_is_refused_501(site: Site) -> None:
    answer = await site.http.post(UPLOAD, headers=NO_CHECK | {"Content-Type": "application/json"}, content=b"{}")
    assert "not multipart/form-data" in refused(answer, 501)["errorMessages"][0]


async def test_more_than_sixty_files_in_one_upload_are_refused_413(site: Site) -> None:
    sent = [("file", (f"{n}.txt", b"x", "text/plain")) for n in range(61)]
    answer = await site.http.post(UPLOAD, headers=NO_CHECK, files=sent)
    assert refused(answer, 413)["errorMessages"] == ["more than 60 files are requested to be uploaded."]
    assert site.jira.attachments("1000") == []
    sixty = await site.http.post(UPLOAD, headers=NO_CHECK, files=sent[:60])
    assert len(ok(sixty)) == 60


async def test_an_upload_to_an_issue_that_is_not_there_is_404(site: Site) -> None:
    answer = await site.http.post(
        f"{API}/issue/NOPE-1/attachments", headers=NO_CHECK, files={"file": ("a", b"x", "text/plain")}
    )
    assert refused(answer, 404)["errorMessages"] == ["Issue does not exist or you do not have permission to see it."]


async def test_an_attachment_of_a_project_the_caller_holds_no_role_in_is_not_found_to_them(site: Site) -> None:
    async with site.client(basic("iris@example.com", IRIS_TOKEN)) as iris:
        sent = await iris.post(
            f"{API}/issue/VAULT-1/attachments", headers=NO_CHECK, files={"file": ("v.txt", b"x", "text/plain")}
        )
        [made] = ok(sent)
        assert (await iris.get(f"{API}/attachment/{made['id']}")).status_code == 200
    assert (await site.http.get(f"{API}/attachment/{made['id']}")).status_code == 404
    assert (await site.http.get(f"{API}/attachment/content/{made['id']}")).status_code == 404
    assert (await site.http.delete(f"{API}/attachment/{made['id']}")).status_code == 404


async def test_deleting_an_issue_takes_its_attachments_with_it(site: Site) -> None:
    [made] = await _upload(site, ("a.txt", b"x", "text/plain"), issue="LAUNCH-2")
    ok(await site.http.delete(f"{API}/issue/LAUNCH-2"), 204)
    assert site.jira.attachment(str(made["id"])) is None and site.jira.blob(str(made["id"])) is None


async def test_a_redirect_flag_that_is_not_a_boolean_is_the_problem_body_jira_gives_for_a_bad_parameter(
    site: Site,
) -> None:
    [made] = await _upload(site, ("a.txt", b"x", "text/plain"))
    answer = await site.http.get(f"{API}/attachment/content/{made['id']}", params={"redirect": "maybe"})
    assert answer.status_code == 400 and answer.headers["content-type"].startswith("application/problem+json")
