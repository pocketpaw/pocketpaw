"""POST /cloud/chat/{scope}/{scope_id}/agent — the JSON endpoint.

The POST no longer streams: it persists the user message, creates a
:class:`ChatRunDoc`, and hands the run to a :class:`RunExecutor`. Event
streaming lives in :mod:`pocketpaw_ee.cloud.chat.runs.router`
(``GET /cloud/chat/runs/{run_id}/stream``) and is covered by
``tests/cloud/runs/test_run_router.py`` + ``tests/cloud/runs/test_run_core.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _fake_ctx() -> SimpleNamespace:
    """Minimal ``ScopeContext`` stand-in good enough for the router's reads."""
    return SimpleNamespace(
        kind=SimpleNamespace(value="session"),
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )


async def _fake_resolve(**_):
    return _fake_ctx()


async def _fake_persist_user_message(_ctx, _body):
    return "user_msg_id_1"


async def _fake_load_history(_ctx, *, limit=50):  # noqa: ARG001
    return []


async def _fake_ensure_session(_ctx):
    return "session_id_1"


async def test_post_agent_creates_run_and_returns_json(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    """POST returns JSON with the new run_id and submits the run to the executor."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    submitted: list[str] = []

    class _FakeExecutor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c1"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"]
    assert body["user_message_id"] == "user_msg_id_1"
    assert body["session_id"] == "session_id_1"
    assert body["client_message_id"] == "c1"
    # The executor received the freshly created run.
    assert submitted == [body["run_id"]]


async def test_post_agent_idempotent_on_client_message_id(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    """Two POSTs with the same ``client_message_id`` resolve to one run."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    submitted: list[str] = []

    class _FakeExecutor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())

    # The second POST sees the first run as ``active`` (still queued — the
    # fake executor never marks it terminal) and tries to cancel it through
    # the stream transport. Stub the transport so the test doesn't need
    # ``POCKETPAW_REDIS_URL`` set.
    class _NullTransport:
        async def request_cancel(self, run_id):  # noqa: ARG002
            return None

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _NullTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        body_json = {"content": "hi", "client_message_id": "same"}
        r1 = await cloud_app_client.post("/cloud/chat/session/s1/agent", json=body_json)
        r2 = await cloud_app_client.post("/cloud/chat/session/s1/agent", json=body_json)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["run_id"] == r2.json()["run_id"], (
        "create_run is idempotent on (workspace, client_message_id), so a "
        "re-submitted message must return the same run."
    )
