# Releasing

Minutehand ships as two things built from one commit:

| What | Where | For |
|---|---|---|
| The package | PyPI, `minutehand` | `pip install minutehand`, `uvx minutehand serve`, and the pytest plugin |
| The image | `ghcr.io/alknoma/minutehand` | an agent in any other language: it is driven by proxy variables and the control API, never imported |

`.github/workflows/release.yml` builds and publishes both. It runs when a GitHub Release is **published** and on
nothing else, so a pushed tag on its own publishes nothing.

## The version

`pyproject.toml` writes it, once (`[project] version`). Everything else reads it from there:

- `minutehand --version` and `minutehand.__version__` read the installed package's metadata
  (`importlib.metadata`), so they cannot disagree with what `pip` installed. `tests/test_version.py` holds both to
  `pyproject.toml`.
- The release's tag is `v` and the version: `0.2.0` is released from tag `v0.2.0`. The workflow's first step
  refuses any other tag before anything is built.
- The image is tagged with the version (`ghcr.io/alknoma/minutehand:0.2.0`), the commit
  (`:sha-<40-character commit>`), and `latest` unless the Release is marked a pre-release. An image tag names
  the same commit as the PyPI release of that version, and the image is pushed only after PyPI accepted the
  package, so no image names a version `pip` cannot find.

Versions follow [PEP 440](https://peps.python.org/pep-0440/). A pre-release (`0.2.0rc1`, tag `v0.2.0rc1`) is
published with the Release's "Set as a pre-release" box ticked, which keeps it off `latest`; pip skips it unless
asked with `--pre`.

## Cutting a release

1. On a branch from `integration-main`, set `version` in `pyproject.toml`, run `uv lock` (the lock records the
   project's version), and open the pull request. CI's `build` job installs the wheel and runs the example.
2. After it merges, promote `integration-main` to `main` as usual.
3. On GitHub, draft a Release: tag `v<version>` on `main`'s head, created by the Release itself; notes saying
   what changed for a user. Publish it.
4. `release.yml` runs:
   - **build** — checks the tag against `pyproject.toml`; `uv build` (the sdist, then the wheel built from
     it); `twine check --strict` on both; `pytest -m packaging tests/packaging/test_installed_package.py`, which
     installs the wheel into a clean environment and runs the example, `--version`, and a pytest suite using the
     plugin, and checks the sdist holds only tracked files and builds the identical wheel.
   - **pypi** — uploads exactly the files `build` checked, by Trusted Publishing from the `pypi` environment.
     No API token exists to leak. Add required reviewers to that environment and this job waits for one.
   - **image** — builds the `runtime` target of `Dockerfile` for `linux/amd64` and `linux/arm64` and pushes it.
5. Check: `pip install minutehand==<version>` in a fresh environment and `minutehand --version`;
   `docker run --rm ghcr.io/alknoma/minutehand:<version> --version`.

A failed run publishes nothing after the job that failed. PyPI never accepts the same version twice, so a
release that reached PyPI and then failed is fixed by re-running the `image` job, never by re-publishing; a
broken package is fixed by a new version, and the bad one yanked on PyPI.

## What is set up once, outside this repository

- **PyPI**: a trusted publisher for project `minutehand` (a "pending" publisher before the first upload) naming
  this repository, workflow `release.yml`, and environment `pypi`.
- **GitHub**: an environment named `pypi` on this repository, ideally with required reviewers.
- **GHCR**: the first push creates the package `minutehand` under the organisation, private. Its visibility is
  set by hand in the package's settings; the `org.opencontainers.image.source` label links it to this
  repository.
