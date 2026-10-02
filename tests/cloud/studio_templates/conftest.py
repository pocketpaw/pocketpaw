# tests/cloud/studio_templates/conftest.py — shared fixtures for studio templates.
#
# Created 2026-10-02 (feat/studio-templates): the builtin Discover sources (so the
# sync and reindex tests can resolve ``studio_template``) and a fixed public base
# URL for the absolute media URLs.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.discover import sources

BASE = "https://paw.example"


@pytest.fixture(autouse=True)
def builtin_sources() -> None:
    sources.register_builtin_sources()


@pytest.fixture(autouse=True)
def public_base(monkeypatch) -> str:
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", BASE + "/")
    return BASE
