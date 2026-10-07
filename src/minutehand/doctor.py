"""`minutehand doctor -- <command>`: which HTTP clients in the agent's interpreter would go around the proxy.

Minutehand is configured into an agent by its environment alone (`HTTPS_PROXY`, `NO_PROXY`, a CA bundle). A client
library that does not read those variables reaches the real network in silence: nothing is recorded, nothing is
refused, and the run reads as if the agent never made the call. This cannot be fixed from outside the agent, but
it can be found before a run.

The doctor starts a proxy as a run would, and runs a probe in the interpreter of the agent's command (its first
word when that is a Python, else that command with `-c`): with exactly the environment the agent is handed, each
client library installed there sends one plain request to a canary host that does not exist. A request the proxy
sees reached it; one that fails to resolve the canary went around it. With `--agent`, the declared outbound hosts
and model hosts are also checked against the handed-out `NO_PROXY`, as each library reads it.

An agent in a container can lose the handed-out `NO_PROXY` before it starts: the Docker CLI copies the client
config's `proxies.<daemon>.noProxy` into `NO_PROXY` and `no_proxy` of every container it starts, over a value from
`--env-file` though not one from `-e` or Compose's `environment:` or `env_file:`. Docker Desktop writes `"*"` there,
which sends every call straight to the real service. `docker_warnings` reads that file for the doctor and for
`minutehand env`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS, Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.scenario import Model
from minutehand.session import Listen, agent_environment

CANARY = "0.0.0.1"
"""Where each library's canary request goes: an address no packet can reach, so a client that goes around the proxy
fails at once on this machine, with no DNS lookup and nothing sent to any network; through the proxy it is
refused 502 naming this host, which is how the probe knows it arrived."""

ADVICE = {
    "requests": "reads HTTPS_PROXY; nothing to do",
    "httpx": "reads HTTPS_PROXY (trust_env=True, the default); nothing to do",
    "urllib": "reads HTTPS_PROXY; nothing to do",
    "aiohttp": "ignores HTTPS_PROXY unless the session is made with ClientSession(trust_env=True)",
    "urllib3": "a bare PoolManager ignores HTTPS_PROXY: use urllib3.ProxyManager(os.environ['HTTPS_PROXY']) or requests",
    "httplib2": "reads HTTPS_PROXY only when PySocks is installed beside it: pip install pysocks",
}

PROBE = r"""
import json, os, sys
canary = sys.argv[1]
url = f"http://{canary}/doctor"
found = {}
def reached(text):
    try:
        return json.loads(text).get("host") == canary
    except Exception:
        return False
def attempt(name, fn):
    try:
        found[name] = "reached" if reached(fn()) else "answered by something else"
    except ModuleNotFoundError:
        found[name] = "not installed"
    except Exception as e:
        found[name] = "bypassed: " + type(e).__name__ + ": " + str(e)[:160]
def with_requests():
    import requests
    return requests.get(url, timeout=5).text
def with_httpx():
    import httpx
    return httpx.get(url, timeout=5).text
def with_urllib():
    import urllib.request, urllib.error
    try:
        return urllib.request.urlopen(url, timeout=5).read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()
def with_urllib3():
    import urllib3
    return urllib3.PoolManager().request("GET", url, timeout=5, retries=False).data.decode()
def with_httplib2():
    import httplib2
    return httplib2.Http(timeout=5).request(url)[1].decode()
def with_aiohttp():
    import aiohttp, asyncio
    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                return await r.text()
    return asyncio.run(go())
for name, fn in [("requests", with_requests), ("httpx", with_httpx), ("urllib", with_urllib),
                 ("urllib3", with_urllib3), ("httplib2", with_httplib2), ("aiohttp", with_aiohttp)]:
    attempt(name, fn)
