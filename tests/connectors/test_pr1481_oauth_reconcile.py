# tests/connectors/test_pr1481_oauth_reconcile.py
# Created during review of PR #1481 (connectors-oauth reconciliation) to verify
# the new behaviour with running code: (1) the auto-connect happy path works,
# (2) it avoids the fail-delete loop on a revoked token, and (3) the
# drive->google_drive rename MIGRATES an existing 'drive' connection instead of
# orphaning it (the registry runs a one-time rename migration on construction).
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
    """Happy path: a live OAuth token reconciles into the registry as connected.

    The adapter's own ``connect()`` (a real token/network handshake) is tested
    elsewhere and is env-dependent; here we verify the reconciliation LOGIC, so
    ``reg.connect`` is stubbed to the success it returns once the token is live.
    """
    from pocketpaw.api.v1.connectors import _auto_connect_if_oauth

    _patch_oauth(monkeypatch, token=_FakeToken(), live_token="live-access-token")

    class _Result:
        success = True

    async def _fake_connect(pocket_id, name, config):
        store.set(name, pocket_id, config)  # mirror what a real connect persists
        return _Result()

    monkeypatch.setattr(reg, "connect", _fake_connect)

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


def test_rename_migrates_existing_drive_connection(tmp_path, store):
    """A user connected as 'drive' before the rename is MIGRATED, not orphaned.

    The registry runs a one-time, idempotent rename migration on construction:
    an existing ``drive`` state row is moved to ``google_drive`` so the
    connection survives the rename instead of surfacing as definition_missing.
    """
    # Simulate an existing connection persisted under the OLD name, BEFORE the
    # registry is built (mirrors a real on-disk row from a prior release).
    store.set("drive", "default", {"scope": "https://www.googleapis.com/auth/drive"})

    # Building the registry triggers the migration.
    reg = ConnectorRegistry(
        REPO_CONNECTORS, state_store=store, home_connectors_dir=tmp_path / "home"
    )

    statuses = {s["name"]: s["status"] for s in reg.status("default")}

    # The connection survived the rename, carried over to the new name...
    assert statuses.get("google_drive") == ConnectorStatus.CONNECTED
    # ...and the legacy 'drive' row is gone (no orphan, no definition_missing).
    assert "drive" not in statuses
    assert store.get("drive", "default") is None
    assert store.get("google_drive", "default") is not None
