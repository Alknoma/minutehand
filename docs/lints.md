# Lints

`wall_clock`, `import_boundaries`, `enum_string_comparisons` and `boundary_dicts` were agreed and are written; `provider_manifest` was not agreed and does not exist. Each section states the five tests.

A new repo has no debt, so every lint here is fail-closed with an inline marker that requires a reason: `# <name>-lint: exempt <reason>` (`clock`, `import`, `enum`, `dict`). A bare marker exempts nothing. There are no baselines.

## How they run

`lints/_core.py` holds what every lint shares: the scan root (`src/`), one walk and parse, `exempt()`, and `Finding(file, line, message, severity, kind)` with `line=None` for a finding about a file. A lint is a module in `lints/` defining `TITLE` and `run(root) -> list[Finding]`; `python -m lints` discovers every such module (no registration), runs all, and exits 1 if any has findings or could not run. `python -m lints.<name> [root]` runs one, over `src/` or the root given. Each lint's tests in `tests/lints/` plant a tree in `tmp_path` holding the violation and the legal code beside it.

## Agreed

### 1. `wall_clock` — written, runs today

| Test | Answer |
|---|---|
| The rule | A fake never reads the machine's clock; every timestamp comes from the run's clock. |
| What pyright cannot see | `datetime.now()` type-checks. Which clock a timestamp came from is not in its type. |
| 1. Recurred | 72 reads across the 9 existing emulators: Slack 37, Asana 11, Teams 10, Jira 6, YouTrack 3, Graph 2, Drive 1, GitHub 1, Notion 1. |
| 2. Catches what happened | Run `f431fc97f427`: a ticket filed on the mission's 30 August shows as created 24 August. The Slack `ts` at `docker/slack-emulator/main.py:1361` is one of the 72. |
| 3. Seen to fail | `python -m lints.wall_clock` over the parent repository's emulator directory → 72 findings, exit 1. On `src/` → 0. |
| 4. Fires on nothing adjacent | Matches `time.time`, `time.time_ns`, `datetime.now/utcnow/today`, `date.today`, however the owner is reached and under whatever name an import gives it (`import time as t; t.time()`, `from datetime import datetime as dt; dt.now()`), and a clock function imported by name and called bare (`from time import time; time()`). A bare name no clock import bound (a local `def time()`, `from time import monotonic as time`) is left alone. `time.monotonic` and `time.perf_counter` (durations) are untouched. |
| Cannot see | A clock read through a name bound some other way: `now = datetime.now` then `now()`, `getattr(time, "time")()`. |
| Escape | `# clock-lint: exempt <reason>` on the line. Two lines carry it: `SqliteStore.apply` filling `WorldEvent.wall_time`, and the Slack provider's `X-Slack-Request-Timestamp` (`adapters/providers/slack/inbound.py`), which the agent checks against its own machine clock. |
| Cost | One marker at each place wall time is truly wanted. |

### 2. `import_boundaries` — written

| Test | Answer |
|---|---|
| The rule | `domain` imports nothing from `application`, `adapters` or `ports`; `ports` nothing from `application` or `adapters`; `application` nothing from `adapters`; `checks` only `domain` and `ports`; `adapters.providers.<a>` nothing from `adapters.providers.<b>`. |
| 1. Recurred | The parent repo's lint exists because it did. Not yet in this repo. |
| 2. Catches what happened | The parent's regex missed package-level `from X.adapters import f` and hid three real violations. This one reads the AST, so `import a.b`, `from a import b`, relative imports and imports inside functions or `TYPE_CHECKING` are all one import. |
| 3. Seen to fail | One planted file per rule and spelling; removing the provider check, relative-import resolution or the domain→ports rule each turns a test red. |
| 4. Fires on nothing adjacent | `minutehand.adapters_notes` is not `minutehand.adapters`; `from minutehand.adapters.providers import X` names a provider only when `X` exists under `adapters/providers/`. |
| Cannot see | An import spelled as a string: `importlib.import_module`, an entry point in `pyproject.toml`. |
| Cost | None until someone crosses the boundary. |