hosts = json.loads(sys.argv[2])
direct = {}
for host in hosts:
    target = f"https://[{host}]/" if ":" in host else f"https://{host}/"
    seen = {}
    try:
        import requests.utils
        seen["requests"] = bool(requests.utils.should_bypass_proxies(target, None))
    except ModuleNotFoundError:
        pass
    import urllib.request
    seen["urllib"] = bool(urllib.request.proxy_bypass_environment(host))
    try:
        import httpx
        with httpx.Client() as c:
            seen["httpx"] = c._transport_for_url(httpx.URL(target)) is c._transport
    except ModuleNotFoundError:
        pass
    try:
        import aiohttp.helpers, yarl
        try:
            aiohttp.helpers.get_env_proxy_for_url(yarl.URL(target))
            seen["aiohttp"] = False
        except LookupError:
            seen["aiohttp"] = True
    except ModuleNotFoundError:
        pass
    direct[host] = seen
installed = sorted(name for name, result in found.items() if result != "not installed")
print(json.dumps({"libraries": found, "hosts": direct, "installed": installed}))
"""


def _ipv6(host: str) -> bool:
    try:
        ipaddress.IPv6Address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _named(host: str, entry: str) -> bool:
    """Whether a `NO_PROXY` entry covers `host` by curl's documented rule: the host itself, or a domain it is under.
    An address entry is matched as an address."""
    entry = entry.strip().lower().lstrip(".")
    host = host.lower()
    return bool(entry) and (host == entry or (not _ipv6(host) and host.endswith("." + entry)))


def _node(host: str, entry: str) -> bool:
    """Whether a `NO_PROXY` entry covers `host` as Node's clients that read it do (undici's `EnvHttpProxyAgent`,
    `proxy-from-env` under axios): exactly, unless the entry starts with `.` or `*`, which make it a suffix."""
    entry = entry.strip().lower()
    host = host.lower()
    if entry.startswith((".", "*")):
        return host.endswith(entry.lstrip("*"))
    return host == entry


def _documented(host: str, no_proxy: Sequence[str]) -> list[str]:
    """The clients outside the probed interpreter that would send `host` direct, by each one's documented rule."""
    found = ["curl"] if any(_named(host, e) for e in no_proxy) else []
    return found + (["node"] if any(_node(host, e) for e in no_proxy) else [])


DOCKER_CONFIG = "DOCKER_CONFIG"
"""The variable naming the Docker client's config folder, as the Docker CLI reads it; unset is `~/.docker`."""


class _DockerProxies(BaseModel):
    """One daemon's entry under `proxies` in the Docker client config; the rest of it is Docker's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    no_proxy: str | None = Field(default=None, alias="noProxy")


class _DockerConfig(BaseModel):
    """The part of the Docker client config the CLI copies into a container's environment; the rest is Docker's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    proxies: dict[str, _DockerProxies] = {}


def docker_config(environ: Mapping[str, str]) -> Path:
    """The Docker client config the Docker CLI on this machine reads."""
    folder = Path(environ[DOCKER_CONFIG]) if DOCKER_CONFIG in environ else Path.home() / ".docker"
    return folder / "config.json"


def claimed_hosts(agent: AgentUnderTest | None) -> list[str]:
    """Every host a call must reach the proxy for: each installed provider's, and the agent's declared hosts."""
    hosts = [host for manifest in Registry.installed().manifests for host in manifest.hosts]
    hosts += [d.host for d in agent.outbound] if agent is not None else []
    return list(dict.fromkeys(hosts))


def _covers(entry: str, host: str) -> bool:
    """Whether a `NO_PROXY` entry sends a host matching `host` (exact, or `*.` and a domain) direct, by curl's rule,
    the widest a client reads: the entry is the host, a domain it is under, or a host under its wildcard."""
    bare = entry.strip().lower().removeprefix("*").lstrip(".")
    domain = host.lower().removeprefix("*.")
    return _named(domain, bare) or (host.startswith("*.") and bool(bare) and bare.endswith("." + domain))


