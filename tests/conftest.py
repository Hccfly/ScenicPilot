from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Create isolated temp roots without pytest's shared per-user directory."""
    with TemporaryDirectory(prefix="scenicpilot-pytest-") as directory:
        yield Path(directory)
