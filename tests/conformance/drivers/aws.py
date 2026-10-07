"""The AWS driver: EventBridge Scheduler and SQS, reached as a real client reaches them.

Every request is signed with Signature Version 4 (botocore's own signer, so the signature is what an SDK sends) and
sent through `Api.http`, so the record holds it byte for byte. The scenario's people are no AWS principals: the
provider seeds no IAM user (`AwsProvider.seed` declares no resources), so this driver is in the accounts family only,
and its writes are schedules.

A world is reached by the hosts it claims, never by a credential (docs/serve.md, "AWS | No | SigV4 is no OAuth
credential"), and moto answers only the regions it knows (`sqs_backends[account]["<anything else>"]` raises
KeyError), so every world claims the same `us-east-1` hosts and two cannot be open at once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote, urlencode

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import Api, Driver, IdKind, PersonSeen, Session, ok

REGION = "us-east-1"
SCHEDULER = f"scheduler.{REGION}.amazonaws.com"
SQS = f"sqs.{REGION}.amazonaws.com"
STS = f"sts.{REGION}.amazonaws.com"
QUEUE = "conformance-wakes"
AT = "at(2030-01-01T09:00:00)"
STS_NS = {"sts": "https://sts.amazonaws.com/doc/2011-06-15/"}
NO_PEOPLE = (
    "the scenario's people are no AWS principals: the aws provider seeds no IAM user, role or access key for a "
    "person (AwsProvider.seed declares no resources), so AWS has no account of theirs to list or act as"
)


def access_key(tag: str) -> str:
    """An IAM access key id: `AKIA` and sixteen characters of base32 upper case, as AWS mints them."""
    digest = base64.b32encode(hashlib.sha256(f"key:{tag}".encode()).digest()).decode()
    return "AKIA" + digest[:16]


def secret_key(key_id: str) -> str:
    """The forty-character secret access key paired with `key_id`."""
    return base64.b64encode(hashlib.sha256(f"secret:{key_id}".encode()).digest()).decode()[:40]


def epoch(value: object, what: str) -> datetime:
    """A timestamp as AWS's JSON protocols send one: seconds since the epoch, a JSON number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssertionError(f"{what} is {value!r}, not epoch seconds as AWS's JSON protocols send a timestamp")
    return datetime.fromtimestamp(value, UTC)