def docker_warnings(hosts: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """One warning per daemon entry in the Docker client config whose `noProxy` would replace the `NO_PROXY` handed
    to an agent in a container and send a call to one of `hosts` around the proxy: `*`, or an entry covering one."""
    path = docker_config(environ)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        return [f"{path} could not be read, so its proxies' noProxy was not checked: {e}"]
    try:
        config = _DockerConfig.model_validate_json(text)
    except ValidationError as e:
        said = str(e).splitlines()
        return [
            f"{path} could not be read as a Docker client config, so its proxies' noProxy was not checked: {said[0]}"
        ]
    found: list[str] = []
    for daemon, proxies in config.proxies.items():
        if proxies.no_proxy is None:
            continue
        entries = [e.strip() for e in proxies.no_proxy.split(",") if e.strip()]
        if "*" in entries:
            what = "every call"
        else:
            covered = [h for h in hosts if any(_covers(e, h) for e in entries)]
            if not covered:
                continue
            more = f" and {len(covered) - 5} more" if len(covered) > 5 else ""
            what = f"calls to {', '.join(covered[:5])}{more}"
        found.append(
            f"{path} sets proxies.{daemon}.noProxy to {proxies.no_proxy!r}: the Docker CLI copies it into NO_PROXY "
            "and no_proxy of every container it starts, over the values in an --env-file, so an agent in a container "
            f"sends {what} straight to the real service and the run records nothing. Hand the container both "
            "NO_PROXY and no_proxy with -e, Compose's environment: or env_file:, or its entrypoint; or remove "
            "noProxy from that file (docs/containers.md)"
        )
    return found


class LibraryCheck(Model):
    library: str
    result: str = Field(description="reached, not installed, or bypassed with the error")
    advice: str


class HostCheck(Model):
    host: str
    bypassed_by: list[str] = Field(description="Libraries that would send a call to this host around the proxy")
    unreachable_by: list[str] = Field(
        default=[],
        description="Installed libraries that cannot reach this host through any proxy: httpx asks for a tunnel to "
        "an IPv6 literal without its brackets, which the proxy refuses naming the cause",
    )


class Diagnosis(Model):
    interpreter: str
    libraries: list[LibraryCheck]
    hosts: list[HostCheck]
    notes: list[str]

    no_proxy: list[str] = Field(default=[], description="The NO_PROXY the agent is handed, entry by entry")
    docker: list[str] = Field(
        default=[],
        description="Warnings about the Docker client config: a noProxy that would replace the handed-out NO_PROXY "
        "in a container and send calls to claimed hosts around the proxy",
    )

    @property
    def bypasses(self) -> bool:
        return (
            any(c.result.startswith("bypassed") for c in self.libraries)
            or any(h.bypassed_by for h in self.hosts)
            or any(h.unreachable_by for h in self.hosts)
        )


@dataclass(frozen=True)
class _Probe:
    argv: list[str]
    note: str | None


def _interpreter(command: Sequence[str]) -> _Probe:
    first = Path(command[0]).name
    if first.startswith("python"):
        return _Probe([command[0]], None)
    if first == "uv" and len(command) > 1 and command[1] == "run":
        return _Probe(["uv", "run", "python"], None)
    found = shutil.which("python3") or "python3"
    return _Probe([found], f"{command[0]} is not a Python; the probe ran in {found}, which may not be the agent's")


async def diagnose(
    command: Sequence[str], agent: AgentUnderTest | None, model_hosts: Sequence[str], listen: Listen | None = None
) -> Diagnosis:
    """`listen` is how the run will be configured (`--agent-host`, `--no-proxy`): what decides the `NO_PROXY` the
    agent is handed."""
    probe = _interpreter(command)
    listen = listen or Listen()
    hosts = sorted({d.host for d in agent.outbound if not d.host.startswith("*.")} if agent is not None else set())
    hosts += [h for h in model_hosts if h not in DEFAULT_MODEL_HOSTS]
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch)
        clock = RunClock(datetime.now(UTC))  # clock-lint: exempt a probe outside any run, stamped for the record only
        store = SqliteStore(base / "world.db", "doctor", clock)
        async with Proxy(Routing(Registry.installed(), model_hosts=list(model_hosts)), store, clock, confdir=base) as p:
            # The proxy as this machine reaches it, and the NO_PROXY the run will hand the agent wherever it runs.
            handed = agent_environment(Listen(), p.port, p.ca_bundle, {}, telemetry_port=None)
            direct = ",".join(listen.direct())
            env = {**os.environ, **handed, "NO_PROXY": direct, "no_proxy": direct, "no_grpc_proxy": direct}
            process = await asyncio.create_subprocess_exec(
                *probe.argv,
                "-c",
                PROBE,
                CANARY,
                json.dumps(hosts),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), 120)
        store.close()
    if process.returncode != 0:
        raise RuntimeError(f"the probe failed in {' '.join(probe.argv)}: {err.decode(errors='replace')[-1500:]}")
    found = json.loads(out.decode().strip().splitlines()[-1])
    libraries = [
        LibraryCheck(library=name, result=result, advice=ADVICE[name]) for name, result in found["libraries"].items()
    ]
    no_proxy = listen.direct()
    installed: list[str] = found["installed"]
    checked = [
        HostCheck(
            host=host,
            bypassed_by=sorted({*(lib for lib, direct in seen.items() if direct), *_documented(host, no_proxy)}),
            unreachable_by=["httpx"] if "httpx" in installed and _ipv6(host) else [],
        )
        for host, seen in found["hosts"].items()
    ]
    notes = [probe.note] if probe.note else []
    notes.append(
        "curl and Node are not probed: a host is checked against the NO_PROXY by their documented rules (curl: the "
        "host or a domain it is under; undici and proxy-from-env: the host exactly, a suffix only for an entry "
        "starting with . or *)"
    )
    notes.append(
        "Node's built-in fetch reads HTTPS_PROXY only with NODE_USE_ENV_PROXY=1, which Minutehand hands every agent and "
        "Node 24 and later read; an older Node's fetch goes around the proxy (not probed here): give it a base URL, or "
        "run it in a container with --transparent-port"
    )
    return Diagnosis(
        interpreter=" ".join(probe.argv),
        libraries=libraries,
        hosts=checked,
        notes=notes,
        no_proxy=no_proxy,
        docker=docker_warnings(claimed_hosts(agent), os.environ),
    )


