import shutil

import pytest
from fastapi.testclient import TestClient

from pocketpaw.config import get_access_token, get_config_dir

# Import app and config logic
from pocketpaw.dashboard import app


# Mock config dir specifically for tests to avoid messing with real token
@pytest.fixture
def mock_config(tmp_path):
    # Override HOME or specific paths?
    # Easier to mock get_config_dir for the duration of the test,
    # but that's hard if imported.
    # Instead, we'll back up existing token if any, and restore.

    config_dir = get_config_dir()
    token_path = config_dir / "access_token"
    backup_path = config_dir / "access_token.bak"

    had_token = False
    if token_path.exists():
        shutil.move(token_path, backup_path)
        had_token = True

    yield

    # Restore
    if token_path.exists():
        token_path.unlink()

    if had_token:
        shutil.move(backup_path, token_path)


def test_token_generation(mock_config):
    """Test that a token is generated if missing."""
    settings_dir = get_config_dir()
    token_path = settings_dir / "access_token"

    # Ensure clean state
    if token_path.exists():
        token_path.unlink()

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


def test_qr_endpoint_open():
    """Test QR endpoint is open (no auth required to GET it implies login flow,
    but wait, we generate the QR *inside* the dashboard for the user to scan?
    Or is it public?
    Actually, usually the QR is displayed on the HOST device (localhost),
    so the user is already on the machine.
    Remote user logic:
    We need access to /api/qr to show it on the dashboard.
    Dashboard is loaded via localhost.
    So /api/qr should be allowed locally?
    Auth middleware allows /api/qr.
    """
    client = TestClient(app)
    response = client.get("/api/qr")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
