# tests/ee/sites/test_site_ai_chat.py: POST /sites/{site_id}/ai/chat for Worker callers.
#
# Covers the SSE and JSON shapes, the gate order and its error bodies, request
# clamps (messages, body size, images by magic bytes, image URL hosts, tools,
# context), plan/owner model rules, presets, the paused state, retry-before-first-
# token, mid-stream failure, run bookkeeping, and that the provider model id, the
# caller's ``user`` and provider error text never leave the server. The LLM is
# faked (``FakeLlm``); no network.
from __future__ import annotations

import base64
import logging

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet

from tests.ee.sites.site_ai_harness import (
    ORIGIN,
    SITE_KEY,
    WS,
    FakeLlm,
    client,
    gateway_token,
    make_pocket,
    make_site,
    parse_sse,
    text_events,
)

PROVIDER_MODEL = "claude-haiku-4-5"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.site_ai import chat

    from pocketpaw.security.rate_limiter import RateLimiter

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(chat, "_ip_limiter", RateLimiter(rate=1000.0, capacity=1000))
    monkeypatch.setattr(chat, "_schedule_bill", lambda run_id: None)


async def _setup(plan: str = "staff", models: list[str] | None = None):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiSettingsDoc

    pid = await make_pocket()
    site = await make_site(pid, plan_tier=plan)
    if models is not None:
        await SiteAiSettingsDoc(workspace=WS, pocket_id=pid, allowed_models=models).insert()
    return site, await gateway_token(site)


def _msgs(text: str = "hi"):
    return [{"role": "user", "content": text}]


async def _post(c, site, token, body, headers=None):
    hdrs = {"authorization": f"Bearer {token}"} if token else {}
    hdrs.update(headers or {})
    return await c.post(f"/api/v1/sites/{site.id}/ai/chat", json=body, headers=hdrs)


