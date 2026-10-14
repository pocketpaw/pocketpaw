# tests/cloud/growth/social/test_accounts.py — Growth › Social accounts
# (``/api/v1/growth/social/accounts``) over the test_router harness, with a fake
# Postiz installed through ``set_production_postiz``. Covers the unconfigured
# deployment, one org per profile, the server-built OAuth return URL, platform
# errors, tenant and profile isolation, disconnect, the encrypted key, the
# Telegram code flow (a fake Bot API read through
# ``set_production_telegram_updates``, including the cross-workspace sweep), and
# the Postiz client's JWT signer, create-org failure and the Bot API read's token
# scrubbing (``httpx.MockTransport``).

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.crypto import decrypt, encrypt
from pocketpaw_ee.cloud.growth.social.postiz import (
    PostizClient,
    PostizError,
    PostizPlatformUnavailable,
    set_production_postiz,
    set_production_telegram_updates,
    sign_jwt,
    telegram_updates,
)
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg

from tests.cloud.growth.social.test_router import BASE, _client

ACCOUNTS = f"{BASE}/accounts"
CONNECT = f"{ACCOUNTS}/connect"
CHECK = f"{ACCOUNTS}/telegram/check"
TOKEN = "123:bot-secret"
LOCAL = {"Origin": "http://localhost:5173"}


class _FakePostiz:
    def __init__(self) -> None:
        self.url: str | Exception = "https://api.x.com/oauth/authenticate?oauth_token=t"
        self.integrations: dict[str, list[dict[str, Any]]] = {}
        self.created: list[tuple[str, str]] = []
        self.connects: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str, str]] = []

    async def create_org(self, external_id: str, name: str) -> tuple[str, str]:
        self.created.append((external_id, name))
        key = f"key-{len(self.created)}"
        self.integrations[key] = []
        return f"org-{len(self.created)}", key

    async def connect_url(
        self, api_key: str, platform: str, redirect_url: str, refresh_id: str | None = None
    ) -> str:
        self.connects.append((api_key, platform, redirect_url))
        if isinstance(self.url, Exception):
            raise self.url
        return self.url

    async def connect_state(self, api_key: str, platform: str, redirect_url: str) -> str:
        self.connects.append((api_key, platform, redirect_url))
        return f"state-{len(self.connects)}"

    async def complete_connect(self, platform: str, state: str, code: str) -> dict[str, Any]:
        self.completed.append((platform, state, code))
        return {
            "id": f"tg-{len(self.completed)}",
            "name": "Acme News",
            "picture": "",
            "providerIdentifier": "telegram",
        }

    async def list_integrations(self, api_key: str) -> list[dict[str, Any]]:
        return list(self.integrations.get(api_key, []))

    async def delete_integration(self, api_key: str, integration_id: str) -> None:
        self.deleted.append((api_key, integration_id))


def _integration(account_id: str) -> dict[str, Any]:
    return {
        "id": account_id,
        "name": "Acme",
        "identifier": "x",
        "picture": "https://pbs.example/a.png",
        "disabled": False,
        "profile": "acme",
    }


class _FakeTelegram:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, int | None]] = []

    async def __call__(self, token: str, offset: int | None = None) -> list[dict[str, Any]]:
        self.calls.append((token, offset))
        if offset is not None:
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
        return list(self.updates)

    def post(self, update_id: int, text: str, chat_id: int, kind: str = "channel_post") -> None:
        self.updates.append({"update_id": update_id, kind: {"text": text, "chat": {"id": chat_id}}})


def _settings(token: str | None = TOKEN, name: str | None = "PawBot") -> SimpleNamespace:
    return SimpleNamespace(
        postiz_url=None,
        postiz_jwt_secret=None,
        postiz_platforms=None,
        postiz_telegram_bot_token=token,
        postiz_telegram_bot_name=name,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "https://app.paw.test")
    try:
        yield
    finally:
        set_production_postiz(None)
        set_production_telegram_updates(None)


@pytest.fixture
def postiz() -> _FakePostiz:
    fake = _FakePostiz()
    set_production_postiz(fake)  # type: ignore[arg-type]
    return fake


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> _FakeTelegram:
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _settings())
    fake = _FakeTelegram()
    set_production_telegram_updates(fake)
    return fake


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with await _client("w2") as client:
        yield client


async def _profile(client: httpx.AsyncClient, name: str = "Acme") -> str:
    pid = (await client.post(f"{BASE}/profiles")).json()["id"]
    await client.put(f"{BASE}/profile", params={"profile_id": pid}, json={"company_name": name})
    return pid


async def test_unconfigured_lists_nothing_and_refuses_connect(w1, monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(postiz_url=None, postiz_jwt_secret=None, postiz_platforms=None),
    )
    await _profile(w1)
    listed = (await w1.get(ACCOUNTS)).json()
    assert listed["configured"] is False and listed["items"] == []
    ids = [p["id"] for p in listed["platforms"]]
    assert len(ids) == 13 and ids[0] == "x" and ids[-3:] == ["dribbble", "slack", "telegram"]
    resp = await w1.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "social.accounts_unavailable"


