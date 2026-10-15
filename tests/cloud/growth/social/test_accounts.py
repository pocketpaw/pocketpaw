# tests/cloud/growth/social/test_accounts.py — Growth › Social accounts
# (``/api/v1/growth/social/accounts``) over the test_router harness, with a fake
# Postiz installed through ``set_production_postiz``. Covers the unconfigured
# deployment, one org per profile, the server-built OAuth return URL, platform
# errors, tenant and profile isolation, disconnect, the encrypted key, the
# Telegram code flow (a fake Bot API read through
# ``set_production_telegram_updates``, including the cross-workspace sweep),
# posting now (Slack channels, X settings, thread parts, media on the first entry,
# the Slack video skip, the idea link, every refusal before Postiz is called, the
# posts list and tenant isolation), media upload (sniffing, size, the private-path
# refusal), and the Postiz client's JWT signer, create-org failure, upload and
# the Bot API read's token scrubbing (``httpx.MockTransport``).

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
from pocketpaw_ee.cloud.models.social_idea import SocialIdea
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg

from tests.cloud.growth.social.test_router import BASE, _client

ACCOUNTS = f"{BASE}/accounts"
CONNECT = f"{ACCOUNTS}/connect"
CHECK = f"{ACCOUNTS}/telegram/check"
POSTS = f"{BASE}/posts"
MEDIA = f"{BASE}/media"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
MP4 = b"\0\0\0\x18ftypmp42" + b"\0" * 32
CDN = "https://cdn.paw.test/uploads"
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
        self.triggered: list[tuple[str, str, str]] = []
        self.channels: list[dict[str, Any]] = [{"id": "C1", "name": "social"}]
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[dict[str, Any]] = []
        self.listed: list[tuple[str, str, str]] = []
        self.uploads: list[tuple[str, str, str, int]] = []
        self.media_base = CDN

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

    async def trigger_tool(
        self, api_key: str, integration_id: str, method: str, data: dict[str, Any] | None = None
    ) -> Any:
        self.triggered.append((api_key, integration_id, method))
        return self.channels

    async def upload(
        self, api_key: str, filename: str, data: bytes, content_type: str
    ) -> dict[str, str]:
        self.uploads.append((api_key, filename, content_type, len(data)))
        return {"id": f"m-{len(self.uploads)}", "path": f"{self.media_base}/{filename}"}

    async def create_posts(self, api_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self.posted.append((api_key, payload))
        return [
            {"postId": f"post-{p['integration']['id']}", "integration": p["integration"]["id"]}
            for p in payload["posts"]
        ]

    async def list_posts(self, api_key: str, start: str, end: str) -> list[dict[str, Any]]:
        self.listed.append((api_key, start, end))
        return list(self.posts)


def _integration(account_id: str, identifier: str = "x") -> dict[str, Any]:
    return {
        "id": account_id,
        "name": "Acme",
        "identifier": identifier,
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
    platforms = (await w1.get(ACCOUNTS)).json()["platforms"]
    assert len(platforms) == 13 and platforms[0] == {"id": "x", "label": "X", "available": True}
    assert [p["id"] for p in platforms if p["available"]] == ["x"]
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
            "can_post": True,
            "needs_channel": False,
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
            "can_post": False,
            "needs_channel": False,
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


async def _connected(client: httpx.AsyncClient, postiz: _FakePostiz, key: str = "key-1") -> None:
    await _profile(client)
    await client.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
    postiz.integrations[key] = [
        _integration("sl-1", "slack"),
        _integration("x-1"),
        _integration("li-1", "linkedin"),
    ]


async def test_accounts_say_whether_they_can_post(w1, postiz):
    await _connected(w1, postiz)
    items = (await w1.get(ACCOUNTS)).json()["items"]
    assert [(a["id"], a["can_post"], a["needs_channel"]) for a in items] == [
        ("sl-1", True, True),
        ("x-1", True, False),
        ("li-1", False, False),
    ]


async def test_channels(w1, postiz):
    await _connected(w1, postiz)
    resp = await w1.get(f"{ACCOUNTS}/sl-1/channels")
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": "C1", "name": "social"}]}
    assert postiz.triggered == [("key-1", "sl-1", "channels")]

    resp = await w1.get(f"{ACCOUNTS}/x-1/channels")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.channels_unsupported"
    resp = await w1.get(f"{ACCOUNTS}/nope/channels")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_account.not_found"
    assert len(postiz.triggered) == 1


async def test_post_now_sends_one_payload_and_maps_the_result(w1, postiz):
    await _connected(w1, postiz)
    body = {"content": "  Ship day!  ", "targets": [{"account_id": "sl-1", "channel": "C1"}]}
    resp = await w1.post(POSTS, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "items": [
            {
                "account_id": "sl-1",
                "platform": "slack",
                "post_id": "post-sl-1",
                "media_skipped": False,
            }
        ]
    }
    [(key, payload)] = postiz.posted
    sent = datetime.fromisoformat(payload.pop("date").replace("Z", "+00:00"))
    assert abs(datetime.now(UTC) - sent) < timedelta(seconds=10)
    assert key == "key-1"
    assert payload == {
        "type": "now",
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": "sl-1"},
                "value": [{"content": "Ship day!", "image": []}],
                "settings": {"__type": "slack", "channel": "C1"},
            }
        ],
    }