def described(diagnosis: Diagnosis) -> str:
    lines = [f"probed in {diagnosis.interpreter}, with the environment a run hands the agent", ""]
    for check in diagnosis.libraries:
        mark = "ok  " if check.result == "reached" else ("--  " if check.result == "not installed" else "MISS")
        line = f"  {mark} {check.library}: {check.result}"
        if check.result.startswith("bypassed"):
            line += f"\n         {check.advice}"
        lines.append(line)
    if diagnosis.hosts:
        lines.append("")
        lines.append(f"declared hosts, against the NO_PROXY the agent is handed ({','.join(diagnosis.no_proxy)}):")
        for host in diagnosis.hosts:
            if host.bypassed_by:
                lines.append(
                    f"  MISS {host.host}: {', '.join(host.bypassed_by)} would send it directly (a name in NO_PROXY "
                    "covers every name under it for these)"
                )
            if host.unreachable_by:
                lines.append(
                    f"  MISS {host.host}: {', '.join(host.unreachable_by)} cannot reach an IPv6 literal through any "
                    "proxy (its CONNECT omits the brackets, and the proxy refuses it saying so): give the host a name"
                )
            if not host.bypassed_by and not host.unreachable_by:
                lines.append(f"  ok   {host.host}")
    if diagnosis.docker:
        lines += ["", *[f"WARN {warning}" for warning in diagnosis.docker]]
    lines += ["", *[f"note: {n}" for n in diagnosis.notes]]
    return "\n".join(lines)
