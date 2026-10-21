"""The shapes Minutehand sends an agent's own product where the agent file names no vendor's, as one OpenAPI 3.1
document generated from the models that cross them, so an agent's team can generate types in any language
(`schemas/agent-api.openapi.json`).

Every endpoint is optional and none exists for Minutehand: each is a place the agent's own product already has (an
inbox of its own, a channel people answer on), which the agent file points at under whatever path it likes. The
paths here are suggestions, the shapes are the contract:

    deliverReply  POST  /replies                                  DeliveredReply    (`ReplyDelivery`, no `body`)
    listPending   GET   /inboxes/{inbox}/pending?person=&cursor=  PendingPage       (an inbox, `document: minutehand`)
    decide        POST  /inboxes/{inbox}/items/{item}/decision    DecisionMade      (an inbox, `document: minutehand`)

An inbox that names `document: minutehand` uses this document as an agent's own: Minutehand takes the method, path
and parameter locations from it and checks what the agent answers against it.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, JsonValue
from pydantic.json_schema import models_json_schema

from minutehand.domain.inboxes import DecisionMade, PendingPage
from minutehand.domain.outbound import DeliveredReply

VERSION = "1"
"""This document's own version, raised whenever a shape in it changes in a way an agent must follow."""

REF = "#/components/schemas/{model}"


def _body(model: type[BaseModel]) -> JsonValue:
    return {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}},
    }


def _answer(model: type[BaseModel] | None, what: str) -> JsonValue:
    if model is None:
        return {"2XX": {"description": what}}
    return {
        "200": {
            "description": what,
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}},
        }
    }


def _parameter(name: str, where: str, what: str, *, required: bool = True) -> JsonValue:
    return {"name": name, "in": where, "required": required, "description": what, "schema": {"type": "string"}}


def document() -> dict[str, JsonValue]:
    """The OpenAPI document, as JSON-ready structure."""
    models: list[type[BaseModel]] = [DeliveredReply, PendingPage, DecisionMade]
    _, schemas = models_json_schema([(m, "serialization") for m in models], ref_template=REF, by_alias=True)
    components = cast(dict[str, JsonValue], schemas["$defs"]) if "$defs" in schemas else {}
    paths: dict[str, JsonValue] = {
        "/replies": {
            "post": {
                "operationId": "deliverReply",
                "summary": "A person's answer to one of the agent's captured sends, in the default shape",
                "requestBody": _body(DeliveredReply),
                "responses": _answer(None, "The reply was taken"),
            }
        },
        "/inboxes/{inbox}/pending": {
            "get": {
                "operationId": "listPending",
                "summary": "What waits on one person in the agent's own product, a page at a time",
                "parameters": [
                    _parameter("inbox", "path", "The inbox's name in the agent file"),
                    _parameter("person", "query", "The email of the person whose items are asked for"),
                    _parameter(
                        "cursor", "query", "The `next` of the page before; absent for the first", required=False
                    ),
                ],
                "responses": _answer(PendingPage, "One page of items"),
            }
        },
        "/inboxes/{inbox}/items/{item}/decision": {
            "post": {
                "operationId": "decide",
                "summary": "A person decides one item",
                "parameters": [
                    _parameter("inbox", "path", "The inbox's name in the agent file"),
                    _parameter("item", "path", "The item's own id"),
                ],
                "requestBody": _body(DecisionMade),
                "responses": _answer(None, "The decision was taken; any other status is the product refusing it"),
            }
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "What an agent may implement for Minutehand",
            "version": VERSION,
            "description": "Every operation is optional. The agent file names the URL of each one it implements.",
        },
        "servers": [{"url": "http://127.0.0.1:8790", "description": "Wherever the agent listens"}],
        "paths": paths,
        "components": {"schemas": components},
    }
