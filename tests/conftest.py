from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import Origin, make_origin

from gigareview.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "db" / "test.sqlite3") as opened:
        yield opened


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return make_origin(tmp_path / "git")