SLACK: dict[str, Any] = {"content": "hi", "targets": [{"account_id": "sl-1", "channel": "C1"}]}


def _media(name: str, base: str = CDN) -> dict[str, str]:
    return {"id": f"id-{name}", "path": f"{base}/{name}"}


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (
            {"content": "hi", "targets": [{"account_id": "li-1"}]},
            422,
            "social.post_platform_unsupported",
        ),
        (
            {"content": "hi", "targets": [{"account_id": "sl-1", "channel": " "}]},
            422,
            "social.channel_required",
        ),
        (
            {
                "content": "hi",
                "targets": [{"account_id": "sl-1", "channel": "C1"}, {"account_id": "nope"}],
            },
            404,
            "social_account.not_found",
        ),
        ({"content": "   ", "targets": [{"account_id": "sl-1", "channel": "C1"}]}, 422, None),
        ({"content": "x" * 10001, "targets": [{"account_id": "sl-1", "channel": "C1"}]}, 422, None),
        ({"content": "hi", "targets": []}, 422, None),
        ({"content": "hi", "targets": [{"account_id": "sl-1", "channel": "C1"}] * 2}, 422, None),
        ({**SLACK, "parts": ["ok", "  "]}, 422, None),
        ({**SLACK, "parts": ["p"] * 26}, 422, None),
        ({**SLACK, "media": [_media("a.png")] * 5}, 422, None),
        ({**SLACK, "media": [_media("a.mp4"), _media("b.png")]}, 422, "social.media_mixed"),
        ({**SLACK, "media": [_media("a.mp4"), _media("b.mp4")]}, 422, "social.media_mixed"),
        *[
            ({**SLACK, "media": [_media("a.png", base)]}, 422, "social.media_not_public")
            for base in (
                "http://127.0.0.1:4007/uploads",
                "http://localhost:4007/uploads",
                "https://10.0.0.5",
                "http://192.168.1.2",
                "http://169.254.169.254",
                "http://[::1]",
                "http://postiz.internal",
                "file:///var/uploads",
            )
        ],
    ],
)
async def test_post_now_refusals_never_reach_postiz(w1, postiz, body, status, code):
    await _connected(w1, postiz)
    resp = await w1.post(POSTS, json=body)
    assert resp.status_code == status, resp.text
    if code:
        assert resp.json()["error"]["code"] == code
    assert postiz.posted == []


async def test_post_now_names_the_platform_it_cannot_post_to(w1, postiz):
    await _connected(w1, postiz)
    resp = await w1.post(POSTS, json={"content": "hi", "targets": [{"account_id": "li-1"}]})
    assert resp.json()["error"]["message"] == "Posting to LinkedIn isn't supported yet."


async def test_post_to_x_and_slack_with_a_thread_and_images(w1, postiz):
    await _connected(w1, postiz)
    images = [_media("a.png"), _media("b.jpg")]
    body = {**SLACK, "targets": [*SLACK["targets"], {"account_id": "x-1"}]}
    body |= {"parts": [" two ", "three"], "media": images}
    resp = await w1.post(POSTS, json=body)
    assert resp.status_code == 200, resp.text
    assert [i["media_skipped"] for i in resp.json()["items"]] == [False, False]
    [(_, payload)] = postiz.posted
    value = [
        {"content": "hi", "image": images},
        {"content": "two", "image": []},
        {"content": "three", "image": []},
    ]
    assert payload["posts"] == [
        {
            "integration": {"id": "sl-1"},
            "value": value,
            "settings": {"__type": "slack", "channel": "C1"},
        },
        {
            "integration": {"id": "x-1"},
            "value": value,
            "settings": {"__type": "x", "who_can_reply_post": "everyone"},
        },
    ]


