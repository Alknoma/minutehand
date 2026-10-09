# CI

What runs, why it is shaped this way, and what is not switched on yet. The shape comes from
alknoma-cloud's CI: what held up there is kept, and what cost time there is designed out.

## What is switched on

| Workflow | Runs on | Jobs |
|---|---|---|
| `ci.yml` | every pull request, every push to `main` and `integration-main`, the merge queue | `types` (pyright), `style` (`ruff format --check`, `ruff check`), `lints` (`python -m lints`), `workflows` (actionlint, zizmor over workflows and `.github/actions`), `tests` (Linux on Python 3.12 and 3.13, macOS on 3.12), `build` (the package and the image, installed and run), `action` (the setup action by its local path: install, example, image), `promotion`, `gate` |
| `nightly.yml` | 03:17 UTC, and by hand | `repeat` (the suite five times in five orders), `newest-clients` (the suite against the newest release of every dependency) |
| `release.yml` | a GitHub Release being published, and nothing else | `build` (sdist and wheel, `twine check`, the installed-package tests), `pypi` (Trusted Publishing from the `pypi` environment), `image` (`ghcr.io/alknoma/minutehand`, amd64 and arm64). `docs/releasing.md` |

Test settings, in `pyproject.toml`, apply locally and in CI alike:

- **No network beyond this machine** (`--disable-socket --allow-hosts=127.0.0.1,::1,localhost`). A provider test that reaches the real service is a broken test.
- **Random order every run**, and `-n auto` in CI. A test that depends on another running first fails.
- **60 seconds per test.** A hang is a failure, not a stuck job.
- **A warning raised against our own code is an error.** Third-party internals are left alone.

## Kept from alknoma-cloud

| Practice | Why it held up |
|---|---|
| One summary job that runs `always()` and is the only required check | A skipped gate reports nothing, and a branch rule cannot tell that from a run still in progress |
| Lints discovered from a directory and run as one step | `post_response_work` sat in the directory for three weeks gating nothing while each lint was its own named step |
| Every job has a timeout and a name that says what failed | A red run names its own cause |
| A test is shown to fail before it is trusted | A green suite over a dead code path still passes |
| Merge commits only, never squash | The commits are the record of what was tried |

## Designed out

| What cost time in alknoma-cloud | Evidence | What this repo does instead |
|---|---|---|
| 4,544 lines of workflow across 13 files, 408 of them deciding what to skip | Three job conditions named events their workflow never triggered on; a lint had to be written to catch the class | No change detection. The whole suite takes under a minute, so everything runs every time. |
| A workflow with a path filter that fed a required check | "A workflow that never starts never reports, and that pull request waits forever" (`ci-tests.yml`) | No path filters. `gate` always reports. |
| The working branch had no required check | `integration-main-protection` requires a pull request and nothing else; "never merge a PR whose CI is not green" was a rule for people to remember | `gate` is required on every protected branch (see "Repository settings") |
| Required checks named individual jobs | Renaming a job blocked every pull request to that branch | Only `gate` is required. Jobs behind it can be renamed, split or added freely. |
| Two branches that were each green and red together | In this repo on 2026-10-04, three branches that each passed alone produced three failures when combined | A merge queue: `gate` runs on the result of the merge, before it lands |
| The same list of services written in four workflows | `github_emulator` was missing from all four and was never built | Nothing is listed. Providers are discovered from the tree and tested by one `pytest` run. |
| A model-dependent suite as a required check | Agent Behavioral Evals: 5 of the last 8 runs failed, slowest 471 minutes | Nothing that calls a model is required. Model-dependent runs are a separate, maintainer-triggered tier. |
| Pull-request CI that needs secrets | Live tests and evals authenticate to cloud services, so a fork's pull request could never run them | Every required job runs with no secret and read-only permissions. A fork gets the same checks as a maintainer. |
| A superseded push keeps running | Only three of thirteen workflows set `concurrency` | A new push to a pull request cancels the run it replaces |
| Actions pinned to a moving tag (`@v4`) | 24 uses of `actions/checkout@v4` | Every action is pinned to a commit; Dependabot proposes the bumps |
| A workflow triggered by comments with write access (`agent-summon`) | Safe among colleagues; with outside contributors it runs on text a stranger wrote | None. No `pull_request_target`, no `issue_comment` trigger. `zizmor` fails the build if one appears. |
| Three branches to promote through | `integration-main` → `staging-main` → `main`, each with its own tier | Two: work lands on `integration-main`, and `main` takes pull requests from `integration-main` only. There is no deployment to stage, so no third. |
| The promotion check lived in a workflow that never fired for it | `source-branch-guard` sat in a workflow triggered for `integration-main` while acting only on `main`; it never ran, and two pull requests went straight into `main` | `promotion` is a job in the one workflow, has no `if`, runs on every event, and sits behind `gate` |

