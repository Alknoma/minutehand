"""An untyped dict at a boundary is seen to fail; a typed one and a local one are not."""

from conftest import SRC, Plant

from lints import boundary_dicts

MODELS = """
    from typing import Any

    from pydantic import BaseModel, ConfigDict


    class Model(BaseModel):
        model_config = ConfigDict(frozen=True)


    class Base(Model):
        pass
"""


def test_signatures_and_pydantic_fields_carrying_an_untyped_dict_are_flagged(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/domain/scenario.py": MODELS,
            "minutehand/domain/bad.py": """
                from typing import Any, Dict

                from minutehand.domain.scenario import Base


                def a(payload: dict[str, Any]) -> None: ...
                def b() -> Dict[str, Any]: ...
                def c(payload: dict) -> None: ...
                def d(rows: list[dict[str, Any]] | None) -> None: ...
                async def e(**extra: dict) -> None: ...
                def f(payload: "dict[str, Any]") -> None: ...


                class Event(Base):
                    body: dict[str, Any]
            """,
        }
    )
    found = boundary_dicts.run(root)
    assert [(f.file, f.line) for f in found] == [("minutehand/domain/bad.py", n) for n in (6, 7, 8, 9, 10, 11, 15)]
    assert "parameter `payload` of a()" in found[0].message
    assert "field `Event.body`" in found[-1].message


def test_typed_dicts_locals_and_non_model_classes_are_left_alone(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/domain/scenario.py": MODELS,
            "minutehand/checks/ok.py": """
                from dataclasses import dataclass
                from typing import Any


                def a(email: dict[str, str]) -> dict[str, int]:
                    scratch: dict[str, Any] = {}
                    return {k: len(v) for k, v in dict.fromkeys(email, "").items()}


                @dataclass
                class Local:
                    scratch: dict[str, int]
            """,
        }
    )
    assert boundary_dicts.run(root) == []


def test_a_providers_wire_file_is_the_one_exception(plant: Plant) -> None:
    signature = "from typing import Any\n\ndef parse(body: dict[str, Any]) -> None: ...\n"
    root = plant(
        {
            "minutehand/adapters/providers/slack/wire.py": signature,
            "minutehand/adapters/providers/slack/api.py": signature,
            "minutehand/domain/wire.py": signature,
        }
    )
    assert sorted(f.file for f in boundary_dicts.run(root)) == [
        "minutehand/adapters/providers/slack/api.py",
        "minutehand/domain/wire.py",
    ]


def test_an_exemption_needs_a_reason(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/bare.py": "from typing import Any\n\ndef a(p: dict[str, Any]) -> None: ...  # dict-lint: exempt\n",
            "minutehand/argued.py": (
                "from typing import Any\n\n"
                "def a(p: dict[str, Any]) -> None: ...  # dict-lint: exempt the SDK's callback signature\n"
            ),
        }
    )
    assert [f.file for f in boundary_dicts.run(root)] == ["minutehand/bare.py"]


def test_the_package_itself_is_clean() -> None:
    assert boundary_dicts.run(SRC) == []