async def test_video_skips_platforms_that_cannot_take_it(w1, postiz):
    await _connected(w1, postiz)
    video = _media("clip.MP4")
    body = {**SLACK, "targets": [*SLACK["targets"], {"account_id": "x-1"}], "media": [video]}
    resp = await w1.post(POSTS, json=body)
    assert resp.status_code == 200, resp.text
    skipped = {i["account_id"]: i["media_skipped"] for i in resp.json()["items"]}
    assert skipped == {"sl-1": True, "x-1": False}
    [(_, payload)] = postiz.posted
    assert [p["value"][0]["image"] for p in payload["posts"]] == [[], [video]]


async def test_post_links_only_an_idea_of_this_profile(w1, w2, postiz):
    await _connected(w1, postiz)
    pid = (await w1.get(f"{BASE}/profile")).json()["id"]
    own = await SocialIdea(workspace="w1", profile=pid, format="x_post", hook="h").insert()
    other_profile = await SocialIdea(
        workspace="w1", profile="p9", format="x_post", hook="h"
    ).insert()
    foreign = await SocialIdea(workspace="w2", profile=pid, format="x_post", hook="h").insert()
    for idea_id in (str(foreign.id), str(other_profile.id), "nope"):
        resp = await w1.post(POSTS, json={**SLACK, "idea_id": idea_id})
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "social_idea.not_found"
    assert postiz.posted == []
    resp = await w1.post(POSTS, json={**SLACK, "idea_id": str(own.id)})
    assert resp.status_code == 200, resp.text
    assert "idea_id" not in json.dumps(postiz.posted)


async def test_upload_media(w1, postiz):
    pid = await _profile(w1)
    resp = await w1.post(MEDIA, params={"profile_id": pid}, files={"file": ("x.bin", PNG)})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "m-1", "path": f"{CDN}/media.png", "kind": "image"}
    resp = await w1.post(MEDIA, files={"file": ("x.png", MP4, "image/png")})
    assert resp.json() == {"id": "m-2", "path": f"{CDN}/media.mp4", "kind": "video"}
    assert postiz.created == [(f"w1:{pid}", "Acme")]
    assert postiz.uploads == [
        ("key-1", "media.png", "image/png", len(PNG)),
        ("key-1", "media.mp4", "video/mp4", len(MP4)),
    ]


async def test_upload_refusals(w1, postiz, monkeypatch):
    await _profile(w1)
    resp = await w1.post(MEDIA, files={"file": ("a.png", b"just text")})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.media_unsupported_type"
    postiz.media_base = "http://127.0.0.1:4007/uploads"
    resp = await w1.post(MEDIA, files={"file": ("a.png", PNG)})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.media_not_public"
    assert "Cloudflare R2" in resp.json()["error"]["message"]

    for module in ("service", "router"):
        monkeypatch.setattr(f"pocketpaw_ee.cloud.growth.social.{module}.MAX_MEDIA_BYTES", 16)
    resp = await w1.post(MEDIA, files={"file": ("a.png", PNG)})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.media_too_large"
    assert len(postiz.uploads) == 1


async def test_another_workspace_cannot_upload_into_a_profile(w1, w2, postiz):
    pid = await _profile(w1)
    await _profile(w2)
    resp = await w2.post(MEDIA, params={"profile_id": pid}, files={"file": ("a.png", PNG)})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_profile.not_found"
    assert postiz.uploads == [] and len(postiz.created) == 0


def _post(post_id: str, state: str, when: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": post_id,
        "content": f"body {post_id}",
        "publishDate": when,
        "releaseURL": extra.pop("url", None),
        "state": state,
        "integration": {"id": "sl-1", "providerIdentifier": "slack", "name": "Acme HQ"},
        **extra,
    }


async def test_posts_list(w1, postiz):
    await _profile(w1)
    assert (await w1.get(POSTS)).json() == {"items": []}
    assert postiz.listed == [] and postiz.created == []

    await w1.post(CONNECT, json={"platform": "x"}, headers=LOCAL)
    postiz.posts = [
        _post("p1", "PUBLISHED", "2026-10-14T08:00:00.000Z", url="https://slack.example/p1"),
        _post("p2", "ERROR", "2026-10-15T08:00:00.000Z", error="channel_not_found"),
        _post("p3", "QUEUE", "2026-10-15T09:00:00.000Z"),
        _post("p4", "ODD", "2026-10-13T09:00:00.000Z"),
    ]
    items = (await w1.get(POSTS)).json()["items"]
    assert [(i["id"], i["state"]) for i in items] == [
        ("p3", "queued"),
        ("p2", "error"),
        ("p1", "published"),
        ("p4", "odd"),
    ]
    assert items[2] == {
        "id": "p1",
        "account_id": "sl-1",
        "platform": "slack",
        "account_name": "Acme HQ",
        "content": "body p1",
        "state": "published",
        "url": "https://slack.example/p1",
        "error": None,
        "published_at": "2026-10-14T08:00:00.000Z",
    }
    assert items[1]["error"] == "channel_not_found" and items[1]["published_at"] is None
    _, start, end = postiz.listed[-1]
    span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    assert span == timedelta(days=8)

    picked = (await w1.get(POSTS, params={"ids": "p1, p3,missing"})).json()["items"]
    assert [i["id"] for i in picked] == ["p3", "p1"]


