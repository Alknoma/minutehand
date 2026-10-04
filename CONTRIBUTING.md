# Contributing

Read `CLAUDE.md` first. It is the house rules, one sentence each, and CI enforces most of them.

## Before you open a pull request

```bash
uv sync
uv run pytest -q -n auto      # random order, no network beyond this machine
uv run pyright
uv run python -m lints
```

All three pass on every commit you push. `docs/ci.md` says what CI runs and why.

## What a pull request must show

- **The problem first.** What is broken or missing without this change.
- **That your test can fail.** Break the code your test covers, watch the test go red, and say which change you made. A test that has never failed is not known to check anything.
- **Contracts.** A change under `src/minutehand/domain/` or `src/minutehand/ports/` is a change every part depends on. Put it in its own commit and update every caller in the same pull request. No aliases, no compatibility wrappers.

## Adding a provider

Copy the layout of `src/minutehand/adapters/providers/slack/`:

| File | Holds |
|---|---|
| `manifest.py` | `MANIFEST`: the key, the hosts, the path prefix. Data only; it imports nothing from the provider. |
| `wire.py` | The only place the service's own JSON is parsed or built, as typed models. |
| `state.py` | The only path to the store: what each thing is called, what it is listed under. |
| `app.py` | The API, as an ASGI application. |
| `seed.py` | The scenario's people and tickets, written as the scenario. |
| `provider.py` | `build()`. |

Rules a provider cannot bend:

- Everything it knows is in the store. No module-level state, no files, nothing held between requests.
- Every timestamp comes from the clock it is given. `lints/wall_clock.py` fails a read of the machine's clock.
- It refuses what the real service refuses, with the real status and the real error shape, and a test asserts each refusal.
- Its tests drive it with the service's own client library, never only with hand-built requests.
- Nothing is registered anywhere. A provider is found because its directory exists.

## Reporting a difference from the real service

Use the "A fake behaves differently" issue form: the request, what the real service answered, what the fake answered.
