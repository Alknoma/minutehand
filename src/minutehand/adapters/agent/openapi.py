"""An operation of an agent's OpenAPI document, as a request Minutehand makes and an answer it checks
(`domain.inboxes.OperationRequest`).

The document is read once, before a run: a file, an http(s) URL, or `minutehand` (`agent_api.document()`). The
operation is found by its `operationId`; its method, path, server and where each parameter goes come from the
document, and every parameter the declaration fills must be one the operation has, every required one filled, a body
given only when the operation takes one. Anything else is refused then, naming it.

What the agent answers is checked against the schema the document gives that status (or its `2XX`, or `default`),
with `jsonschema` over the whole document, so a `$ref` reads as it does for any other client of the document. A
mismatch is the agent's contract having changed, and it is said naming the field: `$.items[0].summary: None is not
of type 'string'`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import yaml
from jsonschema import Draft202012Validator
from pydantic import JsonValue
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from minutehand import agent_api
from minutehand.domain.inboxes import BUILT_IN, OperationRequest

DOCUMENT_URI = "urn:minutehand:agent-document"
METHODS = ("get", "post", "put", "patch", "delete")


class OperationUnresolved(ValueError):
    """The declaration names an operation its document does not have, or fills it as the document does not allow."""


def load(source: str) -> dict[str, object]:
    """The document a declaration names, as structure."""
    if source == BUILT_IN:
        return json.loads(json.dumps(agent_api.document()))
    if source.startswith(("http://", "https://")):
        try:
            answered = httpx.get(source, timeout=30.0, trust_env=False)
        except httpx.HTTPError as e:
            raise OperationUnresolved(f"the OpenAPI document {source} could not be fetched: {e!r}") from e
        if answered.is_error:
            raise OperationUnresolved(f"the OpenAPI document {source} answered {answered.status_code}")
        text = answered.text
    else:
        path = Path(source)
        if not path.is_file():
            raise OperationUnresolved(f"no OpenAPI document at {path.resolve()}")
        text = path.read_text(encoding="utf-8")
    try:
        found = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise OperationUnresolved(f"the OpenAPI document {source} is neither JSON nor YAML: {e}") from e
    if not isinstance(found, dict) or "paths" not in found:
        raise OperationUnresolved(f"{source} is no OpenAPI document: it has no `paths`")
    return found


def _escaped(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class Operation:
    """One operation, resolved: how to call it and how to check what it answers."""

    source: str
    operation_id: str
    method: str
    server: str
    path: str
    parameters: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    """Each parameter's name -> where it goes: path, query or header."""
    required: frozenset[str] = frozenset()
    takes_body: bool = False
    answers: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    """A status, `2XX` or `default` -> the JSON pointer of its JSON schema in the document."""
    registry: Registry = field(default_factory=Registry)

    def url(self, filled: Mapping[str, str]) -> tuple[str, dict[str, str]]:
        """The URL with the path and query parameters in place, and the header parameters."""
        path = self.path
        query: dict[str, str] = {}
        headers: dict[str, str] = {}
        for name, value in filled.items():
            where = self.parameters[name]
            if where == "path":
                path = path.replace("{" + name + "}", quote(value, safe="@"))
            elif where == "query":
                query[name] = value
            else:
                headers[name] = value
        return self.server.rstrip("/") + path + (f"?{urlencode(query)}" if query else ""), headers

    def mismatch(self, status: int, answer: JsonValue) -> str | None:
        """How `answer` departs from the schema the document gives `status`; None when it matches or the document
        gives none."""
        pointer = next(
            (self.answers[k] for k in (str(status), f"{str(status)[0]}XX", "default") if k in self.answers), None
        )
        if pointer is None:
            return None
        validator = Draft202012Validator({"$ref": f"{DOCUMENT_URI}#{pointer}"}, registry=self.registry)
        errors = sorted(validator.iter_errors(answer), key=lambda e: (len(e.path), e.json_path))
        if not errors:
            return None
        first = errors[0]
        return (
            f"{self.operation_id} ({self.source}) answered {status} with what its API description does not allow: "
            f"{first.json_path}: {first.message}" + (f", and {len(errors) - 1} more" if len(errors) > 1 else "")
        )


