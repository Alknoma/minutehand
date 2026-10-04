# Adopting Minutehand in alknoma-cloud

How this repo moves from ten emulator containers and a welded harness to one Minutehand container, without a day where the suites are red. This is a procedure for alknoma-cloud and belongs in a GitHub issue there once agreed.

## What must keep working

Every item below is depended on today.

| Surface | Items |
|---|---|
| Compose service names | `slack_emulator`, `teams_emulator`, `graph_api_emulator`, `google_drive_emulator`, `notion_emulator`, `jira_emulator`, `asana_emulator`, `youtrack_emulator`, `github_emulator`, `firebase_emulators` |
| Ports | 8089 Drive, 8091 YouTrack, 8092 Slack, 8093 Graph, 8094 Teams, 8095 Jira, 8096 Asana, 8097 GitHub, 8098 Notion; `*_EMULATOR_HOST_PORT`; the parallel override `ports: !reset []` |
| Make targets | `emulators`, `emulators-down`, `emulators-parallel`, `dev`, `dev-parallel`, `test-live EMU_SERVICES=` |
| Environment | `USE_EMULATORS`, `{SLACK,TEAMS,GRAPH_API,GOOGLE_DRIVE,YOUTRACK,JIRA,ASANA}_EMULATOR_URL`, `NOTION_API_EMULATOR_URL`, `GITHUB_API_BASE_URL`, `SLACK_SIGNING_SECRET`, `SLACK_EMULATOR_TEAM_ID`, `TEAMS_EMULATOR_{TENANT_ID,TEAM_THREAD_ID,AAD_GROUP_ID,JWT_ISSUER}`, `MICROSOFT_CLIENT_ID/SECRET`, `PLATFORM_GATEWAY_URL` |
| Control routes tests call | every `/api/admin/*`, `/reset`, `/seed/*`, `/_test/*`, `/debug/*`, `/health`, and Teams `/api/admin/mint-token` |
| Seed identities | `U001`–`U009`, `C001GENERAL`, the Asana GIDs (workspace `1100000000000001`, users `12…`, projects `14…`, tasks `17…`), Jira and YouTrack users |
| Events pushed back | Slack → `/api/v1/slack` (HMAC), Teams → `/api/v1/teams/messages` (RS256 JWT), Graph → subscription `notificationUrl`, Notion → registered webhook URLs |
| Pytest fixtures | `*_emulator_url`, `teams_inbound_auth_header`, `youtrack_emulator_eval`, `youtrack_emulator_stress`, `reset_emulator_state` |
| CI | `_build-emulator-images.yml`, the `emulators:` matrix in `live-tests.yml` (21 suites), `agent-evals.yml`, `eval-cron.yml`, images at `ghcr.io/alknoma/emulators/<svc>:<hash>` |
| In-process imports | tests import `docker/{github,google-drive,slack,youtrack}-emulator/main.py` directly; `test_world.py`, `test_slack_install_scopes.py` and `runner.py` read seed files by path |

## Stages

Each stage leaves every suite green and is its own pull request.

### 1. One container, old behaviour

- Minutehand's image serves all nine fakes from one process and also listens on the nine legacy ports.
- `docker-compose.emulators.yml` keeps the ten service names. Nine become network aliases of one `minutehand` service; `firebase_emulators` is unchanged.
- The control routes, seed identities and pushed events are served as they are today.
- CI swaps nine image pulls for one. `github_emulator`, which no workflow builds today, starts being built.
- Nothing in `services/`, `shared/` or `tests/` changes.

### 2. Interception

- Services get `HTTPS_PROXY` and the CA variables in compose.
- Delete the 10 base-URL swaps (`build_slack_client`, `graph_client.py`, `notion_client_factory.py`, `github_client.py`, …) one provider at a time.
- Delete the credential bypasses for Jira, YouTrack, Asana and GitHub once the proxy accepts any token and tests seed `AgentSettings` with a host the proxy claims.
- Delete `EmulatorDriveService` and the four `_fetch_from_emulator` branches once `HTTPLIB2_CA_CERTS` is set. Its failure-shape tests (`test_emulator_service_fails_like_the_real_client.py`) move to Minutehand as Drive provider tests.
- Bot Framework: deletable only when the Teams provider mints tokens under the real issuer and serves JWKS at the intercepted `login.botframework.com`.

Stays regardless: the Firebase Auth-emulator sign-in pages (a browser flow), `notion.py:_register_with_emulator` (real Notion has no registration API), and `firebase_init.py`.

### 3. One clock

- Providers stamp from Minutehand's clock. The 72 machine-clock reads go.
- The harness stops writing `OpenIssue.simulatedNow` itself; the adapter in stage 4 does it from `WakeRequest.now`.

### 4. The harness becomes an adapter

`field_observation/runner.py` (3,121 lines) splits:

| Today, in `runner.py` | Becomes |
|---|---|
| `_scheduler_listener_worker`, clock advance, `_HumanDelayLedger` | Minutehand's orchestrator and `next_jump()` |
| `_generate_substantive_rro_reply`, `_AvailabilityScript` | Minutehand's replier and `Person.absences` |
| `_snapshot_artifacts`, `turn_snapshot.py`, `run_store.py`, `viewer/` | Minutehand's store and viewer |
| 21 `FieldMissionCase` dataclasses in `mission.py` | 21 scenario files |
| Everything that reads or writes `OpenIssues/*`, calls `/api/v1/internal/execute` or `update-state` | `tests/…/minutehand_adapter.py` (size unknown until written) |

The adapter is the only code that knows Ayven. On `WakeRequest` it sets `simulatedNow`, dispatches the due action or the stale wait, and returns `AgentReport(next_wake=min(nextCheck, nextStaleCheck), commitments=[one per active Blocker])`. It lives under `tests/`; nothing is added to `services/`.

Two things change in behaviour and need a decision when this stage is planned:
- Replies arrive as Slack events, through `InboundMessageHandler`, where today they are posted to `/bridge`. That exercises a path field observation has never driven.
- Approvals are decided through the message the owner receives, where today the harness calls `/api/v1/consent/decide`.

### 5. Checks gate CI

- `minutehand run` over the scenarios replaces the manual run plus the two assessor agents for everything a deterministic check can see.
- The `field_observation` marker is removed from the exclusion in `agent-evals.yml` and `eval-cron.yml` once the reference scenarios run inside the job's time limit.

## Local development

`make dev` mounts a sibling checkout of Minutehand over the image when `MINUTEHAND_SRC` is set, so a change to a provider and a change to a service can be tried together before either is merged. The pinned image tag in compose is what CI uses.
