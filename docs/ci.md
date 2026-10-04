# CI

What runs, why it is shaped this way, and what is not switched on yet. The shape comes from
alknoma-cloud's CI: what held up there is kept, and what cost time there is designed out.

## What is switched on

| Workflow | Runs on | Jobs |
|---|---|---|
| `ci.yml` | every pull request, every push to `main` and `integration-main`, the merge queue | `types` (pyright), `lints` (`python -m lints`), `workflows` (actionlint, zizmor), `tests` (Linux on Python 3.12 and 3.13, macOS on 3.12), `gate` |
| `nightly.yml` | 03:17 UTC, and by hand | `repeat` (the suite five times in five orders), `newest-clients` (the suite against the newest release of every dependency) |

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
| Three branches to promote through | `integration-main` → `staging-main` → `main`, each with its own tier | One branch and release tags. There is no deployment to stage. |

## Tiers

| Tier | When | Required | State |
|---|---|---|---|
| **Gate**: types, lints, workflow lint, the whole test suite on three runners | Every pull request and merge-queue entry | Yes | On |
| **Format and style**: `ruff check`, `ruff format --check` | Every pull request | Yes, behind `gate` | Off. 80 findings and 72 unformatted files today; switching it on needs one mechanical commit, made when no branch is in flight. |
| **Build**: build the wheel, install it into a clean environment, run a scenario through the installed command; build the container image and run the same scenario through it | Every pull request | Yes, behind `gate` | Off. Needs the command line, which is on `feat/end-to-end`. |
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

## Repository settings

These are settings on GitHub, not files, and none is applied yet.

| Setting | Value |
|---|---|
| Branch rule on `main` (and `integration-main` while it exists) | Pull request required; `gate` required; branch must be up to date or come through the merge queue; no force push; no deletion |
| Reviews | One approval; a code owner's approval for `domain/`, `ports/`, `lints/`, `.github/`, `pyproject.toml`, `LICENSE.md` |
| Merge methods | Merge commit only |
| Merge queue | On, method merge commit |
| Workflow permissions | Read-only token by default; workflows from first-time contributors need approval to run |
| Head branches | Deleted after merge |

## Contributions and the licence

The project is under the Functional Source License. For Alknoma to keep offering it commercially,
and to convert each release to Apache-2.0 after two years, a contribution has to arrive with the
right to do both. That needs a contributor licence agreement or an equivalent statement in
`CONTRIBUTING.md`, checked on every pull request. It is not written: it is a legal text.