async def test_connect_makes_one_org_per_profile_and_builds_the_return_url(w1, postiz):
    pid = await _profile(w1)
    listed = (await w1.get(ACCOUNTS)).json()
    assert listed["configured"] is True and listed["items"] == []
    assert postiz.created == []

    for _ in range(2):
        resp = await w1.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
        assert resp.status_code == 200
        assert resp.json() == {"url": postiz.url, "telegram": None}
    assert postiz.created == [(f"w1:{pid}", "Acme")]
    assert postiz.connects[0] == ("key-1", "x", "http://localhost:5173/growth/social/accounts")

    hostile = {"Origin": "https://evil.example"}
    assert (await w1.post(CONNECT, json={"platform": "x"}, headers=hostile)).status_code == 200
    assert postiz.connects[-1][2] == "https://app.paw.test/growth/social/accounts"

    doc = await SocialPostizOrg.find_one({"workspace": "w1", "profile": pid})
    assert doc is not None and doc.postiz_org_id == "org-1"
    assert doc.api_key_enc != "key-1" and decrypt(doc.api_key_enc) == "key-1"


async def test_platform_errors(w1, postiz):
    await _profile(w1)
    resp = await w1.post(CONNECT, json={"platform": "myspace"}, headers=LOCAL)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.platform_not_supported"
    assert postiz.created == []

    postiz.url = PostizPlatformUnavailable("postiz.platform_unavailable", "none")
    resp = await w1.post(CONNECT, json={"platform": "tiktok"}, headers=LOCAL)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "social.platform_unavailable"
    assert error["message"] == "TikTok isn't set up on this deployment yet."

    postiz.url = PostizError("postiz.http_error", "boom")
    resp = await w1.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "social.accounts_upstream"


async def test_only_configured_platforms_are_offered(w1, postiz, monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(postiz_url=None, postiz_jwt_secret=None, postiz_platforms="x"),
    )
    await _profile(w1)
    assert (await w1.get(ACCOUNTS)).json()["platforms"] == [{"id": "x", "label": "X"}]
    resp = await w1.post(CONNECT, json={"platform": "linkedin"}, headers=LOCAL)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.platform_unavailable"
    assert postiz.created == []


async def test_profiles_get_their_own_orgs_and_accounts(w1, postiz):
    first = await _profile(w1, "Acme")
    second = await _profile(w1, "Beta")
    for pid in (first, second):
        await w1.post(CONNECT, params={"profile_id": pid}, json={"platform": "x"}, headers=LOCAL)
    assert [c[0] for c in postiz.created] == [f"w1:{first}", f"w1:{second}"]
    postiz.integrations["key-1"] = [_integration("int-1")]

    listed = (await w1.get(ACCOUNTS, params={"profile_id": first})).json()
    assert listed["items"] == [
        {
            "id": "int-1",
            "platform": "x",
            "name": "Acme",
            "username": "acme",
            "picture": "https://pbs.example/a.png",
            "disabled": False,
        }
    ]
    assert (await w1.get(ACCOUNTS, params={"profile_id": second})).json()["items"] == []


async def test_another_workspace_cannot_see_or_disconnect_accounts(w1, w2, postiz):
    await _profile(w1)
    await w1.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
    postiz.integrations["key-1"] = [_integration("int-1")]
    other = await _profile(w2)
    await SocialPostizOrg(
        workspace="w1", profile=other, postiz_org_id="org-1", api_key_enc=encrypt("key-1")
    ).insert()

    listed = (await w2.get(ACCOUNTS)).json()
    assert listed["configured"] is True and listed["items"] == []
    resp = await w2.delete(f"{ACCOUNTS}/int-1")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_account.not_found"
    assert postiz.deleted == []


async def test_disconnect(w1, postiz):
    await _profile(w1)
    assert (await w1.delete(f"{ACCOUNTS}/int-1")).status_code == 404
    await w1.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
    postiz.integrations["key-1"] = [_integration("int-1")]

    resp = await w1.delete(f"{ACCOUNTS}/int-2")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_account.not_found"

    assert (await w1.delete(f"{ACCOUNTS}/int-1")).status_code == 204
    assert postiz.deleted == [("key-1", "int-1")]


async def test_telegram_connect_needs_the_bot_and_replaces_the_code(w1, postiz, monkeypatch):
    pid = await _profile(w1)
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _settings(name=None))
    resp = await w1.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "social.telegram_unavailable"
    assert postiz.created == []

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _settings(name="@PawBot"))
    first = (await w1.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)).json()
    assert first["url"] is None
    assert first["telegram"]["code"] == "state-1" and first["telegram"]["bot_name"] == "PawBot"
    expires = datetime.fromisoformat(first["telegram"]["expires_at"])
    assert timedelta(minutes=54) < expires - datetime.now(UTC) <= timedelta(minutes=55)
    assert postiz.connects[0] == (
        "key-1",
        "telegram",
        "http://localhost:5173/growth/social/accounts",
    )

    second = (await w1.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)).json()
    doc = await SocialPostizOrg.find_one({"workspace": "w1", "profile": pid})
    assert second["telegram"]["code"] == doc.telegram_code == "state-2"
    assert doc.telegram_code_expires_at is not None and doc.telegram_chat_id is None