### 3. `enum_string_comparisons` — written

| Test | Answer |
|---|---|
| The rule | Compare against an enum member, never its value spelled out. |
| Why here | Every kind in `domain/` is a `StrEnum` (`FindingKind`, `Operation`, `Actor`, `WaitingOn`, …). `finding.kind == "fail"` type-checks and a typo answers False in silence, which here means a failed run exits 0. |
| 2. Catches what happened | Two detections. VALUE (ported): the literal is some StrEnum's value; the finding names the member. TYPED (new, because a vocabulary cannot see the typo): the compared name or attribute is declared as a StrEnum or `Literal` and the literal is not among its values (`kind == "fial"`). Both read `==`, `!=`, `in`, `not in` and `match`/`case`. |
| 3. Seen to fail | Removing the TYPED branch, the `match` reading, or the open-type narrowing each turns a test red. |
| 4. Fires on nothing adjacent | A subscript (`payload["type"]`) is someone else's JSON and is not read; an expression declared `str` (`person.name == "agent"`) is not `Actor.AGENT`; the file defining an enum may compare its own values. |
| Cannot see | The type of an object. An attribute is judged by its NAME across every class in the tree: `.kind` is `FindingKind` on `Finding` and `Literal["ticket"]` on `TicketSnapshot`, so `finding.kind == "polled"` passes, because some model's `kind` is `Literal["polled"]`. A name is judged only when annotated in its own function. |
| Cost | None for code that uses members. |

### 4. `boundary_dicts` — written, fail-closed

| Test | Answer |
|---|---|
| The rule | No `dict[str, Any]`, `Dict[str, Any]` or bare `dict` in a function signature or a Pydantic field, outside files named `wire.py` under `adapters/providers/`. |
| What is read | Every parameter, `*args`, `**kwargs` and return, and every class-level field of a class whose bases reach `BaseModel`, `RootModel` or `Model` through the tree; nested (`list[dict[str, Any]] \| None`) and string annotations included. |
| Cannot see | A dataclass, `TypedDict` or `Protocol` attribute: only Pydantic fields were agreed. |
| Why the exception | A provider's request and response bodies are someone else's wire format. They enter as text on `Exchange` and leave `wire.py` as a typed `Snapshot`. |
| 1. Recurred | 699 signatures in the parent repo's baseline. |
| Cost | Every provider needs a `wire.py`. |

## Not agreed

### `provider_manifest`

| Test | Answer |
|---|---|
| The rule | Every directory under `adapters/providers/` has a manifest, is registered under `minutehand.providers`, claims at least one host, and no two providers claim the same host. |
| Since then | Built-in providers are found by walking `adapters/providers/`, not by entry point. `Registry` refuses at load a manifest with no `provider.py`, two providers with one key, and overlapping hosts; `Manifest.hosts` requires at least one. Only an installed package's entry point is still a string nothing checks before load. |
| What pyright cannot see | Entry points are strings in `pyproject.toml`; host patterns are strings. |
| 1. Recurred | Two instances of a fake existing in the tree and missing from where it is declared: `github_emulator` is absent from `_build-emulator-images.yml` and every workflow; `youtrack-emulator/Dockerfile` says `EXPOSE 8090` while the service listens on 8091. |
| 2. Catches what happened | Yes for the first (declared set ≠ directory set). The port mismatch disappears with one process. |
| 3. Seen to fail | Not written. |
| Cost | One line in `pyproject.toml` per provider. |

Refused: two instances is thin.

## Not lints

| Need | Why not a lint | What it is instead |
|---|---|---|
| A fake accepts what the real API refuses | Needs the real API, or its published document | Response validation against the OpenAPI document in the provider's tests; a scheduled job against real accounts. Neither is built: each provider's refusal tests are written by hand. |
| A fake's response drifts from the real one | Same | Capture mode: record real traffic, replay against the fake, diff. Not built. The nightly job runs the suite against the newest release of each service's client library. |
| A scenario names a person who does not exist | pyright cannot see YAML, but `Scenario._keys_resolve` already rejects it at load | A validator, already written |
