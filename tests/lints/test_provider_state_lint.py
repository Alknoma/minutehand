"""The lint is only a check if it is seen to fail: each test plants the state it must find beside what it must not."""

import shutil
from pathlib import Path

from conftest import SRC, Plant

from lints import provider_state

P = "minutehand/adapters/providers"

MANIFEST = 'from minutehand.domain.provider import Manifest\nMANIFEST = Manifest(key="{key}", hosts=["x"]{extra})\n'

PROVIDER = """
from .app import build_app


class FakeProvider:
    def app(self, world, clock):
        return build_app(world, clock)


def build():
    return FakeProvider()
"""


def provider(key: str, app: str, *, extra: str = "", files: dict[str, str] | None = None) -> dict[str, str]:
    planted = {
        f"{P}/{key}/__init__.py": "",
        f"{P}/{key}/manifest.py": MANIFEST.format(key=key, extra=extra),
        f"{P}/{key}/provider.py": PROVIDER,
        f"{P}/{key}/app.py": app,
    }
    planted.update({f"{P}/{key}/{name}": text for name, text in (files or {}).items()})
    return planted


def found(root: Path) -> list[tuple[str, int | None]]:
    return [(f.file.removeprefix(f"{P}/"), f.line) for f in provider_state.run(root)]


def test_a_module_level_cache_changed_in_a_handler_is_flagged(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            _cache: dict[str, str] = {}


            async def handler(request):
                _cache[request.key] = request.body
                return _cache[request.key]


            def build_app(world, clock):
                return handler
            """,
        )
    )
    [finding] = provider_state.run(root)
    assert (finding.file, finding.line) == (f"{P}/fake/app.py", 5)
    assert "module-level _cache" in finding.message and "fake/app.py:1" in finding.message


def test_module_state_is_found_however_it_is_reached(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            from . import state
            from .state import SEEN, TOTAL

            def a(x):
                SEEN.add(x)

            def b(x):
                state.SEEN.discard(x)

            def c(x):
                state.TOTAL = state.TOTAL + x

            def d(x):
                global TOTAL
                TOTAL += x

            def e(x):
                state.REGISTRY.by_key[x] = x

            def build_app(world, clock):
                return a
            """,
            files={"state.py": "SEEN = set()\nTOTAL = 0\nREGISTRY = Registry()\n"},
        )
    )
    assert found(root) == [
        ("fake/app.py", 5),
        ("fake/app.py", 8),
        ("fake/app.py", 11),
        ("fake/app.py", 14),
        ("fake/app.py", 18),
    ]


def test_constants_tables_import_time_building_and_locals_are_left_alone(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            import re
            from typing import Final

            KINDS = ("a", "b")
            NAMES = frozenset({"a"})
            STATUS = {"ok": 200}
            PATTERN = re.compile("x")
            LABEL: Final = "x"
            BUILT: dict[str, int] = {}
            BUILT["at-import"] = 1
            for kind in KINDS:
                BUILT[kind] = 2


            async def handler(request):
                found = STATUS[request.key]
                seen: dict[str, int] = {}
                seen[request.key] = found
                STATUS2 = []
                STATUS2.append(PATTERN.match(LABEL))
                return seen, sorted(NAMES)


            def shadow(STATUS):
                STATUS["x"] = 1


            def build_app(world, clock):
                routes = []
                routes.append(("/x", handler))
                return routes
            """,
        )
    )
    assert found(root) == []


def test_an_instance_cache_on_the_app_object_is_flagged(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            class FakeApi:
                def __init__(self, world, clock):
                    self._world = world
                    self._users: dict[str, str] = {}
                    self._calls = 0

                async def user(self, request):
                    self._calls += 1
                    if request.id not in self._users:
                        self._users[request.id] = self._world.read(request.id)
                    return self._users[request.id]


            def build_app(world, clock):
                return FakeApi(world, clock)
            """,
        )
    )
    messages = [f.message for f in provider_state.run(root)]
    assert found(root) == [("fake/app.py", 8), ("fake/app.py", 10)]
    assert messages[0].startswith("FakeApi.user rebinds self._calls outside __init__")
    assert messages[1].startswith("FakeApi.user changes self._users outside __init__")


def test_state_on_the_provider_object_itself_is_flagged(plant: Plant) -> None:
    files = provider("fake", "def build_app(world, clock):\n    return None\n")
    files[f"{P}/fake/provider.py"] = """
        from .app import build_app


        class FakeProvider:
            def __init__(self):
                self._tickets = []

            def app(self, world, clock):
                return build_app(world, clock)

            def transition(self, ticket, to, world, clock):
                self._tickets.append(ticket)


        def build():
            return FakeProvider()
        """
    assert found(plant(files)) == [("fake/provider.py", 12)]


