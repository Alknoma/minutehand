"""The read model's MCP tools, `schema`, `query_run`, `trace` and `explain`, through the SDK's own client over an
in-memory connection, on the hand-written run and its fork."""

from __future__ import annotations

from mcp.shared.memory import create_connected_server_and_client_session

from minutehand.adapters.mcp.results import QueryAnswer, ReadModelSchema
from minutehand.adapters.mcp.server import build
from minutehand.adapters.query.schema import VERSION, VIEWS
from minutehand.adapters.query.trace import Explained, Traced
from tests.mcp.test_mcp_tools import call, refused
from tests.query.fixture import ANSWER, FORK_FOLLOW_UP, QUESTION, Fixture


async def test_schema_lists_every_view_and_column(run: Fixture) -> None:
    async with create_connected_server_and_client_session(build(run.state)) as client:
        found = await call(client, "schema", ReadModelSchema)
    assert found.version == VERSION
    assert found.views == list(VIEWS)


async def test_query_run_pages_through_rows(run: Fixture) -> None:
    sql = "SELECT seq FROM events ORDER BY seq"
    async with create_connected_server_and_client_session(build(run.state)) as client:
        first = await call(client, "query_run", QueryAnswer, run_id=run.run_id, sql=sql, limit=20)
        assert (first.rows[-1], first.more, first.next_offset) == ([20], True, 20)
        rest = await call(client, "query_run", QueryAnswer, run_id=run.run_id, sql=sql, limit=20, offset=20)
    assert (rest.rows, rest.more, rest.next_offset) == ([[21], [22], [23], [24]], False, None)


async def test_query_run_reads_a_fork_by_the_start_of_its_id(run: Fixture) -> None:
    async with create_connected_server_and_client_session(build(run.state)) as client:
        found = await call(
            client, "query_run", QueryAnswer, run_id="fork", sql="SELECT text FROM messages WHERE is_follow_up = 1"
        )
    assert (found.run_id, found.rows) == (run.fork_id, [[FORK_FOLLOW_UP]])


async def test_query_run_that_writes_is_refused(run: Fixture) -> None:
    async with create_connected_server_and_client_session(build(run.state)) as client:
        said = await refused(client, "query_run", run_id=run.run_id, sql="DROP TABLE messages")
        unknown = await refused(client, "query_run", run_id="nosuchrun", sql="SELECT 1")
    assert "read-only" in said
    assert "no run nosuchrun" in unknown


async def test_trace_answers_the_agents_acts(run: Fixture) -> None:
    async with create_connected_server_and_client_session(build(run.state)) as client:
        found = await call(client, "trace", Traced, run_id=run.run_id, person="sofia", kind="message")
    assert [a["seq"] for a in found.actions] == [run.asked, run.follow_up]


async def test_explain_answers_the_chain_around_an_event(run: Fixture) -> None:
    async with create_connected_server_and_client_session(build(run.state)) as client:
        found = await call(client, "explain", Explained, run_id=run.run_id, seq=run.answered)
        beyond = await refused(client, "explain", run_id=run.run_id, seq=999)
    assert found.message is not None and found.message["text"] == ANSWER
    assert found.answers is not None and found.answers["text"] == QUESTION
    assert [r["reply_id"] for r in found.replies_landed] == [1]
    assert "no event 999" in beyond
