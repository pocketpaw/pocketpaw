# tests/cloud/notifications/test_email.py
# The Cloudflare Email Service REST client and the owner-email templates. The
# client runs for real against an httpx MockTransport: request shape (REST names
# ``from.address`` / ``reply_to``), 2xx sent, 429/5xx retry, 400/401 permanent,
# and permanent bounces reported back. Templates: every visitor value is
# HTML-escaped, both html and text are present, reply_to is the lead's email
# only when it is a plausible address.

from __future__ import annotations

import json

import httpx
import pytest
from pocketpaw_ee.cloud.notifications import email as email_mod

CONFIG = email_mod.EmailConfig("acct123", "tok_secret", "notify@paw.example", "Paw Alerts")
MESSAGE = email_mod.EmailMessage(
    to=["owner@example.com"],
    subject="New lead",
    html="<p>hi</p>",
    text="hi",
    reply_to="visitor@example.org",
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


async def test_success_sends_rest_shape_and_reads_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(
            {
                "delivered": ["owner@example.com"],
                "queued": [],
                "permanent_bounces": [],
                "message_id": "<m1@x>",
            }
        )

    async with _client(handler) as client:
        result = await email_mod.send_email(MESSAGE, config=CONFIG, client=client)

    assert result.outcome == "sent"
    assert result.delivered == ["owner@example.com"]
    assert result.message_id == "<m1@x>"
    req = seen[0]
    assert str(req.url) == (
        "https://api.cloudflare.com/client/v4/accounts/acct123/email/sending/send"
    )
    assert req.headers["authorization"] == "Bearer tok_secret"
    body = json.loads(req.content)
    assert body["from"] == {"address": "notify@paw.example", "name": "Paw Alerts"}
    assert body["reply_to"] == "visitor@example.org"
    assert "replyTo" not in body
    assert body["to"] == ["owner@example.com"]
    assert body["html"] and body["text"]


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_rate_limit_and_server_errors_are_retryable(status) -> None:
    def handler(_request):
        return httpx.Response(
            status, json={"success": False, "errors": [{"code": 10004, "message": "throttled"}]}
        )

    async with _client(handler) as client:
        result = await email_mod.send_email(MESSAGE, config=CONFIG, client=client)
    assert result.outcome == "retry"
    assert result.status_code == status


async def test_transport_error_is_retryable() -> None:
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    async with _client(handler) as client:
        result = await email_mod.send_email(MESSAGE, config=CONFIG, client=client)
    assert result.outcome == "retry"


@pytest.mark.parametrize("status", [400, 422])
async def test_client_errors_are_permanent(status) -> None:
    def handler(_request):
        return httpx.Response(
            status,
            json={
                "success": False,
                "errors": [
                    {"code": 10001, "message": "email.sending.error.invalid_request_schema"}
                ],
            },
        )

    async with _client(handler) as client:
        result = await email_mod.send_email(MESSAGE, config=CONFIG, client=client)
    assert result.outcome == "permanent"
    assert "10001" in result.error


async def test_permanent_bounces_are_reported() -> None:
    def handler(_request):
        return _ok({"delivered": [], "queued": [], "permanent_bounces": ["owner@example.com"]})

    async with _client(handler) as client:
        result = await email_mod.send_email(MESSAGE, config=CONFIG, client=client)
    assert result.outcome == "sent"
    assert result.permanent_bounces == ["owner@example.com"]


async def test_unconfigured_sink_never_calls_out(monkeypatch) -> None:
    monkeypatch.setattr(email_mod, "load_config", lambda: None)
    assert email_mod.is_configured() is False
    result = await email_mod.send_email(MESSAGE)
    assert result.outcome == "permanent"


def test_load_config_requires_all_three(monkeypatch) -> None:
    from types import SimpleNamespace

    import pocketpaw.config as config_mod

    partial = SimpleNamespace(
        cf_email_account_id="a", cf_email_api_token="t", cf_email_from="", cf_email_from_name=""
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: partial)
    assert email_mod.load_config() is None
    full = SimpleNamespace(
        cf_email_account_id="a",
        cf_email_api_token="t",
        cf_email_from="n@x.io",
        cf_email_from_name="",
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: full)
    assert email_mod.load_config() == email_mod.EmailConfig("a", "t", "n@x.io", "PocketPaw")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

EVIL = '<script>alert("x")</script>'


def _lead(**props) -> dict:
    return {
        "id": "lead1",
        "site_id": "site1",
        "site_name": "Bright <b>Smile</b>",
        "form_type": "lead",
        "properties": props,
        "source": {"kind": "concierge"},
    }


def test_lead_email_escapes_every_visitor_value() -> None:
    out = email_mod.render_lead_email(
        _lead(
            full_name=EVIL,
            email="priya@x.com",
            phone="555 0101 222",
            message=f"hello {EVIL}",
            budget=EVIL,
        )
    )
    assert "<script>" not in out.html
    assert "&lt;script&gt;" in out.html
    assert "<b>Smile</b>" not in out.html
    # text part carries the raw values (plain text is not HTML)
    assert "priya@x.com" in out.text and "555 0101 222" in out.text
    assert "budget" in out.html  # extra properties are listed too
    # links: the lead in paw-enterprise and the settings footer
    assert "/sites/site1?view=leads&amp;lead=lead1" in out.html
    assert "/sites/site1?view=leads&lead=lead1" in out.text
    assert "mode=more&amp;section=notifications" in out.html
    assert "Change notification settings" in out.text
    assert "\n" not in out.subject


def test_lead_email_reply_to_is_the_lead_email_when_valid() -> None:
    assert email_mod.render_lead_email(_lead(email="priya@x.com")).reply_to == "priya@x.com"
    # alias field names resolve too (imported forms)
    assert email_mod.render_lead_email(_lead(EmailAddress="a@b.co")).reply_to == "a@b.co"
    assert email_mod.render_lead_email(_lead(email="not-an-email")).reply_to is None
    assert email_mod.render_lead_email(_lead(email="a@b.co\r\nBcc: x@y.z")).reply_to is None
    assert email_mod.render_lead_email(_lead(phone="5550101222")).reply_to is None


def test_subject_strips_newlines_from_visitor_name() -> None:
    out = email_mod.render_lead_email(_lead(full_name="Eve\r\nBcc: x@y.z"))
    assert "\r" not in out.subject and "\n" not in out.subject


def test_confirm_and_test_emails_have_both_parts() -> None:
    confirm = email_mod.render_confirm_email(
        site_name=EVIL, confirm_url="https://api.x/confirm/abc", footer_url="https://app.x/s"
    )
    assert "<script>" not in confirm.html
    assert (
        "https://api.x/confirm/abc" in confirm.html and "https://api.x/confirm/abc" in confirm.text
    )
    test = email_mod.render_test_email(site_name="Acme", footer_url="https://app.x/s")
    assert test.html and test.text and "Acme" in test.subject


@pytest.mark.parametrize(
    "status,code",
    [(401, 10101), (403, 10102), (403, 10203)],  # 10203: sending_disabled
)
async def test_auth_and_sending_disabled_are_retryable_and_flagged(status, code) -> None:
    def handler(_request):
        return httpx.Response(
            status, json={"success": False, "errors": [{"code": code, "message": "x"}]}
        )

    async with _client(handler) as client:
        result = await email_mod.send_email(MESSAGE, config=CONFIG, client=client)
    assert result.outcome == "retry"
    assert result.auth_failure is True
