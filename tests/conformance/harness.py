"""What every property shares: which providers are installed and what family each is in, the driver of each, the
cases a property runs, the server the suite talks to, and the world's neutral view.

Providers are found by walking the installed package's provider directories and reading each manifest (data only;
no provider code is imported), so a new provider directory is picked up with no registration. Its driver is found
at `tests/conformance/drivers/<key>.py`; a provider without one still has every case generated, and each fails
saying so.
"""

from __future__ import annotations

import importlib
import importlib.resources
import itertools
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from minutehand.adapters.control.wire import CreateWorld, Inbound
from minutehand.domain.provider import Manifest
from minutehand.domain.world import EntityKind, Operation, Snapshot, WorldEvent
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld
from tests.conformance.contract import (
    FAMILY_SESSIONS,
    STAMP_CHANNEL,
    STAMP_PROJECT,
    Api,
    Driver,
    Family,
    Property,
    Recorder,
    Session,
)

START = "2026-09-01T09:00:00Z"

PEOPLE: list[dict[str, object]] = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
    {"key": "mila", "name": "Mila Hart", "email": "mila@example.com", "reply": {"kind": "silent"}},
]


# ---------------------------------------------------------------------------------------------- providers


def manifests() -> list[Manifest]:
    """Every provider the installed package carries, found by its directory."""
    root = importlib.resources.files("minutehand.adapters.providers")
    found: list[Manifest] = []
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.is_dir() and entry.joinpath("manifest.py").is_file():
            module = importlib.import_module(f"minutehand.adapters.providers.{entry.name}.manifest")
            manifest = module.MANIFEST
            assert isinstance(manifest, Manifest)
            found.append(manifest)
    return found


KIND_FAMILY = {
    EntityKind.TICKET: Family.TICKETS,
    EntityKind.MESSAGE: Family.MESSAGING,
    EntityKind.DOCUMENT: Family.DOCUMENTS,
}


def families(manifest: Manifest) -> list[Family]:
    return [Family.ACCOUNTS, *(KIND_FAMILY[k] for k in manifest.kinds if k in KIND_FAMILY)]


MANIFESTS = {m.key: m for m in manifests()}
PROVIDERS = sorted(MANIFESTS)


def in_family(family: Family) -> list[str]:
    return [p for p in PROVIDERS if family in families(MANIFESTS[p])]


class NoDriver(Exception):
    pass


def driver_of(provider: str) -> Driver:
    """The provider's driver, or `NoDriver` naming the file it needs."""
    try:
        module = importlib.import_module(f"tests.conformance.drivers.{provider}")
    except ModuleNotFoundError as missing:
        if missing.name != f"tests.conformance.drivers.{provider}":
            raise
        raise NoDriver(
            f"provider {provider!r} has no conformance driver: add tests/conformance/drivers/{provider}.py "
            "defining DRIVER (docs/conformance.md)"
        ) from None
    driver = module.DRIVER
    assert isinstance(driver, Driver) and driver.provider == provider
    return driver


def require(provider: str, family: Family) -> Driver:
    """The driver, which must drive `family`: a provider in a family its driver cannot drive fails here."""
    driver = driver_of(provider)
    wanted = FAMILY_SESSIONS[family]
    if not issubclass(driver.session, wanted):
        raise NoDriver(
            f"provider {provider!r} is in the {family.value} family (its manifest maps "
            f"{', '.join(k.value for k in MANIFESTS[provider].kinds)}), and its driver's session "
            f"{driver.session.__name__} does not drive it: implement {wanted.__name__}"
        )
    return driver


def holds_channels(provider: str) -> bool:
    """Whether a messaging provider's conversations are channels a seed can declare: every one's are, but where its
    driver declares `messaging.channels` absent. A provider with no driver is taken to, so its cases fail by name."""
    try:
        return "messaging.channels" not in driver_of(provider).absent
    except NoDriver:
        return True


# ---------------------------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    provider: str
    prop: Property
    case: str

    @property
    def id(self) -> str:
        return f"{self.provider}-{self.case}"


def cases(prop: Property, family: Family, names: Sequence[str], providers: Sequence[str] | None = None) -> list[Case]:
    chosen = in_family(family) if providers is None else providers
    return [Case(p, prop, n) for p in chosen for n in names]


def parametrize(found: Sequence[Case]) -> pytest.MarkDecorator:
    return pytest.mark.parametrize("case", list(found), ids=[c.id for c in found])


ABSENT_PROPERTY = "conformance_not_applicable"


