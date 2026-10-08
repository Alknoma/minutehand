"""Calendar v3 is scoped from Google's own description of it: the discovery document, retrieved on the date its
file name carries from https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest. Every method in it is served
or refused by name, and a served method serves or refuses by name exactly the parameters the document gives it.

The behaviour is driven with Google's own client built from that document (`build_from_document`), in a process of
its own, through the proxy: each refused method and each refused parameter answers 501 naming it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minutehand.adapters.providers.google_workspace.calendars import REFUSED, SERVED, STANDARD_PARAMETERS
from tests.providers.google_workspace.proxied import serving
from tests.providers.google_workspace.test_calendar_through_proxy import SCENARIO

pytestmark = pytest.mark.timeout(120)

DISCOVERY = Path(__file__).resolve().parents[2] / "data" / "google_calendar_v3" / "discovery-retrieved-2026-10-08.json"


class Documented:
    def __init__(self, raw: dict[str, object]) -> None:
        self.raw = raw

    def methods(self) -> dict[str, dict[str, object]]:
        found: dict[str, dict[str, object]] = {}

        def walk(node: object) -> None:
            assert isinstance(node, dict)
            resources = node["resources"] if "resources" in node else {}
            assert isinstance(resources, dict)
            for resource in resources.values():
                assert isinstance(resource, dict)
                methods = resource["methods"] if "methods" in resource else {}
                assert isinstance(methods, dict)
                for method in methods.values():
                    assert isinstance(method, dict)
                    name = method["id"]
                    assert isinstance(name, str)
                    found[name.removeprefix("calendar.")] = method
                walk(resource)

        walk(self.raw)
        return found


def documented() -> Documented:
    raw = json.loads(DISCOVERY.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return Documented(raw)


def _parameters(method: dict[str, object]) -> set[str]:
    found = method["parameters"] if "parameters" in method else {}
    assert isinstance(found, dict)
    return set(found)


def test_every_documented_method_is_served_or_refused_by_name_at_its_documented_path() -> None:
    methods = documented().methods()
    served = {m.name: m for m in SERVED}
    refused = {m.name: m for m in REFUSED}
    assert not set(served) & set(refused), "a method both served and refused"
    assert set(served) | set(refused) == set(methods), (
        f"undocumented: {sorted(set(served) | set(refused) - set(methods))}; "
        f"neither served nor refused: {sorted(set(methods) - set(served) - set(refused))}"
    )
    for name, method in methods.items():
        ours = served[name] if name in served else refused[name]
        assert (ours.http, ours.path) == (method["httpMethod"], method["path"]), name


def test_a_served_method_serves_or_refuses_exactly_its_documented_parameters() -> None:
    methods = documented().methods()
    for ours in SERVED:
        assert not ours.serves & ours.refuses, ours.name
        assert ours.serves | ours.refuses == _parameters(methods[ours.name]), ours.name
    standard = documented().raw["parameters"]
    assert isinstance(standard, dict) and set(standard) == STANDARD_PARAMETERS


def _value(spec: dict[str, object]) -> object:
    if "enum" in spec:
        values = spec["enum"]
        assert isinstance(values, list)
        return values[0]
    kind = spec["type"]
    return {"boolean": True, "integer": 1}[str(kind)] if kind in ("boolean", "integer") else "x"


def _required(method: dict[str, object]) -> dict[str, object]:
    """The parameters the method requires, and an empty body where it takes one."""
    found = method["parameters"] if "parameters" in method else {}
    assert isinstance(found, dict)
    asked = {name: _value(spec) for name, spec in found.items() if spec.get("required")}
    return {**asked, "body": {}} if "request" in method else asked


async def test_each_refused_method_and_parameter_answers_501_naming_it_to_googles_own_client(tmp_path: Path) -> None:
    methods = documented().methods()
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    for ours in REFUSED:
        calls.append((ours.name, ours.name, f"calendar.{ours.name}", _required(methods[ours.name])))
    for ours in SERVED:
        specs = methods[ours.name]["parameters"] if "parameters" in methods[ours.name] else {}
        assert isinstance(specs, dict)
        for parameter in sorted(ours.refuses):
            asked = {**_required(methods[ours.name]), parameter: _value(specs[parameter])}
            calls.append((f"{ours.name}?{parameter}", ours.name, parameter, asked))
    program = f"""
from googleapiclient.discovery import build_from_document
calendar = build_from_document(open({str(DISCOVERY)!r}).read(), credentials=creds)
found = {{}}
for key, name, asked in {[(k, n, a) for k, n, _, a in calls]!r}:
    resource, method = name.rsplit(".", 1)
    target = calendar
    for part in resource.split("."):
        target = getattr(target, part)()
    request = getattr(target, method.replace("import", "import_"))(**asked)
    try:
        request.execute()
        found[key] = None
    except HttpError as error:
        found[key] = [error.resp.status, error._get_reason()]
say(found=found)
"""
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(program)
        found = (await client.heard())["found"]
        await client.finished()
    assert isinstance(found, dict)
    wrong = {
        key: found[key]
        for key, _, named, _ in calls
        if not (isinstance(found[key], list) and found[key][0] == 501 and named in found[key][1])
    }
    assert not wrong, (sorted(wrong), sorted(set(found) - set(wrong)))


async def test_a_parameter_the_document_does_not_name_is_refused_by_name_and_answers_are_indented(
    tmp_path: Path,
) -> None:
    """Google's client refuses an unknown keyword itself, so the call is sent on its own authorised connection."""
    program = """
calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
base = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
unknown = calendar._http.request(base + "?colour=blue")
pretty = calendar._http.request(base)
compact = calendar._http.request(base + "?prettyPrint=false")
say(unknown=[unknown[0].status, unknown[1].decode()], pretty=pretty[1].decode(), compact=compact[1].decode())
"""
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(program)
        seen = await client.heard()
        await client.finished()
    status, body = seen["unknown"]  # type: ignore[misc]
    assert status == 501 and "the parameter colour of calendar.calendarList.list" in body
    pretty, compact = seen["pretty"], seen["compact"]
    assert isinstance(pretty, str) and isinstance(compact, str)
    assert pretty.startswith('{\n  "kind": "calendar#calendarList"') and "\n" not in compact
    assert json.loads(pretty) == json.loads(compact)