class AwsSession(Session):
    def __init__(self, api: Api, key_id: str) -> None:
        self._http = api.http()
        self._credentials = Credentials(key_id, secret_key(key_id))
        self._queue_arn: str | None = None
        self.secrets = (key_id,)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------------------------------ signing

    def _send(
        self,
        service: str,
        method: str,
        url: str,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        credentials: Credentials | None = None,
    ) -> httpx.Response:
        request = AWSRequest(method=method, url=url, data=body, headers=dict(headers or {}))
        SigV4Auth(credentials or self._credentials, service, REGION).add_auth(request)
        signed = {k: str(v) for k, v in request.headers.items()}
        return self._http.request(method, url, content=body, headers=signed)

    def _scheduler(
        self, method: str, path: str, body: object = None, credentials: Credentials | None = None
    ) -> httpx.Response:
        content = b"" if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        return self._send("scheduler", method, f"https://{SCHEDULER}{path}", content, headers, credentials)

    def _sqs(self, action: str, body: Mapping[str, object]) -> httpx.Response:
        headers = {"Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": f"AmazonSQS.{action}"}
        return self._send("sqs", "POST", f"https://{SQS}/", json.dumps(body).encode(), headers)

    # ------------------------------------------------------------------------------------------ accounts

    def whoami(self) -> str:
        """STS GetCallerIdentity (query protocol): the caller's ARN."""
        form = urlencode({"Action": "GetCallerIdentity", "Version": "2011-06-15"}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        answered = ok("GetCallerIdentity", self._send("sts", "POST", f"https://{STS}/", form, headers))
        arn = ElementTree.fromstring(answered.content).find("sts:GetCallerIdentityResult/sts:Arn", STS_NS)
        if arn is None or not arn.text:
            raise AssertionError(f"GetCallerIdentity answered no Arn: {answered.text[:300]}")
        return arn.text

    def people(self) -> list[PersonSeen]:
        raise NotImplementedError(NO_PEOPLE)

    def people_pages(self, page_size: int) -> list[list[str]]:
        raise NotImplementedError(NO_PEOPLE)

    def unknown_credential(self) -> httpx.Response:
        """ListSchedules signed with an access key no world was given."""
        stranger = Credentials("AKIAUNKNOWNCALLER000", "unknownunknownunknownunknownunknownunkno")
        return self._scheduler("GET", "/schedules", credentials=stranger)

    # ------------------------------------------------------------------------------------------ schedules

    def _schedule_names(self) -> list[str]:
        names: list[str] = []
        token: str | None = None
        while True:
            query = "" if token is None else "?" + urlencode({"NextToken": token})
            answered = ok("ListSchedules", self._scheduler("GET", f"/schedules{query}")).json()
            names.extend(s["Name"] for s in answered["Schedules"])
            token = answered["NextToken"] if "NextToken" in answered else None
            if not token:
                return names

    def _get_schedule(self, name: str) -> httpx.Response:
        return ok(f"GetSchedule {name}", self._scheduler("GET", f"/schedules/{quote(name, safe='')}"))

    def observe(self) -> str:
        """ListSchedules, then GetSchedule of each schedule it lists, by name."""
        listed = sorted(self._schedule_names())
        return "\n".join(self._get_schedule(name).text for name in listed)

    def _target_queue(self) -> str:
        """The SQS queue the schedules deliver to, made (CreateQueue is idempotent) and its ARN read."""
        if self._queue_arn is None:
            url = ok("CreateQueue", self._sqs("CreateQueue", {"QueueName": QUEUE})).json()["QueueUrl"]
            attributes = ok(
                "GetQueueAttributes", self._sqs("GetQueueAttributes", {"QueueUrl": url, "AttributeNames": ["QueueArn"]})
            ).json()["Attributes"]
            self._queue_arn = str(attributes["QueueArn"])
        return self._queue_arn

    def _create(self, label: str) -> str:
        name = "label-" + hashlib.sha256(label.encode()).hexdigest()[:24]
        queue = self._target_queue()
        account = queue.split(":")[4]
        body = {
            "ScheduleExpression": AT,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Description": label,
            "Target": {
                "Arn": queue,
                "RoleArn": f"arn:aws:iam::{account}:role/scheduler",
                "Input": json.dumps({"label": label}),
            },
        }
        ok(f"CreateSchedule {name}", self._scheduler("POST", f"/schedules/{quote(name, safe='')}", body))
        return name

    def change(self, label: str) -> None:
        """CreateSchedule: a one-time schedule whose Description and Target.Input hold `label`."""
        self._create(label)

    def stamp(self, label: str) -> tuple[str, datetime]:
        made = self._create(label)
        return made, self.stamped(made)

    def stamped(self, made: str) -> datetime:
        """GetSchedule's CreationDate."""
        answered = self._get_schedule(made).json()
        if "CreationDate" not in answered:
            raise AssertionError(f"GetSchedule {made} answered no CreationDate: {answered}")
        return epoch(answered["CreationDate"], "CreationDate")


class AwsDriver(Driver):
    provider = "aws"
    session = AwsSession
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.people": NO_PEOPLE,
        "accounts.person_credentials": NO_PEOPLE,
        "accounts.title": NO_PEOPLE,
        "accounts.bot": NO_PEOPLE,
        "accounts.guest": NO_PEOPLE,
        "accounts.deactivated": NO_PEOPLE,
        "accounts.no_email": NO_PEOPLE,
        "accounts.vendor_login": NO_PEOPLE,
        "listing.people": NO_PEOPLE,
        "state.concurrent": "SigV4 is no OAuth credential: a world claims the host, or is the default, one at a time "
        "(docs/serve.md), and moto answers only the regions it knows, so no two worlds can claim hosts of their own",
    }
    # No id kind of the accounts family applies: the provider lists no person (`accounts.people` is absent).
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {}
    page_floor: ClassVar[Mapping[str, int]] = {}
    # A request signed with an access key AWS does not know: 403 UnrecognizedClientException, "The security token
    # included in the request is invalid." (EventBridge Scheduler API reference, Common Errors).
    unknown_refusal = (403, "The security token included in the request is invalid")

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError(f"AWS has no login of a person's own: {self.absent['accounts.vendor_login']}")
        # The access key is claimed as a token only so the world names it (`credential_of`); nothing routes on it.
        claims = Claims(hosts=[SCHEDULER, SQS, STS], tokens=[access_key(tag)])
        return CreateWorld(seed=Seed.model_validate(seed), claims=claims)

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        if person is not None:
            raise NotImplementedError(NO_PEOPLE)
        return AwsSession(api, self._key(world))

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        return None if person is not None else self._key(world)

    @staticmethod
    def _key(world: WorldView) -> str:
        [key] = world.claims.tokens
        return key


DRIVER = AwsDriver()
