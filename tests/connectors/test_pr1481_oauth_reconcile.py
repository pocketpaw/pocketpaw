# tests/connectors/test_pr1481_oauth_reconcile.py
# Created during review of PR #1481 (connectors-oauth reconciliation) to verify
# the new behaviour with running code: (1) the auto-connect happy path works,
# (2) it avoids the fail-delete loop on a revoked token, and (3) the
# drive->google_drive rename ORPHANS an existing 'drive' connection (migration
# gap). Tests 1-2 are expected to PASS (prove the fix); test 3 documents the gap.
from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.connectors.protocol import ConnectorStatus
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore

REPO_CONNECTORS = Path(__file__).resolve().parents[2] / "connectors"


@pytest.fixture
def store(tmp_path) -> FileConnectorStateStore:
    return FileConnectorStateStore(base_dir=tmp_path / "state")


@pytest.fixture
def reg(tmp_path, store) -> ConnectorRegistry:
    return ConnectorRegistry(
        REPO_CONNECTORS, state_store=store, home_connectors_dir=tmp_path / "home"
    )


class _FakeToken:
    def __init__(self, *, access="acc", expires_at=None, refresh="ref", scopes=("a",)):
        self.access_token = access
        self.expires_at = expires_at
        self.refresh_token = refresh
        self.scopes = list(scopes)


def _patch_oauth(monkeypatch, *, token, live_token):
    """Patch the lazy imports inside _auto_connect_if_oauth."""

    class _FakeStore:
        def load(self, svc):
            return token

        def delete(self, svc):  # pragma: no cover - not used here
            pass

    class _FakeMgr:
        def __init__(self, store):
            self._store = store

        async def get_valid_token(self, **kwargs):
            return live_token

    class _FakeSettings:
        google_oauth_client_id = "cid"
        google_oauth_client_secret = "csec"

        @classmethod
        def load(cls):
            return cls()

    monkeypatch.setattr("pocketpaw.clients.token_store.TokenStore", _FakeStore)
    monkeypatch.setattr("pocketpaw.clients.oauth.OAuthManager", _FakeMgr)
    monkeypatch.setattr("pocketpaw.config.Settings", _FakeSettings)


async def test_auto_connect_registers_google_drive_with_valid_token(reg, store, monkeypatch):
    """Happy path: a live OAuth token reconciles into the registry as connected."""
    from pocketpaw.api.v1.connectors import _auto_connect_if_oauth

    _patch_oauth(monkeypatch, token=_FakeToken(), live_token="live-access-token")
    newly = await _auto_connect_if_oauth(reg, "google_drive", "default")

    assert newly is True
    statuses = {s["name"]: s["status"] for s in reg.status("default")}
    assert statuses.get("google_drive") == ConnectorStatus.CONNECTED


async def test_auto_connect_no_fail_delete_on_revoked_token(reg, store, monkeypatch):
    """A present-but-revoked token must NOT create-then-delete state (the old loop)."""
    from pocketpaw.api.v1.connectors import _auto_connect_if_oauth

    # Token exists with a refresh_token, but the refresh fails -> get_valid_token None.
    _patch_oauth(monkeypatch, token=_FakeToken(refresh="dead"), live_token=None)
    newly = await _auto_connect_if_oauth(reg, "google_drive", "default")

    assert newly is False
    # No state row was written for google_drive (no fail-delete churn).
    assert store.get("google_drive", "default") is None
    statuses = {s["name"]: s["status"] for s in reg.status("default")}
    assert statuses.get("google_drive") == ConnectorStatus.DISCONNECTED


def test_rename_orphans_existing_drive_connection(reg, store):
    """GAP: a user connected as 'drive' before the rename is now orphaned, not migrated.

    Documents the migration gap — there is no rename of existing state rows, so the
    pre-rename connection surfaces as definition_missing and google_drive shows
    disconnected until the user re-authorises.
    """
    # Simulate an existing connection persisted under the OLD name.
    store.set("drive", "default", {"scope": "https://www.googleapis.com/auth/drive"})

    statuses = {s["name"]: s["status"] for s in reg.status("default")}

    # The old connection is orphaned (definition gone after the rename)...
    assert statuses.get("drive") == ConnectorStatus.DEFINITION_MISSING
    # ...and the renamed connector has no state -> appears disconnected.
    assert statuses.get("google_drive") == ConnectorStatus.DISCONNECTED
