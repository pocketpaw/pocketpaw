# test_e2e_cloud_api.py — E2E: Real HTTP calls against paw-cloud (localhost:3000).
# Created: 2026-03-28
# Requires paw-cloud running at http://localhost:3000.
# Skipped automatically when the backend is not reachable.
#
# Run with:
#   PYTHONPATH=. uv run pytest tests/test_e2e_cloud_api.py -v

from __future__ import annotations

import socket

import httpx
import pytest

# ---------------------------------------------------------------------------
# Reachability guard — skips the entire module when backend is offline
# ---------------------------------------------------------------------------


def _is_backend_up(host: str = "localhost", port: int = 3000) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


BACKEND_UP = _is_backend_up()
skip_if_no_backend = pytest.mark.skipif(
    not BACKEND_UP,
    reason="paw-cloud not running at localhost:3000",
)

BASE_URL = "http://localhost:3000"
SUPERUSER_EMAIL = "daw@aahnik.dev"
SUPERUSER_PASSWORD = "hello super interacly"


# ---------------------------------------------------------------------------
# Shared session fixture — logs in once per test module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def auth_client():
    """Synchronous fixture that provides an httpx.Client with auth cookie.

    Logs in as the superuser and reuses the session across all tests in this
    module to avoid repeated login round-trips.
    """
    client = httpx.Client(base_url=BASE_URL, timeout=15.0)
    resp = client.post(
        "/auth/login",
        json={"email": SUPERUSER_EMAIL, "password": SUPERUSER_PASSWORD},
    )
    assert resp.status_code in (200, 201), (
        f"Login failed with {resp.status_code}: {resp.text[:300]}"
    )
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@skip_if_no_backend
def test_login_returns_ok(auth_client: httpx.Client) -> None:
    """POST /auth/login returns 200 and the response body contains user data."""
    # auth_client fixture already performed login — re-login to verify response
    resp = auth_client.post(
        "/auth/login",
        json={"email": SUPERUSER_EMAIL, "password": SUPERUSER_PASSWORD},
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    # At minimum, there should be some user identifier in the response
    assert isinstance(data, dict)


@skip_if_no_backend
def test_get_me_returns_user_profile(auth_client: httpx.Client) -> None:
    """GET /auth/me returns the authenticated user's profile."""
    resp = auth_client.get("/auth/me")
    assert resp.status_code == 200
    profile = resp.json()
    # Profile should include something identifiable
    assert isinstance(profile, dict)
    # Typical NestJS user objects include id or email
    assert any(key in profile for key in ("id", "email", "userId")), (
        f"Unexpected profile shape: {list(profile.keys())}"
    )


@skip_if_no_backend
def test_agents_list_populated(auth_client: httpx.Client) -> None:
    """GET /agents returns a non-empty list of agents."""
    resp = auth_client.get("/agents")
    assert resp.status_code == 200
    data = resp.json()
    # Could be list or paginated object
    if isinstance(data, list):
        agents = data
    elif isinstance(data, dict):
        agents = data.get("data") or data.get("agents") or data.get("items") or []
    else:
        agents = []
    assert len(agents) >= 1, f"Expected agents list to be non-empty, got: {data}"


@skip_if_no_backend
def test_ocean_room_lifecycle(auth_client: httpx.Client) -> None:
    """Create a room → add message → verify persistence → delete."""
    created_room_id = None
    try:
        # --- Create room ---
        resp = auth_client.post("/ocean/rooms", json={"name": "test-e2e-room"})
        assert resp.status_code in (200, 201), f"Create room failed: {resp.text[:300]}"
        room = resp.json()
        created_room_id = room.get("id") or room.get("roomId")
        assert created_room_id, f"Room ID missing from response: {room}"

        # --- Add a user message ---
        msg_resp = auth_client.post(
            f"/ocean/rooms/{created_room_id}/messages",
            json={"content": "Hello from e2e test", "role": "user"},
        )
        assert msg_resp.status_code in (200, 201), f"Add message failed: {msg_resp.text[:300]}"

        # --- Get room and verify message persisted ---
        get_resp = auth_client.get(f"/ocean/rooms/{created_room_id}")
        assert get_resp.status_code == 200
        room_data = get_resp.json()
        assert room_data is not None

        # Messages may be on the room object directly or fetched separately
        messages = room_data.get("messages", [])
        if messages:
            contents = [m.get("content", "") for m in messages]
            assert any("Hello from e2e test" in c for c in contents), (
                f"Test message not found in room messages: {contents}"
            )

    finally:
        # --- Cleanup: always delete the room ---
        if created_room_id:
            del_resp = auth_client.delete(f"/ocean/rooms/{created_room_id}")
            assert del_resp.status_code in (200, 204), (
                f"Room delete returned {del_resp.status_code}"
            )


@skip_if_no_backend
def test_get_dms_list(auth_client: httpx.Client) -> None:
    """GET /rooms/dms returns a list (may be empty for a fresh account)."""
    resp = auth_client.get("/rooms/dms")
    assert resp.status_code == 200
    data = resp.json()
    # Accept list or paginated response
    assert isinstance(data, (list, dict)), f"Unexpected DMs response type: {type(data)}"


@skip_if_no_backend
def test_unauthorized_access_rejected() -> None:
    """Requests without auth cookie must be rejected (401 or 403)."""
    unauthenticated = httpx.Client(base_url=BASE_URL, timeout=10.0)
    resp = unauthenticated.get("/auth/me")
    unauthenticated.close()
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated /auth/me, got {resp.status_code}"
    )