def test_a_per_request_object_and_a_table_built_once_are_left_alone(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            class View:
                def __init__(self, world):
                    self.world = world
                    self._users = {}

                def user(self, gid):
                    if gid not in self._users:
                        self._users[gid] = self.world.read(gid)
                    return self._users[gid]


            class FakeApi:
                def __init__(self, world, clock):
                    self._world = world
                    self._routes = []
                    self._methods = {"users.list": self.users}

                def route(self, path, handler):
                    self._routes.append((path, handler))

                def users(self, request):
                    return View(self._world).user(request.id)


            def build_app(world, clock):
                api = FakeApi(world, clock)
                api.route("/users", api.users)
                return api
            """,
        )
    )
    assert found(root) == []


def test_a_method_that_builds_the_table_and_also_serves_is_flagged(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            class FakeApi:
                def __init__(self, world, clock):
                    self._routes = []

                def route(self, path, handler):
                    self._routes.append((path, handler))

                async def hook(self, request):
                    self.route(request.path, None)


            def build_app(world, clock):
                api = FakeApi(world, clock)
                api.route("/users", None)
                return api
            """,
        )
    )
    assert found(root) == [("fake/app.py", 6)]


def test_in_flight_delivery_tasks_are_exempt_and_nothing_else_on_the_same_object_is(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            import asyncio


            class FakeApp:
                def __init__(self, world, clock):
                    self._sending: set[asyncio.Task[None]] = set()
                    self._delivered: set[str] = set()

                def delivering(self):
                    return len(self._sending)

                async def __call__(self, scope, receive, send):
                    task = asyncio.create_task(self._deliver())
                    self._sending.add(task)
                    task.add_done_callback(self._sending.discard)
                    self._delivered.add(scope["path"])


            def build_app(world, clock):
                return FakeApp(world, clock)
            """,
        )
    )
    assert found(root) == [("fake/app.py", 16)]


def test_a_closure_over_a_build_time_local_is_flagged(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            def build_app(world, clock):
                seen = {}
                count = 0
                table = [("/x", None)]

                async def handler(request):
                    nonlocal count
                    count += 1
                    seen[request.key] = request.body
                    return table[0]

                return handler
            """,
        )
    )
    assert found(root) == [("fake/app.py", 7), ("fake/app.py", 9)]


def test_a_class_attribute_shared_by_every_instance_is_flagged(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            class View:
                seen = {}
                kinds = ("a", "b")

                def __init__(self, world):
                    self.world = world

                def read(self, key):
                    self.seen[key] = self.world.read(key)
                    View.seen.clear()
                    return self.kinds


            class Own:
                cache = {}

                def __init__(self):
                    self.cache = {}

                def read(self, key):
                    self.cache[key] = key


            def build_app(world, clock):
                return None
            """,
        )
    )
    assert found(root) == [("fake/app.py", 9), ("fake/app.py", 10)]


def test_a_manifest_declaring_state_outside_the_log_exempts_its_provider_and_only_it(plant: Plant) -> None:
    app = "_cache = {}\n\ndef handler(request):\n    _cache[request.key] = 1\n\ndef build_app(world, clock):\n    return handler\n"
    root = plant(
        {
            **provider("declared", app, extra=', state_outside_log="moto\'s memory of the process"'),
            **provider("none_declared", app, extra=", state_outside_log=None"),
            **provider("aws", app),
        }
    )
    assert found(root) == [("aws/app.py", 4), ("none_declared/app.py", 4)]


def test_an_exemption_needs_a_reason(plant: Plant) -> None:
    root = plant(
        provider(
            "fake",
            """
            _a = {}
            _b = {}

            def handler(request):
                _a[request.key] = 1  # state-lint: exempt
                _b[request.key] = 1  # state-lint: exempt a reason someone can argue with
            """,
        )
    )
    assert found(root) == [("fake/app.py", 5)]


def test_code_outside_a_provider_is_not_read(plant: Plant) -> None:
    root = plant({"minutehand/application/cache.py": "_cache = {}\n\ndef f(k):\n    _cache[k] = 1\n"})
    assert found(root) == []


def test_a_cache_added_to_a_real_provider_is_flagged(tmp_path: Path) -> None:
    """The mutation from the report, on a copy of a real provider: a module cache and an instance cache on the app."""
    shutil.copytree(SRC / P, tmp_path / P, ignore=shutil.ignore_patterns("__pycache__"))
    assert provider_state.run(tmp_path) == []
    app = tmp_path / P / "github" / "app.py"
    text = app.read_text(encoding="utf-8")
    handler = "    async def user(self, request: Request, caller: Caller) -> Answered:\n"
    assert handler in text and "class GitHubApi:\n" in text
    text = text.replace(
        handler,
        handler + "        _cache[caller.login] = caller.login\n        self._seen[caller.login] = caller.login\n",
    )
    text = text.replace("class GitHubApi:\n", "_cache: dict[str, str] = {}\n\n\nclass GitHubApi:\n")
    app.write_text(text, encoding="utf-8")
    messages = sorted(f.message.split(";")[0] for f in provider_state.run(tmp_path))
    assert len(messages) == 2, messages
    assert messages[0].startswith("GitHubApi.user changes self._seen outside __init__")
    assert messages[1].startswith("module-level _cache")


def test_the_package_itself_is_clean() -> None:
    assert provider_state.run(SRC) == []