## Minutehand in another repository's CI

`.github/actions/setup-minutehand` is a composite action that installs Minutehand from the action's own
files: the runner fetches the action at the commit the caller names, and that copy of this repository is what is
installed (`$GITHUB_ACTION_PATH/../../..`). No package index, no image registry, no token and no secret.

```yaml
jobs:
  live:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>                 # the caller's own repository; Minutehand's is never checked out
      - run: python3 -m venv "$RUNNER_TEMP/venv"
      - id: minutehand
        uses: <org>/minutehand/.github/actions/setup-minutehand@<full commit sha>
        with:
          python: ${{ runner.temp }}/venv/bin/python  # default: `python` on PATH
          installer: auto                              # uv when it is on PATH, else pip; or `uv`, `pip`
          image-tag: minutehand:ci                     # optional: build the image and load it into Docker
      - run: echo "minutehand ${{ steps.minutehand.outputs.version }} at ${{ steps.minutehand.outputs.commit }}"
```

| Output | What |
|---|---|
| `version` | The installed package's version |
| `commit` | The commit installed: the SHA the action was pinned at (or, used by a local path, the checkout's `HEAD`); a reference that is not a full SHA is refused |

Pin it by full commit SHA, as every action here is pinned; a tag or branch is refused, since nothing could then
say which Minutehand a run used. With `image-tag`, the image is built from the same files with `docker build` on
the runner and is in the job's Docker under that tag (`docker compose` and `docker run` find it); nothing is pushed.

**The one repository setting it relies on.** This repository is internal, and a workflow in another repository
can use an action from it only when this repository's *Settings → Actions → General → Access* is set to
"Accessible from repositories in the organization". Nothing here changes it; whoever administers the repository
sets it once. Without it the caller's run fails at "Set up job" with the action not found.

**Running a folder of scenarios.** Once Minutehand is installed (from PyPI, `pip install minutehand`, or with the
action), one step plays every scenario of a folder in parallel and fails the job on any surprise:

```yaml
      - run: minutehand run-all minutehand/scenarios --agent minutehand/agent.yaml --jobs 4 -- python agent.py --port {run.port}
```

Each scenario gets a port of its own and a folder of its own (`{run.port}`, `{run.dir}` in the agent file and the
command; `MINUTEHAND_RUN_PORT`, `MINUTEHAND_RUN_DIR` in the agent's environment). `minutehand run` fills them the
same way, once, when it starts the agent's command (the folder is under `<state>/run-all/run/`); with no command to
start, and in `env` and `doctor`, a file holding either is refused by name. `run-all` hands each of its runs
`--record-model-calls`, `--model-host`, `--capture-unknown`, `--upstream-ca`, `--proxy-host`, `--agent-proxy-host`,
`--no-proxy` and `--no-receive-telemetry` as given; each run takes free ports of its own. Each says the verdict it is written
to reach (`expect_outcome: passed | failed | unfinished | not_judged`, default `passed`): a scenario whose point is
that nobody answers may expect `unfinished`, and one that shows a known bug `failed`. `run-all` prints a line per
scenario and a timeline per person, and exits 1 when any verdict differs from its scenario's, 2 when any run could
not be performed, 0 otherwise. `--json` prints the same as data.

**What a single run's exit code says.** `minutehand run` exits with its verdict: 0 passed, 1 a rule, an expectation
or a check of the agent's own failed, 3 not finished, 4 Minutehand itself failed, 5 not judged, 6 simulation
incomplete (the simulated world did not play as its files declare; the agent's verdict over what did happen is in the
words). Every run is
assessed against the world its files declare (`docs/assessments.md`), so a job gates on the scenario it plays with no
rule written; rules add the team's own policy.

The `action` job in `ci.yml` uses the action by its local path on a runner with nothing set up but the checkout,
and runs the installed `minutehand --help`, `examples/follow_up` and the built image.

## Tiers

| Tier | When | Required | State |
|---|---|---|---|
| **Gate**: types, lints, workflow lint, the whole test suite on three runners | Every pull request and merge-queue entry | Yes | On |
| **Build**: `uv build` makes the sdist and the wheel; the wheel, installed into a fresh environment with none of the dev dependencies, runs `minutehand --help` and both `examples/follow_up` scenarios (exit 0, and exit 1 with the scenario's rule `follows_up_when_due` and its pattern); the image is built (not pushed) and runs both scenarios with the example agent started inside it. `tests/packaging`, marked `packaging` and left out of the default run | Every pull request | Yes, behind `gate` | On: the `build` job |
| **Nightly**: repeat runs, newest dependencies | Nightly | No. A failure is a signal about tomorrow, not about the pull request in front of you. | On |
| **Conformance**: each provider's answers validated against the service's published API description; recorded real traffic replayed against the fake | Nightly | No | Off. Not built. |
| **Mutation**: a mutation tester over `domain/`, the store and the checks, reporting what survives | Weekly | No, reported as a trend | Off. Not built. Today "seen to fail" is a claim in the pull request, checked by the reviewer. |
| **Model**: model-written people and judged checks | By a maintainer, from a protected environment that holds the key | No | Off. The features are not built. |
| **Release**: build once on a version tag, attest, publish to PyPI by trusted publishing and to the container registry | A tag `v*`, approved by a maintainer | n/a | Off. Publishing is a decision not yet made. |
| **Dependency review, code scanning, secret scanning** | Every pull request | Yes | Off. Free when the repo is public; needs a licence while it is internal. |

## Flaky tests

- No automatic retry. A test that passes on the second try has a defect, in the test or in the code.
- `nightly / repeat` exists to find them before a contributor does.
- A test may be quarantined only with a marker that names an open issue; `--strict-markers` rejects an unknown marker.

## Branches and who may merge

Applied on 2026-10-04 as the ruleset "integration-main and main: pull request and gate".

| Rule | `integration-main` | `main` |
|---|---|---|
| Changes arrive by pull request; a direct push is rejected | Yes | Yes |
| `gate` must pass, on a branch that is up to date with its target | Yes | Yes |
| Where a pull request may come from | Any branch or fork | `integration-main` only (the `promotion` job) |
| Merge method | Merge commit | Merge commit |
| Force push, deletion | Refused | Refused |

- **Anyone who can read the repo can open a pull request. Only people with write access can merge one**, and write access is held by Alknoma's members alone. Someone outside Alknoma who needs to label or triage gets the triage role, which cannot merge.
- A ruleset that restricted updates to a list of maintainers was tried and removed: it turned every merge into a "bypass" merge.
- Approvals required: none yet. Every pull request so far has one author, who cannot approve their own. With a second maintainer this becomes one approval, and a code owner's for the paths in `CODEOWNERS`.
- Squash and rebase merging are switched off for the repository.
- Seen to hold: a direct push to each branch was rejected; a pull request into `main` from another branch failed `promotion` and was blocked; a pull request whose merged result broke a lint was blocked until fixed.

Not applied: the merge queue, deleting head branches after merge, approval of workflow runs from first-time contributors.

## Contributions and the licence

The project is under the Functional Source License. For Alknoma to keep offering it commercially,
and to convert each release to Apache-2.0 after two years, a contribution has to arrive with the
right to do both. That needs a contributor licence agreement or an equivalent statement in
`CONTRIBUTING.md`, checked on every pull request. It is not written: it is a legal text.