def absent(driver: Driver, capability: str, record: Callable[[str, object], None]) -> bool:
    """True when the driver declares the vendor has no such thing: the reason is recorded on the report (and listed
    at the end of the run), and the property holds vacuously for it. Never a silent skip."""
    reason = driver.absent.get(capability)
    if reason is None:
        return False
    record(ABSENT_PROPERTY, f"{driver.provider} {capability}: {reason}")
    return True


# ---------------------------------------------------------------------------------------------- the server


@dataclass
class Server:
    url: str
    process: subprocess.Popen[str]
    state: Path

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(10)


BANNER = re.compile(r"control (http://\S+)/v1")


@contextmanager
def minutehand_serve(state: Path) -> Iterator[Server]:
    """`minutehand serve` as its own process, on free loopback ports, as a suite outside this repository runs it."""
    command = Path(sys.executable).with_name("minutehand")
    process = subprocess.Popen(
        [
            str(command),
            "serve",
            "--state",
            str(state),
            "--proxy-port",
            "0",
            "--control-port",
            "0",
            "--telemetry-port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "MINUTEHAND_URL"},
    )
    assert process.stdout is not None
    said: list[str] = []
    for line in process.stdout:
        said.append(line)
        found = BANNER.search(line)
        if found:
            server = Server(found.group(1), process, state)
            break
    else:
        process.wait(10)
        raise RuntimeError("minutehand serve did not start:\n" + "".join(said))
    try:
        yield server
    finally:
        server.stop()


@contextmanager
def fresh_server() -> Iterator[MinutehandClient]:
    """A second server of its own, for a property that compares with a server nothing has touched."""
    with tempfile.TemporaryDirectory(prefix="conformance-fresh-") as state:
        with minutehand_serve(Path(state)) as server, MinutehandClient(server.url) as client:
            yield client


_tags = itertools.count()


def tag() -> str:
    """Lowercase letters and digits unique to this process and call: a Jira site, a YouTrack instance, a token."""
    return f"c{uuid.uuid4().hex[:8]}{next(_tags)}"


class Harness:
    """What a property test holds: the control API's client and a recording API for drivers."""

    def __init__(self, client: MinutehandClient) -> None:
        self.client = client
        environment = client.environment()
        self.api = Api(proxy=environment["HTTPS_PROXY"], bundle=environment["SSL_CERT_FILE"], recorder=Recorder())

    def spec(self, driver: Driver, seed: dict[str, object], *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        return driver.world(seed, tag(), logins=logins)

    @contextmanager
    def world(
        self,
        driver: Driver,
        seed: dict[str, object],
        *,
        inbound: list[Inbound] | None = None,
        logins: Mapping[str, str] | None = None,
        client: MinutehandClient | None = None,
        spec: CreateWorld | None = None,
    ) -> Iterator[OpenWorld]:
        through = client or self.client
        chosen = spec or self.spec(driver, seed, logins=logins)
        if inbound:
            chosen = chosen.model_copy(update={"inbound": [*chosen.inbound, *inbound]})
        world = OpenWorld(through, through.create_world(chosen))
        try:
            yield world
        finally:
            through.close_world(world.world_id, quiet=False)

    def session(self, driver: Driver, world: OpenWorld, *, person: str | None = None) -> Session:
        return driver.connect(self.api, world.view, person=person)


def seed(provider: str, **more: object) -> dict[str, object]:
    """The base seed every property starts from: three people, and for each family the provider is in, the place
    its operations act in: a project `Launch` holding one ticket, a channel `launch` everyone is in (where the
    provider's conversations are channels at all)."""
    found: dict[str, object] = {"starts_at": START, "people": [dict(p) for p in PEOPLE]}
    held = families(MANIFESTS[provider])
    if Family.TICKETS in held:
        found["tickets"] = [{"provider": provider, "project": STAMP_PROJECT, "title": "Book the venue",
                             "assignee": "sofia"}]  # fmt: skip
    if Family.MESSAGING in held and holds_channels(provider):
        found["channels"] = [{"provider": provider, "name": STAMP_CHANNEL, "members": ["owen", "sofia", "mila"]}]
    return found | more


# ---------------------------------------------------------------------------------------------- the neutral view


def changes(events: Sequence[WorldEvent], provider: str) -> list[WorldEvent]:
    return [
        e for e in events if e.entity.provider == provider and e.operation not in (Operation.READ, Operation.SEARCH)
    ]


def latest(events: Sequence[WorldEvent], provider: str) -> dict[tuple[EntityKind, str], Snapshot | None]:
    """Each entity's neutral state at the head: its latest snapshot, None once deleted."""
    found: dict[tuple[EntityKind, str], Snapshot | None] = {}
    for event in changes(events, provider):
        key = (event.entity.kind, event.entity.external_id)
        if event.operation is Operation.DELETE:
            found[key] = None
        elif event.after is not None:
            found[key] = event.after
    return found
