# tests/cloud/lens/test_overview.py — POST /api/v1/lens/runs/{trace_id}/overview.
#
# Reuses test_router's app and the conftest MockTransport upstream, and swaps the service's
# ``PocketPawCompilerBackend`` for a fake that records each prompt and the
# ``baggage()`` attributes live during the call. Covers: admin-only (member 403,
# nothing upstream), cache hit (no LLM call, no PUT), refresh regenerates, the
# PUT body, LLM failure and timeout -> 503 lens.overview_failed with no PUT,
# paw.internal + workspace stamped during the call, and the digest cap.

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pocketpaw_ee.cloud.lens import service as lens_service

from pocketpaw import observability
from tests.cloud.lens.test_router import _app, _settings

TRACE = "a" * 32
URL = f"/api/v1/lens/runs/{TRACE}/overview"

_DETAIL = {
    "run": {"agent": "Sales Bot", "model": "claude", "status": "error", "cost_usd": 0.12},
    "spans": [
        {"span_id": "m1", "kind": "model"},
        {"span_id": "t1", "kind": "tool"},
        {"span_id": "h1", "kind": "http"},
    ],
    "findings": [{"detector": "tool_error", "severity": "high", "message": "crm 500"}],
    "overview": None,
}
_SPANS = {
    "m1": {
        "span_id": "m1",
        "messages": {
            "input": [{"role": "user", "parts": [{"content": "find the acme deal"}]}],
            "output": [{"role": "assistant", "parts": [{"content": "looking it up"}]}],
        },
        "tool": None,
    },
    "t1": {
        "span_id": "t1",
        "error": "HTTP 500",
        "messages": None,
        "tool": {"name": "crm_search", "arguments": {"q": "acme"}, "result": None},
    },
}


class _FakeLLM:
    calls: list[dict] = []
    reply: object = "- asked for acme\n- crm_search failed"

    async def complete(self, prompt: str, system_prompt: str = "") -> str:
        _FakeLLM.calls.append(
            {"prompt": prompt, "baggage": dict(observability._RUN_ATTRIBUTES.get())}
        )
        if isinstance(_FakeLLM.reply, BaseException):
            raise _FakeLLM.reply
        if _FakeLLM.reply == "hang":
            await asyncio.sleep(5)
        return str(_FakeLLM.reply)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    _FakeLLM.calls = []
    _FakeLLM.reply = "- asked for acme\n- crm_search failed"
    monkeypatch.setattr(lens_service, "PocketPawCompilerBackend", _FakeLLM)
    return _FakeLLM


def _handler(detail):
    def _h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "PUT":
            return httpx.Response(200, json={"created_at": "2026-10-03T00:00:00Z"})
        if "/spans/" in path:
            return httpx.Response(200, json=_SPANS[path.rsplit("/", 1)[-1]])
        return httpx.Response(200, json=detail)

    return _h


def test_member_forbidden(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    resp = _app("member").post(URL)
    assert resp.status_code == 403
    assert seen == [] and _FakeLLM.calls == []


def test_cache_hit_skips_llm(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    cached = {"text": "- cached", "model": "m", "created_at": "2026-10-01T00:00:00Z"}
    upstream(_handler({**_DETAIL, "overview": cached}))
    resp = _app("admin").post(URL)
    assert resp.status_code == 200
    assert resp.json() == cached
    assert _FakeLLM.calls == []
    assert [r.method for r in seen] == ["GET"]


def test_generates_and_stores(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    upstream(_handler(_DETAIL))
    resp = _app("admin").post(URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "- asked for acme\n- crm_search failed"
    assert body["created_at"] == "2026-10-03T00:00:00Z"
    # Model + tool spans fetched, the http span not.
    fetched = sorted(r.url.path.rsplit("/", 1)[-1] for r in seen if "/spans/" in r.url.path)
    assert fetched == ["m1", "t1"]
    prompt = _FakeLLM.calls[0]["prompt"]
    for needle in ("find the acme deal", "looking it up", "crm_search", "HTTP 500", "crm 500"):
        assert needle in prompt, needle
    put = [r for r in seen if r.method == "PUT"][0]
    assert put.url.path == f"/v1/runs/{TRACE}/overview"
    assert put.url.params["workspace_id"] == "ws_test"
    assert json.loads(put.content) == {
        "text": body["text"],
        "model": body["model"],
        "generated_by": "u1",
    }


def test_baggage_marks_call_internal(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(_handler(_DETAIL))
    assert _app("admin").post(URL).status_code == 200
    assert _FakeLLM.calls[0]["baggage"]["paw.internal"] == "true"
    assert _FakeLLM.calls[0]["baggage"]["paw.workspace_id"] == "ws_test"
    assert "paw.internal" not in observability._RUN_ATTRIBUTES.get()


def test_refresh_regenerates_over_cache(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    upstream(_handler({**_DETAIL, "overview": {"text": "old"}}))
    resp = _app("admin").post(URL, params={"refresh": "1"})
    assert resp.status_code == 200
    assert resp.json()["text"].startswith("- asked")
    assert len(_FakeLLM.calls) == 1
    assert any(r.method == "PUT" for r in seen)


@pytest.mark.parametrize("reply", [RuntimeError("backend down"), ""])
def test_llm_failure_is_503(monkeypatch, upstream, seen, reply):
    _settings(monkeypatch, "http://lens:8790")
    upstream(_handler(_DETAIL))
    _FakeLLM.reply = reply
    resp = _app("admin").post(URL)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "lens.overview_failed"
    assert not any(r.method == "PUT" for r in seen)


def test_llm_timeout_is_503(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(_handler(_DETAIL))
    monkeypatch.setattr(lens_service, "OVERVIEW_TIMEOUT_SECONDS", 0.05)
    _FakeLLM.reply = "hang"
    resp = _app("admin").post(URL)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "lens.overview_failed"


def test_digest_truncated():
    spans = [
        {"tool": {"name": f"t{i}", "arguments": "x" * 5000, "result": "y" * 5000}}
        for i in range(100)
    ]
    digest = lens_service.build_digest({"run": {}, "findings": []}, spans)
    assert len(digest) <= lens_service.DIGEST_MAX_CHARS + 40
    assert digest.endswith("[digest truncated]")
    assert "…[truncated]" in digest  # per-field cap too