def resolve(
    request: OperationRequest, documents: dict[str, dict[str, object]], *, defaulted: frozenset[str]
) -> Operation:
    """`request`'s operation in its document (read once into `documents`). `defaulted` are parameters Minutehand
    fills itself (a page's cursor, a built-in default) beside the declaration's own."""
    if request.document not in documents:
        documents[request.document] = load(request.document)
    document = documents[request.document]
    paths = document["paths"]
    assert isinstance(paths, dict)
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            found = item[method] if method in item else None
            if not isinstance(found, dict) or "operationId" not in found or found["operationId"] != request.operation:
                continue
            return _resolved(request, document, str(path), method, found, item, defaulted)
    raise OperationUnresolved(f"the OpenAPI document {request.document} has no operation {request.operation!r}")


def _resolved(
    request: OperationRequest,
    document: dict[str, object],
    path: str,
    method: str,
    operation: dict[object, object],
    item: dict[object, object],
    defaulted: frozenset[str],
) -> Operation:
    where: dict[str, str] = {}
    required: set[str] = set()
    for declared in [*_list(item, "parameters"), *_list(operation, "parameters")]:
        if not isinstance(declared, dict) or "name" not in declared or "in" not in declared:
            continue
        name, place = str(declared["name"]), str(declared["in"])
        if place not in ("path", "query", "header"):
            continue
        where[name] = place
        if place == "path" or ("required" in declared and declared["required"] is True):
            required.add(name)
    unknown = sorted(set(request.parameters) - set(where))
    if unknown:
        raise OperationUnresolved(
            f"{request.operation} in {request.document} has no parameter {', '.join(unknown)}; it has "
            + (", ".join(sorted(where)) or "none")
        )
    missing = sorted(required - set(request.parameters) - defaulted)
    if missing:
        raise OperationUnresolved(f"{request.operation} in {request.document} requires {', '.join(missing)}, not given")
    takes_body = "requestBody" in operation
    if request.body is not None and not takes_body:
        raise OperationUnresolved(f"{request.operation} in {request.document} takes no body, and one is given")
    server = request.server
    if server is None:
        servers = document["servers"] if "servers" in document else None
        first = servers[0] if isinstance(servers, list) and servers else None
        if not isinstance(first, dict) or "url" not in first:
            raise OperationUnresolved(f"{request.document} names no server: give the request a `server`")
        server = str(first["url"])
    answers: dict[str, str] = {}
    responses = operation["responses"] if "responses" in operation else {}
    if isinstance(responses, dict):
        for status, answer in responses.items():
            content = answer["content"] if isinstance(answer, dict) and "content" in answer else None
            if isinstance(content, dict) and "application/json" in content:
                kind = content["application/json"]
                if isinstance(kind, dict) and "schema" in kind:
                    answers[str(status).upper()] = "/".join(
                        [
                            "",
                            "paths",
                            _escaped(path),
                            method,
                            "responses",
                            _escaped(str(status)),
                            "content",
                            "application~1json",
                            "schema",
                        ]
                    )
    resource = Resource(contents=document, specification=DRAFT202012)
    return Operation(
        source=request.document,
        operation_id=request.operation,
        method=method.upper(),
        server=server,
        path=path,
        parameters=where,
        required=frozenset(required),
        takes_body=takes_body,
        answers=answers,
        registry=Registry().with_resource(DOCUMENT_URI, resource),
    )


def _list(holder: dict[object, object], key: str) -> list[object]:
    found = holder[key] if key in holder else []
    return list(found) if isinstance(found, list) else []


def body_json(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode()