@pytest.mark.asyncio
async def test_worker_stream_relays_chunks_usage_done_and_records_the_run(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=[text_events("Hello there", 10, 5)])
    async with client() as c:
        resp = await _post(c, site, token, {"model": "fast", "messages": _msgs(), "user": "evil"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert [e for e, _ in events] == ["chunk", "usage", "done"]
    assert events[0][1] == {"delta": "Hello there"}
    assert events[1][1]["input_tokens"] == 10 and events[1][1]["output_tokens"] == 5
    assert events[1][1]["credits_micro"] > 0
    assert events[2][1]["finish_reason"] == "stop"
    assert PROVIDER_MODEL not in resp.text

    payload = fake.payloads[0]
    assert payload["model"] == PROVIDER_MODEL
    assert payload["user"] == WS  # the paying workspace, never the caller's ``user``
    assert f"site:{site.id}" in payload["extra_body"]["metadata"]["tags"]
    assert payload["max_tokens"] == 4096

    run = await ChatRunDoc.find_one(ChatRunDoc.context_type == "site_ai")
    assert run is not None and run.status == "completed" and run.scope_id == site.pocket_id
    assert run.usage["input_tokens"] == 10 and run.partial_text == "" and run.user_text == ""
    call = await SiteAiCallDoc.find_one(SiteAiCallDoc.run_id == run.run_id)
    assert call.state == "settled" and call.model == "fast" and call.caller == "worker"
    assert call.actual_micro == events[1][1]["credits_micro"]


@pytest.mark.asyncio
async def test_json_mode_returns_chat_response_with_tool_calls(beanie_test_db, monkeypatch):
    from pocketpaw_ee.site_ai.llm import LlmResult

    site, token = await _setup()
    call = {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    FakeLlm(
        monkeypatch,
        result=LlmResult(None, [call], "tool_calls", input_tokens=20, output_tokens=3),
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    async with client() as c:
        resp = await _post(
            c,
            site,
            token,
            {"model": "fast", "messages": _msgs(), "stream": False, "tools": tools},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "fast" and body["id"].startswith("aic_")
    assert body["message"] == {"role": "assistant", "content": None, "tool_calls": [call]}
    assert body["finish_reason"] == "tool_calls"
    assert body["usage"]["input_tokens"] == 20 and body["usage"]["credits_micro"] > 0
    assert PROVIDER_MODEL not in resp.text


@pytest.mark.asyncio
async def test_stream_relays_tool_call_deltas(beanie_test_db, monkeypatch):
    from pocketpaw_ee.site_ai.llm import LlmEvent

    site, token = await _setup()
    FakeLlm(
        monkeypatch,
        script=[
            [
                LlmEvent("tool", index=0, id="c1", name="lookup", arguments='{"q":'),
                LlmEvent("tool", index=0, arguments='"x"}'),
                LlmEvent("finish", reason="tool_calls"),
                LlmEvent("usage", input_tokens=5, output_tokens=4),
            ]
        ],
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    async with client() as c:
        resp = await _post(c, site, token, {"model": "fast", "messages": _msgs(), "tools": tools})
    events = parse_sse(resp.text)
    assert events[0] == (
        "tool_call",
        {"index": 0, "id": "c1", "name": "lookup", "arguments_delta": '{"q":'},
    )
    assert events[1] == ("tool_call", {"index": 0, "arguments_delta": '"x"}'})
    assert events[-1][1]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_auth_gates_and_flat_error_bodies(beanie_test_db, monkeypatch):
    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events()])
    body = {"model": "fast", "messages": _msgs()}
    async with client() as c:
        none = await _post(c, site, None, body)
        bad = await _post(c, site, "nope", body)
        browser = await _post(c, site, None, body, {"x-paw-site-key": SITE_KEY, "origin": ORIGIN})
        evil = await _post(
            c, site, None, body, {"x-paw-site-key": SITE_KEY, "origin": "https://evil.test"}
        )
    assert none.status_code == 401
    assert none.json() == {"detail": "unauthorized", "message": none.json()["message"]}
    assert bad.status_code == 401
    assert (browser.status_code, browser.json()["detail"]) == (403, "browser_disabled")
    assert (evil.status_code, evil.json()["detail"]) == (403, "origin_not_allowed")


@pytest.mark.asyncio
async def test_request_clamps(beanie_test_db, monkeypatch):
    site, token = await _setup(models=["fast", "vision"])
    FakeLlm(monkeypatch, script=[text_events()])

    def img(url):
        return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]

    png_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    jpeg_claimed = "data:image/jpeg;base64," + base64.b64encode(PNG).decode()
    svg = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    cases = {
        "model": ({"model": "gpt-4o", "messages": _msgs()}, 400, "invalid_request"),
        "too_many": ({"model": "fast", "messages": _msgs() * 201}, 400, "invalid_request"),
        "no_messages": ({"model": "fast", "messages": []}, 400, "invalid_request"),
        "bad_role": (
            {"model": "fast", "messages": [{"role": "root", "content": "x"}]},
            400,
            "invalid_request",
        ),
        "image_on_fast": ({"model": "fast", "messages": img(png_url)}, 403, "model_not_allowed"),
        "magic_mismatch": ({"model": "vision", "messages": img(jpeg_claimed)}, 400, None),
        "svg": ({"model": "vision", "messages": img(svg)}, 400, None),
        "http": ({"model": "vision", "messages": img("http://a.test/x.png")}, 400, None),
        "private": ({"model": "vision", "messages": img("https://10.0.0.5/x.png")}, 400, None),
        "loopback": ({"model": "vision", "messages": img("https://127.0.0.1/x.png")}, 400, None),
        "localhost": ({"model": "vision", "messages": img("https://localhost/x.png")}, 400, None),
        "metadata": (
            {"model": "vision", "messages": img("https://169.254.169.254/x")},
            400,
            None,
        ),
        "creds": ({"model": "vision", "messages": img("https://u:p@a.test/x.png")}, 400, None),
        "nine_images": (
            {
                "model": "vision",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": png_url}}] * 9,
                    }
                ],
            },
            400,
            None,
        ),
        "tool_choice": (
            {
                "model": "fast",
                "messages": _msgs(),
                "tool_choice": {"type": "function", "function": {"name": "ghost"}},
            },
            400,
            None,
        ),
        "temperature": ({"model": "fast", "messages": _msgs(), "temperature": 3}, 400, None),
        "context": ({"model": "fast", "messages": _msgs("x" * 70_000)}, 400, None),
        "system_image": (
            {
                "model": "vision",
                "messages": [
                    {
                        "role": "system",
                        "content": [{"type": "image_url", "image_url": {"url": png_url}}],
                    },
                    *_msgs(),
                ],
            },
            400,
            None,
        ),
    }
    async with client() as c:
        for name, (body, status, code) in cases.items():
            resp = await _post(c, site, token, body)
            assert resp.status_code == status, (name, resp.text)
            if code:
                assert resp.json()["detail"] == code, name
        big = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            content=b'{"model":"fast","messages":[{"role":"user","content":"'
            + b"x" * (401 * 1024)
            + b'"}]}',
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        )
        not_json = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            content=b"nope",
            headers={"authorization": f"Bearer {token}"},
        )
    assert (big.status_code, big.json()["detail"]) == (413, "request_too_large")
    assert not_json.status_code == 400 and not_json.json()["field"] == "body"


