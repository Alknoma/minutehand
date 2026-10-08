"""One hand-written run and its fork per module: the read model's tests only read them."""

from __future__ import annotations

import pytest

from tests.query.fixture import Fixture, build_fixture


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Fixture:
    return build_fixture(tmp_path_factory.mktemp("state"))
