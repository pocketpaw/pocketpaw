from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_upload_root(tmp_path: Path) -> Path:
    """Isolated root for each test."""
    root = tmp_path / "uploads"
    root.mkdir()
    return root