@pytest.mark.asyncio
async def test_vision_images_are_re_encoded_and_unknown_fields_never_forwarded(
    beanie_test_db, monkeypatch
):
    site, token = await _setup(models=["fast", "vision"])
    fake = FakeLlm(monkeypatch, script=[text_events()])
    png_url = "data:image/png;base64,\n" + base64.b64encode(PNG).decode()
    body = {
        "model": "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": png_url, "detail": "high"}},
                ],
                "secret": "x",
            }
        ],
        "max_output_tokens": 100_000,
        "api_base": "https://evil.test",
        "metadata": {"tags": ["spoof"]},
        "user": "visitor-1",
    }
    async with client() as c:
        resp = await _post(c, site, token, body)
    assert resp.status_code == 200, resp.text
    payload = fake.payloads[0]
    assert set(payload) <= {"model", "messages", "max_tokens", "user", "extra_body"}
    assert payload["max_tokens"] == 4096
    assert payload["user"] == WS
    assert payload["extra_body"]["metadata"]["tags"][0] == f"site:{site.id}"
    part = payload["messages"][0]["content"][1]
    assert part == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
    }
    assert "secret" not in payload["messages"][0]


@pytest.mark.asyncio
async def test_plan_and_owner_model_rules(beanie_test_db, monkeypatch):
    FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        site, token = await _setup(plan="site")
        smart = await _post(c, site, token, {"model": "smart", "messages": _msgs()})
        vision = await _post(c, site, token, {"model": "vision", "messages": _msgs()})
        free_site, free_token = await _setup(plan="free")
        free = await _post(c, free_site, free_token, {"model": "fast", "messages": _msgs()})
    assert (smart.status_code, smart.json()["detail"]) == (403, "model_not_allowed")
    # vision is in the plan but the owner hasn't allowed it (default: fast only).
    assert (vision.status_code, vision.json()["detail"]) == (403, "model_not_allowed")
    assert (free.status_code, free.json()["detail"]) == (403, "model_not_allowed")


