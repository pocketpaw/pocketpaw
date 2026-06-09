# Tests for the Web Push SEND path (pocketpaw#1392).
# Created: 2026-06-09 (feat/push-send-prune) — exercises send_to_user's
# fan-out to every stored subscription, the 404/410 dead-endpoint pruning,
# the "one bad endpoint doesn't abort the rest" resilience, the private-key
# chokepoint usage (signing pulls the tenant PEM, the key never leaves the
# backend), and the no-op empty-subscription case. ``pywebpush.webpush`` is
# patched in every test so NO network call is made; the patch records the
# subscription_info / data / vapid args each call receives so we can assert
# the send was signed with the tenant key and carried the payload.

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.push import service as push_service
from pocketpaw_ee.cloud.push.dto import PushPayload

pytestmark = pytest.mark.usefixtures("mongo_db")

_KEY_ENV = "CLOUD_ENCRYPTION_KEY"


@pytest.fixture
def enc_key(monkeypatch):
    """Set a valid Fernet key so VAPID private-key encrypt/decrypt works."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(_KEY_ENV, key)
    return key


def _sub_body(endpoint: str) -> dict:
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "PUB256", "auth": "AUTHSECRET"},
        "expirationTime": None,
    }


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _web_push_exc(status_code: int):
    """Build a WebPushException carrying a vendor response with a status."""
    return push_service.WebPushException("dead", response=_FakeResponse(status_code))


async def _seed(workspace: str, user: str, *endpoints: str) -> None:
    for ep in endpoints:
        await push_service.subscribe(workspace, user, _sub_body(ep))


# ---------------------------------------------------------------------------
# Fan-out — every subscription is attempted, signed with the tenant key
# ---------------------------------------------------------------------------


async def test_send_fans_out_to_all_subscriptions(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/a", "https://push.example/b")
    # Generate the keypair so the send path can decrypt a private PEM.
    await push_service.get_vapid_public_key("w1")

    calls: list[dict] = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    result = await push_service.send_to_user("w1", "u1", PushPayload(title="Hi", body="There"))

    assert result.sent == 2
    assert result.pruned == 0
    assert result.failed == 0
    # Both endpoints were attempted.
    endpoints = {c["subscription_info"]["endpoint"] for c in calls}
    assert endpoints == {"https://push.example/a", "https://push.example/b"}
    # Each call carried the browser keys and the JSON payload.
    for c in calls:
        assert c["subscription_info"]["keys"] == {"p256dh": "PUB256", "auth": "AUTHSECRET"}
        assert json.loads(c["data"]) == {"title": "Hi", "body": "There"}


async def test_send_signs_with_tenant_private_key(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/a")
    await push_service.get_vapid_public_key("w1")
    expected_pem = await push_service.get_decrypted_private_pem("w1")

    captured: dict = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))

    # The PEM handed to webpush is the tenant key from the chokepoint, and it
    # is a real private PEM (never the public key, never ciphertext).
    assert captured["vapid_private_key"] == expected_pem
    assert "BEGIN PRIVATE KEY" in captured["vapid_private_key"]
    # The contact claim is present and non-personal by default.
    assert captured["vapid_claims"]["sub"].startswith("mailto:")


async def test_send_contact_is_configurable(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/a")
    await push_service.get_vapid_public_key("w1")
    monkeypatch.setenv("CLOUD_PUSH_CONTACT", "mailto:ops@example.test")

    captured: dict = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)
    await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))
    assert captured["vapid_claims"]["sub"] == "mailto:ops@example.test"


# ---------------------------------------------------------------------------
# Pruning — 404/410 deletes the dead row
# ---------------------------------------------------------------------------


async def test_send_prunes_410_gone(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/dead")
    await push_service.get_vapid_public_key("w1")

    def fake_webpush(**kwargs):
        raise _web_push_exc(410)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    result = await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))

    assert result.pruned == 1
    assert result.sent == 0
    # The dead subscription row was deleted.
    assert await push_service.list_for_user("w1", "u1") == []


async def test_send_prunes_404(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/gone")
    await push_service.get_vapid_public_key("w1")

    monkeypatch.setattr(
        push_service, "webpush", lambda **kw: (_ for _ in ()).throw(_web_push_exc(404))
    )

    result = await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))
    assert result.pruned == 1
    assert await push_service.list_for_user("w1", "u1") == []


async def test_send_only_prunes_the_dead_endpoint(enc_key, monkeypatch) -> None:
    # Two endpoints, one dead (410) and one live — only the dead one is pruned,
    # and the whole fan-out still completes.
    await _seed("w1", "u1", "https://push.example/live", "https://push.example/dead")
    await push_service.get_vapid_public_key("w1")

    def fake_webpush(**kwargs):
        if kwargs["subscription_info"]["endpoint"].endswith("/dead"):
            raise _web_push_exc(410)
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    result = await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))

    assert result.sent == 1
    assert result.pruned == 1
    remaining = await push_service.list_for_user("w1", "u1")
    assert [s.endpoint for s in remaining] == ["https://push.example/live"]


# ---------------------------------------------------------------------------
# Resilience — a non-404/410 error doesn't abort the fan-out, isn't pruned
# ---------------------------------------------------------------------------


async def test_send_one_bad_endpoint_does_not_abort_rest(enc_key, monkeypatch) -> None:
    await _seed(
        "w1",
        "u1",
        "https://push.example/ok1",
        "https://push.example/boom",
        "https://push.example/ok2",
    )
    await push_service.get_vapid_public_key("w1")

    def fake_webpush(**kwargs):
        if kwargs["subscription_info"]["endpoint"].endswith("/boom"):
            # A 500 (transient) — logged + counted, NOT pruned.
            raise _web_push_exc(500)
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    result = await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))

    assert result.sent == 2  # both good endpoints still got their send
    assert result.failed == 1
    assert result.pruned == 0
    # The transient-failure endpoint is left in place (only 404/410 prune).
    assert len(await push_service.list_for_user("w1", "u1")) == 3


async def test_send_unexpected_exception_is_swallowed(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/a", "https://push.example/b")
    await push_service.get_vapid_public_key("w1")

    seen: list[str] = []

    def fake_webpush(**kwargs):
        ep = kwargs["subscription_info"]["endpoint"]
        seen.append(ep)
        if ep.endswith("/a"):
            raise RuntimeError("connection reset")  # not a WebPushException
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    result = await push_service.send_to_user("w1", "u1", PushPayload(title="t", body="b"))

    assert len(seen) == 2  # both endpoints attempted despite the first raising
    assert result.sent == 1
    assert result.failed == 1


# ---------------------------------------------------------------------------
# No-op — a user with no subscriptions doesn't touch the key or the network
# ---------------------------------------------------------------------------


async def test_send_no_subscriptions_is_noop(enc_key, monkeypatch) -> None:
    called = False

    def fake_webpush(**kwargs):
        nonlocal called
        called = True
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    result = await push_service.send_to_user("w1", "ghost", PushPayload(title="t", body="b"))

    assert result.sent == 0 and result.pruned == 0 and result.failed == 0
    assert called is False  # no fan-out, so webpush was never invoked


async def test_send_accepts_dict_payload(enc_key, monkeypatch) -> None:
    await _seed("w1", "u1", "https://push.example/a")
    await push_service.get_vapid_public_key("w1")

    captured: dict = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)
        return _FakeResponse(201)

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    # Internal callers may pass a raw dict; the service validates it.
    result = await push_service.send_to_user(
        "w1", "u1", {"title": "Hi", "body": "B", "url": "/inbox"}
    )
    assert result.sent == 1
    assert json.loads(captured["data"]) == {"title": "Hi", "body": "B", "url": "/inbox"}
