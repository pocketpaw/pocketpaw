# conftest.py — shared fixtures for the browser tests.
# Created: 2026-09-06 (BR-5, feat/browser-surface-profile).
#
# BR-5 gave every session its own persistent profile under
# ``~/.pocketpaw/browser-profiles/<session_id>``, which means the session-manager
# tests started writing directories into the DEVELOPER'S real home the moment
# they called ``get_or_create`` — silently, since the driver itself is mocked.
# This redirects the config dir at a tmp path for the whole tree.
"""Shared fixtures for the browser tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def profiles_home(tmp_path, monkeypatch):
    """Redirect ``get_config_dir`` so no test touches the real ~/.pocketpaw."""
    monkeypatch.setattr("pocketpaw.config.get_config_dir", lambda: tmp_path)
    return tmp_path