@pytest.mark.asyncio
async def test_preset_system_model_and_caps_win(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiPresetDoc

    site, token = await _setup(models=["fast", "smart"])
    await SiteAiPresetDoc(
        workspace=WS,
        pocket_id=site.pocket_id,
        preset_id="pre_x",
        name="x",
        system_prompt="You are Ada.",
        model="smart",
        max_output_tokens=200,
        temperature=0.1,
        version=3,
    ).insert()
    fake = FakeLlm(monkeypatch, script=[text_events()])
    body = {
        "model": "fast",
        "preset": "pre_x",
        "temperature": 1.9,
        "max_output_tokens": 5000,
        "messages": [{"role": "system", "content": "worker extra"}, *_msgs()],
    }
    async with client() as c:
        ok = await _post(c, site, token, body)
        unknown = await _post(c, site, token, {**body, "preset": "pre_nope"})
    assert ok.status_code == 200, ok.text
    payload = fake.payloads[0]
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["max_tokens"] == 200 and payload["temperature"] == 0.1
    assert payload["messages"][0] == {"role": "system", "content": "You are Ada."}
    assert (unknown.status_code, unknown.json()["field"]) == (400, "preset")
    assert "You are Ada." not in ok.text


@pytest.mark.asyncio
async def test_paused_site_streams_unavailable_and_json_gets_402(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiBudgetDoc

    site, token = await _setup()
    await SiteAiBudgetDoc(workspace=WS, pocket_id=site.pocket_id, paused=True).insert()
    fake = FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        streamed = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
        plain = await _post(c, site, token, {"model": "fast", "messages": _msgs(), "stream": False})
    assert streamed.status_code == 200
    assert parse_sse(streamed.text) == [("unavailable", {"reason": "paused"})]
    assert (plain.status_code, plain.json()["detail"]) == (402, "budget_exhausted")
    assert fake.payloads == []


@pytest.mark.asyncio
async def test_retry_once_before_first_token_then_unavailable(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.site_ai.llm import LlmError

    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=[[LlmError(True)], text_events()])
    async with client() as c:
        retried = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
    assert [e for e, _ in parse_sse(retried.text)] == ["chunk", "usage", "done"]
    assert len(fake.payloads) == 2

    fake = FakeLlm(monkeypatch, script=[[LlmError(True)], [LlmError(True)]])
    async with client() as c:
        down = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
    assert parse_sse(down.text) == [("unavailable", {"reason": "temporary"})]

    fake = FakeLlm(monkeypatch, script=[[LlmError(False)]])
    async with client() as c:
        refused = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
        plain = await _post(c, site, token, {"model": "fast", "messages": _msgs(), "stream": False})
    assert parse_sse(refused.text) == [("unavailable", {"reason": "temporary"})]
    assert len(fake.payloads) == 2  # one each, no retry on a non-transient failure
    assert (plain.status_code, plain.json()["detail"]) == (503, "provider_unavailable")
    # Failures before any output cost nothing and leave no billable run.
    assert await ChatRunDoc.find(ChatRunDoc.status == "failed").count() == 0


@pytest.mark.asyncio
async def test_mid_stream_failure_bills_what_streamed_and_ends_with_error(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.site_ai.llm import LlmError, LlmEvent

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[[LlmEvent("text", text="partial "), LlmError(True)]])
    async with client() as c:
        resp = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
    events = parse_sse(resp.text)
    assert [e for e, _ in events] == ["chunk", "usage", "error"]
    assert events[2][1]["code"] == "provider_unavailable"
    assert events[1][1]["credits_micro"] > 0
    run = await ChatRunDoc.find_one(ChatRunDoc.context_type == "site_ai")
    assert run.status == "failed" and run.usage["output_tokens"] > 0


@pytest.mark.asyncio
async def test_provider_error_text_never_reaches_the_response_or_logs(
    beanie_test_db, monkeypatch, caplog
):
    from types import SimpleNamespace

    from pocketpaw_ee.site_ai import llm

    secret = "upstream said: model claude-haiku-4-5 key sk-live-123 refused"

    class _Completions:
        async def create(self, **_kw):
            raise RuntimeError(secret)

    monkeypatch.setattr(
        llm, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    )
    site, token = await _setup()
    caplog.set_level(logging.DEBUG)
    async with client() as c:
        streamed = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
        plain = await _post(c, site, token, {"model": "fast", "messages": _msgs(), "stream": False})
    assert parse_sse(streamed.text) == [("unavailable", {"reason": "temporary"})]
    assert plain.status_code == 503
    for text in (streamed.text, plain.text, caplog.text):
        assert "sk-live-123" not in text and PROVIDER_MODEL not in text


@pytest.mark.asyncio
async def test_site_rate_limit(beanie_test_db, monkeypatch):
    from pocketpaw_ee.site_ai import chat

    monkeypatch.setattr(chat, "WORKER_SITE_PER_MIN", 1)
    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        first = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
        second = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
    assert first.status_code == 200
    assert (second.status_code, second.json()["detail"]) == (429, "rate_limited")
    assert 1 <= second.json()["retry_after"] <= 60


# ---------------------------------------------------------------- adversarial pass


@pytest.mark.asyncio
async def test_deeply_nested_json_is_a_400_not_a_crash(beanie_test_db, monkeypatch):
    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events()])
    nested = b"[" * 100_000 + b"]" * 100_000
    async with client() as c:
        resp = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            content=b'{"model":"fast","tools":' + nested + b"}",
            headers={"authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400 and resp.json()["detail"] == "invalid_request"


@pytest.mark.parametrize(
    "url",
    [
        "https://0x7f.1/x.png",
        "https://127.1/x.png",
        "https://2130706433/x.png",
        "https://[::1]/x.png",
        "https://[::ffff:127.0.0.1]/x.png",
        "https://[fd00::1]/x.png",
        "https://intranet/x.png",
        "https://printer.local/x.png",
        "https://a.test:99999/x.png",
    ],
)
def test_image_hosts_that_point_inward_are_refused(url):
    from pocketpaw_ee.site_ai.errors import AiError
    from pocketpaw_ee.site_ai.validate import check_image_url

    with pytest.raises(AiError):
        check_image_url(url, "x")
    assert check_image_url("https://cdn.example.com/a.png", "x").startswith("https://cdn.")


@pytest.mark.asyncio
async def test_provider_rejection_is_invalid_request_not_an_outage(beanie_test_db, monkeypatch):
    from pocketpaw_ee.site_ai.llm import LlmError

    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=[[LlmError(False, "http", 400)]])
    async with client() as c:
        streamed = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
        plain = await _post(c, site, token, {"model": "fast", "messages": _msgs(), "stream": False})
    assert parse_sse(streamed.text)[0][0] == "error"
    assert parse_sse(streamed.text)[0][1]["code"] == "invalid_request"
    assert (plain.status_code, plain.json()["detail"]) == (400, "invalid_request")
    assert len(fake.payloads) == 2


@pytest.mark.asyncio
async def test_nan_and_huge_numbers_are_refused(beanie_test_db, monkeypatch):
    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        nan = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            content=b'{"model":"fast","temperature":NaN,"messages":[{"role":"user","content":"x"}]}',
            headers={"authorization": f"Bearer {token}"},
        )
        inf = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            content=b'{"model":"fast","max_output_tokens":Infinity,'
            b'"messages":[{"role":"user","content":"x"}]}',
            headers={"authorization": f"Bearer {token}"},
        )
        digits = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            content=b'{"model":"fast","max_output_tokens":' + b"9" * 5000 + b"}",
            headers={"authorization": f"Bearer {token}"},
        )
    assert nan.status_code == 400 and inf.status_code == 400 and digits.status_code == 400


@pytest.mark.asyncio
async def test_a_site_without_a_pocket_is_unauthorized(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site import Site

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events()])
    await Site.get_pymongo_collection().update_one({"_id": site.id}, {"$set": {"pocket_id": ""}})
    async with client() as c:
        resp = await _post(c, site, token, {"model": "fast", "messages": _msgs()})
    assert resp.status_code == 401
