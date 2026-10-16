# tests/cloud/notifications/test_whatsapp_notify.py
# The site-owner WhatsApp provider seam (``notifications.whatsapp_notify``), with
# no database and no network: provider selection (mock without credentials),
# the Meta request body for text / template / hello_world, the URL and Bearer
# header, Meta error mapping, and that the token never shows in a repr or an
# error string. HTTP goes through an httpx MockTransport; Meta is never called.

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pocketpaw_ee.cloud.notifications import whatsapp_notify as wn

TOKEN = "EAAG-secret-token-123"
PHONE_ID = "109876543210"
_ENV = (
    "POCKETPAW_WA_NOTIFY_ACCESS_TOKEN",
    "POCKETPAW_WA_NOTIFY_PHONE_NUMBER_ID",
    "POCKETPAW_WA_NOTIFY_SEND_AS",
    "POCKETPAW_WA_NOTIFY_TEMPLATE",
    "POCKETPAW_WA_NOTIFY_TEMPLATE_LANG",
    "POCKETPAW_WA_NOTIFY_API_VERSION",
)


@pytest.fixture
def wa_env(monkeypatch):
    """Set WhatsApp notify env vars (and clear the rest) for one test."""
    from pocketpaw.config import get_settings

    def apply(**values: str) -> None:
        for key in _ENV:
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(f"POCKETPAW_WA_NOTIFY_{key.upper()}", value)
        get_settings.cache_clear()

    apply()
    yield apply
    get_settings.cache_clear()


def _client(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_mock_without_credentials(wa_env) -> None:
    assert wn.load_config() is None and wn.mode() == "mock"
    assert isinstance(wn.get_provider(), wn.MockProvider)
    wa_env(access_token=TOKEN)  # a token alone isn't enough
    assert wn.mode() == "mock"
    wa_env(phone_number_id=PHONE_ID)
    assert wn.mode() == "mock"
    wa_env(access_token=TOKEN, phone_number_id="not-a-number")
    assert wn.mode() == "mock"


def test_live_with_token_and_phone_id(wa_env) -> None:
    wa_env(access_token=TOKEN, phone_number_id=PHONE_ID)
    config = wn.load_config()
    assert wn.mode() == "live" and isinstance(wn.get_provider(), wn.MetaCloudProvider)
    assert config.send_as == "template" and config.template == "hello_world"
    assert config.template_lang == "en_US" and config.api_version == wn.DEFAULT_API_VERSION
    wa_env(access_token=TOKEN, phone_number_id=PHONE_ID, send_as="nonsense", api_version="x")
    config = wn.load_config()
    assert config.send_as == "template" and config.api_version == wn.DEFAULT_API_VERSION


def test_token_never_in_repr(wa_env) -> None:
    wa_env(access_token=TOKEN, phone_number_id=PHONE_ID)
    config = wn.load_config()
    assert TOKEN not in repr(config) and TOKEN not in str(config)
    assert TOKEN not in repr(wn.get_provider())


async def test_mock_logs_masked_number_and_reports_mock(wa_env, caplog) -> None:
    caplog.set_level(logging.INFO, logger=wn.__name__)
    result = await wn.send("+919876543210", "hello", client=None)
    assert result == wn.SendResult("sent", provider="mock")
    assert "+91******3210" in caplog.text and "9876543210" not in caplog.text


def _config(**kw) -> wn.WaNotifyConfig:
    return wn.WaNotifyConfig(access_token=TOKEN, phone_number_id=PHONE_ID, **kw)


def test_text_payload() -> None:
    body = wn.MetaCloudProvider(_config(send_as="text")).payload("+14155550123", "New lead")
    assert body == {
        "messaging_product": "whatsapp",
        "to": "14155550123",
        "type": "text",
        "text": {"body": "New lead", "preview_url": False},
    }


def test_template_payload_passes_the_text_as_body_parameter_1() -> None:
    provider = wn.MetaCloudProvider(_config(template="paw_new_lead", template_lang="en"))
    assert provider.payload("+14155550123", "New lead") == {
        "messaging_product": "whatsapp",
        "to": "14155550123",
        "type": "template",
        "template": {
            "name": "paw_new_lead",
            "language": {"code": "en"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "New lead"}]}],
        },
    }


def test_hello_world_has_no_variables() -> None:
    body = wn.MetaCloudProvider(_config()).payload("+14155550123", "ignored")
    assert body["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


async def test_meta_post_url_and_bearer() -> None:
    seen: list[httpx.Request] = []
    async with _client([httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})], seen) as c:
        result = await wn.MetaCloudProvider(_config(api_version="v26.0")).send(
            "+14155550123", "hi", client=c
        )
    assert result == wn.SendResult("sent", provider="meta")
    [req] = seen
    assert str(req.url) == f"https://graph.facebook.com/v26.0/{PHONE_ID}/messages"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content)["to"] == "14155550123"


def _err(code: int) -> dict:
    return {"error": {"message": f"boom {TOKEN}", "type": "OAuthException", "code": code}}


@pytest.mark.parametrize(
    ("status", "body", "expected", "fragment"),
    [
        (400, _err(131047), "dead", "24h window"),
        (400, _err(131030), "dead", "allowed list"),
        (401, _err(190), "dead", "access token"),
        (400, _err(100), "dead", "meta http 400 code 100"),
        (404, None, "dead", "meta http 404"),
        (429, _err(130429), "retry", "meta http 429"),
        (400, _err(130429), "retry", "code 130429"),
        (400, _err(131056), "retry", "code 131056"),
        (500, None, "retry", "meta http 500"),
        (503, _err(2), "retry", "meta http 503"),
    ],
)
async def test_meta_error_mapping(status, body, expected, fragment) -> None:
    seen: list[httpx.Request] = []
    async with _client([httpx.Response(status, json=body)], seen) as c:
        result = await wn.MetaCloudProvider(_config()).send("+14155550123", "hi", client=c)
    assert result.status == expected and result.provider == "meta"
    assert fragment in result.error
    assert TOKEN not in result.error and "boom" not in result.error


async def test_transport_error_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        result = await wn.MetaCloudProvider(_config()).send("+14155550123", "hi", client=c)
    assert result.status == "retry" and "ConnectError" in result.error


def test_event_text_is_one_line_and_defanged() -> None:
    text = wn.event_text(
        site_name="Bright Smile",
        title="Needs a human",
        body="Call me *now*\n see https://evil.example",
    )
    assert text == (
        "Needs a human on Bright Smile via Paw Sites by PocketPaw: "
        "Call me now see hxxps://evil.example"
    )
    assert len(wn.event_text(site_name="S", title="T", body="x" * 5000)) <= 900
