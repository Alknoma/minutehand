# CLAUDE.md — Minutehand

Each norm is one sentence. `docs/design.md` is the design; read it in full before changing a part of the system.

## Architecture

- `domain/` and `ports/` are the contract every part is written against: they hold no I/O and import nothing from `application/` or `adapters/`, and `application/` never imports `adapters/`.
- A provider never imports another provider, reads and writes the world only through `ports.store.Store`, and takes every timestamp from `ports.clock.Clock`.
- The world is an append-only log: nothing is updated in place, and an entity's state is its latest version at the run's head.
- A provider that cannot do something its port asks for means the port is wrong — split the port (`Provider` / `PushesEvents` / `BooksWakes`) or raise loudly, never `return []`.
- Before writing any state, name where it lives when the process is gone; in this repo the answer is the run's SQLite file or it is not state.

## Types

- Every model extends `domain.scenario.Model` (frozen, unknown fields rejected), kinds are `StrEnum` members, and unions are discriminated on a `kind` literal.
- Compare against an enum member, never its value spelled out.
- No `dict[str, Any]`, `hasattr()` or `dict.get()` on anything crossing a boundary; a provider's own JSON crosses as text (`Change.body`, `Exchange.request_body`) and is parsed only inside that provider.
- The field names `data`, `info`, `details`, `params`, `options`, `config` are forbidden.
- Never decide what something is by matching on one of our own identifiers or labels; matching on someone else's wire format or on natural-language text is fine.

## Implementation

- No TODO comments, placeholders, mock implementations or stubs.
- Every behaviour of a fake is its vendor's, documented (`CLAIMS.md` cites the page) or observed of the real service (`CLAIMS.md` cites the recorded evidence), and none is invented or chosen because it seems better or is convenient.
- A fake is scoped from the vendor's API surface, not from guesses about what an agent will call, and refuses by name, never approximates, every method and parameter it does not serve yet.
- A fake keeps what the agent writes verbatim and returns it verbatim, generating only what the real API itself assigns (ids, etags, timestamps, `kind`, links it computes).
- Search for an existing pattern before adding a file or class, and change existing code rather than building beside it.
- Breaking changes are good: no aliases, shims or compatibility wrappers — update every caller.
- A check is deterministic unless it cannot be; anything that needs judgement answers `FindingKind.REVIEW`, and a check that could not read its input says so in `CheckReport.blocked`.
- Never read the machine's clock in `src/` except to fill `wall_time` (enforced by `lints/wall_clock.py`; the only way past is `# clock-lint: exempt <reason>` on the line).
- Nothing in this repo names Ayven, Alknoma, or a real person, project or credential outside `docs/adopting-in-alknoma-cloud.md`.

## Tests

- Never assert a test works because it passes; show it fails when the thing it tests is removed, and say in the final report which mutation you tried.
- No bare `MagicMock()` for one of our own types: use the real `SqliteStore` and `RunClock`, which are fast, before any double.
- A test that deliberately triggers a refusal says so in its name (`test_..._is_refused`, `test_..._rejects_...`).
- `uv run pytest -q`, `uv run pyright`, `uv run python -m lints`, `uv run ruff format --check .` and `uv run ruff check .` all pass before a commit.

## Git

- Branch from the latest `integration-main` with a `feat/`, `fix/`, `refactor/` or `chore/` prefix; never push to `main` or `integration-main`, never squash-merge, never rewrite a pushed branch.
- A lint is proposed and agreed before it is written (`docs/lints.md`).

## Reporting

- Lead with what is broken or unproven, give the evidence, and stop; no list of what you did not do unless it is a fact about the system.
