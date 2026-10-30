# tests/ee/versions/conftest.py
# Created: 2026-06-18 (feat/branch-primitive-versions, BP-1) — fixtures for the
# versions-core tests.
#
# The versions service emits Journal events through the process-cached org
# journal (``pocketpaw.journal_dep.get_journal``). To make those events
# assertable + isolated, ``versions_journal`` points the cached journal at a
# per-test tmp file by setting ``SOUL_DATA_DIR`` (which ``journal_dep`` reads
# when resolving the org data dir) and clearing the lru cache before/after.
# It yields the opened Journal so a test can ``.query(action=...)`` it.
#
# ``beanie_test_db`` (the mongomock-motor + init_beanie fixture that registers
# ALL_DOCUMENTS, including ArtifactVersion) is inherited from
# ``tests/ee/conftest.py`` — no need to redeclare it here.
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.journal_dep import get_journal, reset_journal_cache


@pytest.fixture()
def versions_journal(tmp_path: Path) -> Iterator:
    """Point the cached org journal at a fresh per-test tmp file and yield it.

    Resetting the cache before and after keeps the journal from leaking across
    tests; setting ``SOUL_DATA_DIR`` makes ``journal_dep._org_data_dir`` resolve
    to ``tmp_path`` so the service's lazy ``get_journal()`` opens the same file
    the test reads back.
    """
    reset_journal_cache()
    with patch.dict(os.environ, {"SOUL_DATA_DIR": str(tmp_path)}):
        journal = get_journal()
        try:
            yield journal
        finally:
            reset_journal_cache()
