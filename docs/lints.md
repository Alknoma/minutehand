# Lints proposed for Minutehand

None of these is adopted. `alknoma-cloud/.claude/rules/lints.md` requires a lint to be agreed out loud before it is written; this page is the proposal. Each row states the five tests.

A new repo has no debt, so every lint here is fail-closed with an inline marker that requires a reason. There are no baselines.

## Proposed

### 1. `wall_clock` — written, runs today

| Test | Answer |
|---|---|
| The rule | A fake never reads the machine's clock; every timestamp comes from the run's clock. |
| What pyright cannot see | `datetime.now()` type-checks. Which clock a timestamp came from is not in its type. |
| 1. Recurred | 72 reads across the 9 existing emulators: Slack 37, Asana 11, Teams 10, Jira 6, YouTrack 3, Graph 2, Drive 1, GitHub 1, Notion 1. |
| 2. Catches what happened | Run `f431fc97f427`: a ticket filed on the mission's 30 August shows as created 24 August. The Slack `ts` at `docker/slack-emulator/main.py:1361` is one of the 72. |
| 3. Seen to fail | `python lints/wall_clock.py research-services/docker` → 72 findings, exit 1. On `src/` → 0. |
| 4. Fires on nothing adjacent | Matches `time.time`, `time.time_ns`, `datetime.now/utcnow/today`, `date.today` by call. `time.monotonic` and `time.perf_counter` (durations) are untouched. |
| Escape | `# clock-lint: exempt <reason>` on the line. `wall_time` on `WorldEvent` is the one legitimate reader. |
| Cost | One marker at each place wall time is truly wanted. |

### 2. `import_boundaries` — port from alknoma-cloud

| Test | Answer |
|---|---|
| The rule | `domain/` and `application/` never import `adapters/`; a provider never imports another provider. |
| 1. Recurred | The parent repo's lint exists because it did. Not yet in this repo. |
| 2–4 | Carried by the parent's implementation and its mutation test. |
| Cost | None until someone crosses the boundary. |

### 3. `enum_string_comparisons` — port from alknoma-cloud

| Test | Answer |
|---|---|
| The rule | Compare against an enum member, never its value spelled out. |
| Why here | Every kind in `domain/` is a `StrEnum` (`FindingKind`, `Operation`, `Actor`, `WaitingOn`, …). `finding.kind == "fail"` type-checks and a typo answers False in silence, which here means a failed run exits 0. |
| Cost | None for code that uses members. |

### 4. `provider_manifest` — new

| Test | Answer |
|---|---|
| The rule | Every directory under `adapters/providers/` has a manifest, is registered under `minutehand.providers`, claims at least one host, and no two providers claim the same host. |
| What pyright cannot see | Entry points are strings in `pyproject.toml`; host patterns are strings. |
| 1. Recurred | Two instances of a fake existing in the tree and missing from where it is declared: `github_emulator` is absent from `_build-emulator-images.yml` and every workflow; `youtrack-emulator/Dockerfile` says `EXPOSE 8090` while the service listens on 8091. |
| 2. Catches what happened | Yes for the first (declared set ≠ directory set). The port mismatch disappears with one process. |
| 3. Seen to fail | Not yet written. |
| Cost | One line in `pyproject.toml` per provider. |

Two instances is thin. This is the one to refuse if any.

### 5. `boundary_dicts` — port, fail-closed

| Test | Answer |
|---|---|
| The rule | No `dict[str, Any]` in a signature outside `adapters/providers/*/wire.py`. |
| Why the exception | A provider's request and response bodies are someone else's wire format. They enter as text on `Exchange` and leave `wire.py` as a typed `Snapshot`. |
| 1. Recurred | 699 signatures in the parent repo's baseline. |
| Cost | Every provider needs a `wire.py`. |

## Not lints

| Need | Why not a lint | What it is instead |
|---|---|---|
| A fake accepts what the real API refuses | Needs the real API, or its published document | Response validation against the OpenAPI document in the provider's tests; a scheduled job against real accounts |
| A fake's response drifts from the real one | Same | Capture mode: record real traffic, replay against the fake, diff |
| A scenario names a person who does not exist | pyright cannot see YAML, but `Scenario._keys_resolve` already rejects it at load | A validator, already written |