async def test_posts_need_postiz(w1, monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(postiz_url=None, postiz_jwt_secret=None, postiz_platforms=None),
    )
    await _profile(w1)
    for resp in (
        await w1.get(POSTS),
        await w1.post(POSTS, json={"content": "hi", "targets": [{"account_id": "sl-1"}]}),
        await w1.get(f"{ACCOUNTS}/sl-1/channels"),
    ):
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "social.accounts_unavailable"


async def test_post_upstream_failure_is_502(w1, postiz):
    await _connected(w1, postiz)

    async def boom(api_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raise PostizError("postiz.http_error", "boom")

    postiz.create_posts = boom  # type: ignore[method-assign]
    body = {"content": "hi", "targets": [{"account_id": "sl-1", "channel": "C1"}]}
    resp = await w1.post(POSTS, json=body)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "social.accounts_upstream"


async def test_another_workspace_cannot_post_or_list_channels(w1, w2, postiz):
    await _connected(w1, postiz)
    other = await _profile(w2)
    await SocialPostizOrg(
        workspace="w1", profile=other, postiz_org_id="org-1", api_key_enc=encrypt("key-1")
    ).insert()

    resp = await w2.get(f"{ACCOUNTS}/sl-1/channels")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_account.not_found"
    body = {"content": "hi", "targets": [{"account_id": "sl-1", "channel": "C1"}]}
    resp = await w2.post(POSTS, json=body)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_account.not_found"
    assert (await w2.get(POSTS)).json() == {"items": []}
    assert postiz.triggered == [] and postiz.posted == [] and postiz.listed == []


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


async def test_client_upload_is_multipart():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(201, json={"id": "m1", "path": f"{CDN}/m1.png"})
        return httpx.Response(201, json={"id": "m2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PostizClient("http://postiz.test/", "s3cret", client=http)
        assert await client.upload("key-9", "media.png", PNG, "image/png") == {
            "id": "m1",
            "path": f"{CDN}/m1.png",
        }
        with pytest.raises(PostizError) as caught:
            await client.upload("key-9", "media.png", PNG, "image/png")
    assert caught.value.code == "postiz.upload_failed"
    assert seen[0].url.path == "/api/public/v1/upload"
    assert seen[0].headers["authorization"] == "key-9"
    assert seen[0].headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"; filename="media.png"' in seen[0].content and PNG in seen[0].content


async def test_client_publishing_calls_hit_the_public_api():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/integration-trigger/sl-1"):
            return httpx.Response(200, json={"output": [{"id": "C1", "name": "social"}]})
        if request.method == "POST":
            return httpx.Response(201, json=[{"postId": "p1", "integration": "sl-1"}])
        if request.url.params.get("startDate") == "bad":
            return httpx.Response(500, text="no key-9 for you")
        return httpx.Response(200, json={"posts": [{"id": "p1"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PostizClient("http://postiz.test/", "s3cret", client=http)
        assert await client.trigger_tool("key-9", "sl-1", "channels") == [
            {"id": "C1", "name": "social"}
        ]
        assert await client.create_posts("key-9", {"type": "now"}) == [
            {"postId": "p1", "integration": "sl-1"}
        ]
        assert await client.list_posts("key-9", "2026-10-08", "2026-10-16") == [{"id": "p1"}]
        with pytest.raises(PostizError) as caught:
            await client.list_posts("key-9", "bad", "2026-10-16")
    assert [(r.method, r.url.path) for r in seen[:3]] == [
        ("POST", "/api/public/v1/integration-trigger/sl-1"),
        ("POST", "/api/public/v1/posts"),
        ("GET", "/api/public/v1/posts"),
    ]
    assert json.loads(seen[0].content) == {"methodName": "channels", "data": {}}
    assert seen[2].url.params["endDate"] == "2026-10-16"
    assert all(r.headers["authorization"] == "key-9" for r in seen)
    assert "key-9" not in caught.value.message
