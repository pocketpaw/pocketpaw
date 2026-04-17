from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pocketpaw.config import get_access_token

# Import app and config logic
from pocketpaw.dashboard import app


# Mock config dir to use tmp_path — avoids race conditions under parallel test runs.
# Patches every call site so get_access_token / get_token_path resolve to tmp_path.
@pytest.fixture
def mock_config(tmp_path):
    with patch("pocketpaw.config.get_config_dir", return_value=tmp_path):
        # Clear any cached token so it regenerates under tmp_path
        token_path = tmp_path / "access_token"
        token_path.unlink(missing_ok=True)
        yield


def test_token_generation(mock_config, tmp_path):
    """Test that a token is generated if missing."""
    token_path = tmp_path / "access_token"

    # Ensure clean state
    token_path.unlink(missing_ok=True)

    token = get_access_token()
    assert token is not None
    assert len(token) > 20  # UUID length
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8") == token


def test_auth_middleware_deny():
    """Test access denied without token."""
    client = TestClient(app)

    # Access protected endpoint
    response = client.get("/api/identity")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_auth_middleware_allow_header(mock_config):
    """Test access allowed with Bearer token."""
    token = get_access_token()
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/identity", headers=headers)

    # We might get 500 if dependencies fail (e.g. settings file missing),
    # but we should get PAST the 401.
    assert response.status_code != 401

    # If app works fully, 200. If internal error, 500.
    # Since we fixed the import, ideally 200.
    if response.status_code == 500:
        print("Got 500, but auth passed. Error:", response.text)

    # Ideally 200
    assert response.status_code == 200


def test_auth_middleware_allow_query_param(mock_config):
    """Test access allowed with query param."""
    token = get_access_token()
    client = TestClient(app)

    response = client.get(f"/api/identity?token={token}")
    assert response.status_code != 401
    assert response.status_code == 200


def test_qr_endpoint_requires_auth():
    """Test that /api/qr returns 401 without authentication.

    Fixes #854 — the QR endpoint was previously exempt from auth,
    allowing any network-reachable client to obtain a valid session token.
    """
    client = TestClient(app)
    response = client.get("/api/qr")
    assert response.status_code == 401


def test_qr_endpoint_allowed_with_auth(mock_config):
    """Test that /api/qr returns a PNG image when authenticated."""
    token = get_access_token()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/qr", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
