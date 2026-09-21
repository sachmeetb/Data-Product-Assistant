import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packs.spec import BlockLibrary, PackRegistry  # noqa: E402


@pytest.fixture(scope="session")
def registry() -> PackRegistry:
    return PackRegistry()


@pytest.fixture(scope="session")
def blocks() -> BlockLibrary:
    return BlockLibrary()
