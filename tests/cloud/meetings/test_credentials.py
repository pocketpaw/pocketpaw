# Tests for pocketpaw_ee/cloud/meetings/credentials.py + _core/crypto.py
# Covers: at-rest encryption round-trip, provider credential storage,
# Google Meet OAuth state handling, disconnect, and the stored-vs-env
# resolution used by the adapter factory.

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import (
    CompleteGoogleMeetOAuthRequest,
    StoreGoogleMeetCredentialsRequest,
    StoreZoomCredentialsRequest,
)
from pocketpaw_ee.cloud.meetings.providers.recall import credentials as creds
from pocketpaw_ee.cloud.meetings.providers.recall.clients.zoom import ZoomAPIError
from pocketpaw_ee.cloud.models.meeting import MeetingProviderCredentials

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"


@pytest.fixture
def enc_key(monkeypatch):
    """Set a valid Fernet key in the environment for the test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(_KEY_ENV, key)
    return key


# ---------------------------------------------------------------------------
# _core.crypto — at-rest encryption
# ---------------------------------------------------------------------------


def test_is_configured_false_without_key(monkeypatch):
    monkeypatch.delenv(_KEY_ENV, raising=False)
    assert crypto.is_configured() is False


def test_is_configured_true_with_key(enc_key):
    assert crypto.is_configured() is True


def test_encrypt_decrypt_round_trip(enc_key):
    payload = {"client_secret": "s3cr3t", "refresh_token": "r3fr3sh"}
    token = crypto.encrypt_json(payload)
    assert token and "s3cr3t" not in token  # genuinely encrypted
    assert crypto.decrypt_json(token) == payload


def test_decrypt_empty_token_returns_empty(enc_key):
    assert crypto.decrypt_json("") == {}


def test_encrypt_without_key_raises(monkeypatch):
    monkeypatch.delenv(_KEY_ENV, raising=False)
    with pytest.raises(ValidationError):
        crypto.encrypt_json({"a": "b"})


def test_decrypt_with_rotated_key_raises(monkeypatch):
    """Ciphertext written under one key cannot be read under another."""
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())
    token = crypto.encrypt_json({"a": "b"})
    monkeypatch.setenv(_KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(ValidationError):
        crypto.decrypt_json(token)


def test_invalid_key_raises(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "not-a-valid-fernet-key")
    with pytest.raises(ValidationError):
        crypto.encrypt_json({"a": "b"})


# ---------------------------------------------------------------------------
# Google Meet — store + OAuth consent
# ---------------------------------------------------------------------------


async def test_store_google_meet_then_get(mongo_db, enc_key):
    snap = await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="cid", client_secret="csec")
    )
    assert snap.provider == "google_meet"
    assert snap.has_credentials is True
    assert snap.enabled is False  # not enabled until consent completes
    assert snap.last_error == "awaiting_oauth_consent"

    fetched = await creds.get_credentials("google_meet")
    assert fetched.has_credentials is True
    assert fetched.enabled is False


async def test_get_credentials_unconfigured(mongo_db):
    snap = await creds.get_credentials("zoom")
    assert snap.has_credentials is False
    assert snap.enabled is False


async def test_auth_url_requires_stored_creds(mongo_db, enc_key):
    with pytest.raises(ValidationError):
        await creds.get_google_meet_auth_url()


async def test_auth_url_after_store(mongo_db, enc_key):
    await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="my-client-id", client_secret="csec")
    )
    res = await creds.get_google_meet_auth_url()
    assert "my-client-id" in res.auth_url
    assert "accounts.google.com" in res.auth_url
    assert res.redirect_uri == "http://localhost"
    # The state nonce is persisted and echoed into the URL.
    doc = await creds._get_doc("google_meet")
    assert doc.pending_state and doc.pending_state in res.auth_url


async def test_complete_oauth_state_mismatch(mongo_db, enc_key):
    await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="cid", client_secret="csec")
    )
    await creds.get_google_meet_auth_url()
    with pytest.raises(ValidationError):
        await creds.complete_google_meet_oauth(
            CompleteGoogleMeetOAuthRequest(code="abc", state="wrong-state")
        )


async def test_complete_oauth_happy_path(mongo_db, enc_key, monkeypatch):
    await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="cid", client_secret="csec")
    )
    await creds.get_google_meet_auth_url()
    state = (await creds._get_doc("google_meet")).pending_state

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"refresh_token": "the-refresh-token", "access_token": "at"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(creds.httpx, "AsyncClient", _FakeClient)
    snap = await creds.complete_google_meet_oauth(
        CompleteGoogleMeetOAuthRequest(code="auth-code", state=state)
    )
    assert snap.enabled is True
    assert snap.last_error == ""
    # The refresh token is now resolvable for the adapter factory.
    resolved = await creds.resolve("google_meet")
    assert resolved == {
        "client_id": "cid",
        "client_secret": "csec",
        "refresh_token": "the-refresh-token",
    }


# ---------------------------------------------------------------------------
# Zoom — store + validate
# ---------------------------------------------------------------------------


async def test_store_zoom_validates_and_persists(mongo_db, enc_key, monkeypatch):
    class _OkZoom:
        def __init__(self, *a, **k):
            pass

        async def _get_token(self):
            return "tok"

    monkeypatch.setattr(creds, "ZoomClient", _OkZoom)
    snap = await creds.store_zoom(
        StoreZoomCredentialsRequest(account_id="acc", client_id="cid", client_secret="csec")
    )
    assert snap.enabled is True
    assert snap.has_credentials is True
    resolved = await creds.resolve("zoom")
    assert resolved == {"account_id": "acc", "client_id": "cid", "client_secret": "csec"}


async def test_store_zoom_rejects_invalid(mongo_db, enc_key, monkeypatch):
    class _BadZoom:
        def __init__(self, *a, **k):
            pass

        async def _get_token(self):
            raise ZoomAPIError(401, "invalid_client")

    monkeypatch.setattr(creds, "ZoomClient", _BadZoom)
    with pytest.raises(ValidationError):
        await creds.store_zoom(
            StoreZoomCredentialsRequest(account_id="acc", client_id="cid", client_secret="bad")
        )
    # Nothing was persisted on validation failure.
    assert (await creds.get_credentials("zoom")).has_credentials is False


# ---------------------------------------------------------------------------
# Disconnect + list
# ---------------------------------------------------------------------------


async def test_disconnect(mongo_db, enc_key):
    await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="cid", client_secret="csec")
    )
    res = await creds.disconnect("google_meet")
    assert res.disconnected is True
    assert (await creds.get_credentials("google_meet")).has_credentials is False
    with pytest.raises(NotFound):
        await creds.disconnect("google_meet")


async def test_disconnect_unknown_provider(mongo_db):
    with pytest.raises(ValidationError):
        await creds.disconnect("webex")


async def test_list_credentials(mongo_db, enc_key):
    assert await creds.list_credentials() == []
    await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="cid", client_secret="csec")
    )
    rows = await creds.list_credentials()
    assert [r.provider for r in rows] == ["google_meet"]


# ---------------------------------------------------------------------------
# resolve() — feeds the adapter factory
# ---------------------------------------------------------------------------


async def test_resolve_none_when_no_doc(mongo_db):
    assert await creds.resolve("zoom") is None


async def test_resolve_none_when_not_enabled(mongo_db, enc_key):
    await creds.store_google_meet(
        StoreGoogleMeetCredentialsRequest(client_id="cid", client_secret="csec")
    )
    assert await creds.resolve("google_meet") is None  # enabled flips only after consent


async def test_resolve_returns_enabled_creds(mongo_db, enc_key):
    await MeetingProviderCredentials(
        provider="google_meet",
        enabled=True,
        public_config={"client_id": "cid"},
        secret_enc=crypto.encrypt_json({"client_secret": "csec", "refresh_token": "rt"}),
    ).insert()
    assert await creds.resolve("google_meet") == {
        "client_id": "cid",
        "client_secret": "csec",
        "refresh_token": "rt",
    }


# ---------------------------------------------------------------------------
# Adapter factory — stored creds win, env is the fallback
# ---------------------------------------------------------------------------


async def test_adapter_factory_prefers_stored_creds(mongo_db, enc_key, monkeypatch):
    await MeetingProviderCredentials(
        provider="zoom",
        enabled=True,
        public_config={"account_id": "stored-acc", "client_id": "stored-cid"},
        secret_enc=crypto.encrypt_json({"client_secret": "stored-sec"}),
    ).insert()
    # Env is also set — the stored row must take precedence.
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "env-acc")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "env-cid")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "env-sec")

    adapter = await meetings_service._build_adapter_default("ws-1", "zoom")
    assert adapter._client._account_id == "stored-acc"


async def test_adapter_factory_falls_back_to_env(mongo_db, monkeypatch):
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "env-acc")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "env-cid")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "env-sec")
    adapter = await meetings_service._build_adapter_default("ws-1", "zoom")
    assert adapter._client._account_id == "env-acc"