async def test_telegram_check_is_pending_until_the_bot_sees_connect(w1, postiz, telegram):
    pid = await _profile(w1)
    await w1.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)
    resp = await w1.post(CHECK, json={"code": "state-1"})
    assert resp.json() == {"status": "pending", "account": None}
    assert telegram.calls == [(TOKEN, None)]

    telegram.post(5, "hello", -100777, kind="message")
    telegram.post(6, "/connect@PawBot state-1", -100123)
    resp = await w1.post(CHECK, json={"code": "state-1"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "connected",
        "account": {
            "id": "tg-1",
            "platform": "telegram",
            "name": "Acme News",
            "username": None,
            "picture": None,
            "disabled": False,
        },
    }
    assert postiz.completed == [("telegram", "state-1", "-100123")]
    assert telegram.calls[-1] == (TOKEN, 7) and telegram.updates == []
    doc = await SocialPostizOrg.find_one({"workspace": "w1", "profile": pid})
    assert (doc.telegram_code, doc.telegram_code_expires_at, doc.telegram_chat_id) == (None,) * 3
    assert (await w1.post(CHECK, json={"code": "state-1"})).status_code == 404


async def test_telegram_sweep_keeps_another_workspaces_connect(w1, w2, postiz, telegram):
    await _profile(w1)
    await _profile(w2)
    await w1.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)
    await w2.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)
    telegram.post(10, "/connect state-1", -1001, kind="message")
    telegram.post(11, "/connect@PawBot state-2", -1002)

    assert (await w1.post(CHECK, json={"code": "state-1"})).json()["status"] == "connected"
    assert telegram.calls == [(TOKEN, None), (TOKEN, 12)] and telegram.updates == []
    assert (await w2.post(CHECK, json={"code": "state-2"})).json()["status"] == "connected"
    assert len(telegram.calls) == 2
    assert postiz.completed == [("telegram", "state-1", "-1001"), ("telegram", "state-2", "-1002")]


async def test_telegram_check_refuses_unknown_expired_and_foreign_codes(w1, w2, postiz, telegram):
    pid = await _profile(w1)
    await w1.post(CONNECT, json={"platform": "telegram"}, headers=LOCAL)
    resp = await w1.post(CHECK, json={"code": "nope"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "telegram_code.not_found"

    other = await _profile(w2)
    await SocialPostizOrg(
        workspace="w1",
        profile=other,
        postiz_org_id="org-9",
        api_key_enc=encrypt("key-9"),
        telegram_code="planted",
        telegram_code_expires_at=datetime.now(UTC) + timedelta(hours=1),
    ).insert()
    assert (await w2.post(CHECK, json={"code": "planted"})).status_code == 404

    doc = await SocialPostizOrg.find_one({"workspace": "w1", "profile": pid})
    await doc.set({"telegram_code_expires_at": datetime.now(UTC) - timedelta(minutes=1)})
    assert (await w1.post(CHECK, json={"code": "state-1"})).status_code == 404
    assert telegram.calls == [] and postiz.completed == []


async def test_telegram_updates_scrubs_the_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("offset") == "8":
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 8}]})
        return httpx.Response(409, json={"ok": False, "description": f"conflict bot{TOKEN}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await telegram_updates(TOKEN, offset=8, client=http) == [{"update_id": 8}]
        with pytest.raises(PostizError) as caught:
            await telegram_updates(TOKEN, client=http)
    assert json.loads(seen[0].url.params["allowed_updates"]) == ["message", "channel_post"]
    assert caught.value.code == "telegram.http_error" and TOKEN not in caught.value.message


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_sign_jwt_is_hs256():
    token = sign_jwt({"id": "w1:p1", "name": "Acme"}, "s3cret")
    header, body, sig = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "HS256", "typ": "JWT"}
    assert json.loads(_unb64(body)) == {"id": "w1:p1", "name": "Acme"}
    expected = hmac.digest(b"s3cret", f"{header}.{body}".encode(), hashlib.sha256)
    assert _unb64(sig) == expected


async def test_create_org_refusal_is_an_error_without_secrets():
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"create": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PostizClient("http://postiz.test/", "s3cret", client=http)
        with pytest.raises(PostizError) as caught:
            await client.create_org("w1:p1", "Acme")
    assert caught.value.code == "postiz.create_failed"
    assert "s3cret" not in repr(client)
    claims = json.loads(_unb64(seen[0]["params"].split(".")[1]))
    assert claims == {"id": "w1:p1", "name": "Acme", "saasName": "PocketPaw", "email": ""}
