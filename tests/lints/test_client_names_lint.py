"""The lint is only a check if it is seen to fail. A planted name here is a made-up five-letter word with its own
hash, so these tests name no client either."""

from pathlib import Path

from conftest import SRC, Plant

from lints import client_names

PLANTED = frozenset({client_names.hashed("quxel")})


def test_it_flags_a_name_in_any_text_file_in_any_case(plant: Plant) -> None:
    root = plant(
        {
            "docs/design.md": "# Design\n\nThe goal: Quxel signs three partners.\n",
            "tests/data/world.json": '{"goal": "QUXEL has three"}\n',
            "src/pkg/mod.py": '"""Tuned on quxel\'s export."""\n',
            "schemas/a.yaml": "protected_names: [Quxel]\n",
        }
    )
    assert sorted((f.file, f.line) for f in client_names.run(root, PLANTED)) == [
        ("docs/design.md", 3),
        ("schemas/a.yaml", 1),
        ("src/pkg/mod.py", 1),
        ("tests/data/world.json", 1),
    ]


def test_a_name_in_the_adoption_page_or_the_rule_is_allowed(plant: Plant) -> None:
    root = plant(
        {
            "docs/adopting-in-alknoma-cloud.md": "Quxel adopts Minutehand.\n",
            "CLAUDE.md": "- Nothing names Quxel outside the adoption page.\n",
        }
    )
    assert client_names.run(root, PLANTED) == []


def test_a_word_that_only_resembles_a_name_is_left_alone(plant: Plant) -> None:
    """Another word of the same length, the name inside a longer word, and a possessive's neighbour."""
    root = plant({"notes.md": "Quxes and Quxelia met. quxel_ish is one word to Python, two here: quxel\n"})
    assert [(f.file, f.line) for f in client_names.run(root, PLANTED)] == [("notes.md", 1)]
    root = plant({"notes.md": "Quxes and Quxelia met.\n"})
    assert client_names.run(root, PLANTED) == []


def test_environments_caches_and_other_checkouts_are_not_read(plant: Plant) -> None:
    root = plant(
        {
            ".venv/lib/site.py": "quxel\n",
            ".git/COMMIT_EDITMSG": "quxel\n",
            ".claude/notes.md": "quxel\n",
            ".worktrees/other/README.md": "quxel\n",
            "pkg/__pycache__/a.txt": "quxel\n",
        }
    )
    assert client_names.run(root, PLANTED) == []


def test_a_file_that_is_not_text_is_skipped(plant: Plant) -> None:
    root = plant({"ok.md": "fine\n"})
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe quxel")
    assert client_names.run(root, PLANTED) == []


def test_the_real_names_are_the_ones_the_adoption_page_uses() -> None:
    """The hashes are of the client's names: the page that may name the client holds a word that hashes to the
    first, and that word with its one letter changed as the agent wrote it hashes to the second."""
    page = (Path(client_names.__file__).resolve().parent.parent / "docs/adopting-in-alknoma-cloud.md").read_text()
    [name] = {w.lower() for w in client_names.WORD.findall(page) if client_names.hashed(w) in client_names.NAMES}
    assert {client_names.hashed(name), client_names.hashed(name.replace("y", "i", 1))} == client_names.NAMES


def test_given_src_it_reads_the_whole_repository_and_finds_nothing() -> None:
    assert client_names.run(SRC) == []
