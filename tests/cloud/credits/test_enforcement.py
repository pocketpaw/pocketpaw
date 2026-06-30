# tests/cloud/credits/test_enforcement.py — BC-4 run-start hard-block +
# the chunk-3 monthly-quota fast-reject (feat/billing-quota-enforcement).
#
# Proves the credit gates at the SINGLE chat run-start chokepoint
# (chat/agent_router.py::post_agent_chat):
#   1. billing_enforced=True + balance 0  -> POST returns 402 credits.insufficient
#      and NO ChatRunDoc is created (the gate runs before create_run/submit).
#   2. billing_enforced=True + balance > 0 -> the request proceeds past the gate.
#   3. billing_enforced=False (default)   -> never gates, even at balance 0.
#   4. credits.service.check_balance raises InsufficientCredits at balance <= 0
#      and is a no-op at balance > 0 (unit level).
#   5. billing_enforced=True + over monthly QUOTA (balance still > 0) -> POST
#      returns 402 credits.quota_exceeded and NO ChatRunDoc is created — the
#      ``check_quota`` fast-reject sits beside ``check_balance`` (chunk 3).
#   6. billing_enforced=True + under quota -> proceeds past the gate.
#   7. billing_enforced=False -> the quota gate never fires either.
#
# Created 2026-06-24 (integration/billing-credits, BC-4).
# Changed 2026-06-30 (feat/billing-quota-enforcement, chunk 3): added the
# ``check_quota`` chat fast-reject cases (5-7). They fund the wallet so the
# ``check_balance`` guard is a no-op, then stub ``credits_service.check_quota``
# to isolate the quota wiring (the chunk-2 quota math is owned by test_quota.py).

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Router-test scaffolding (mirrors tests/cloud/test_agent_router.py so the POST
# path runs without a real Redis transport / scope resolution).
# ---------------------------------------------------------------------------


def _fake_ctx() -> SimpleNamespace:
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


class _StubTransport:
    """Emits one ``stream_end`` so the SSE generator closes without Redis."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def request_cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    def read_events(self, run_id: str, *, after: str = "0", block_ms: int = 15000) -> AsyncIterator:  # noqa: ARG002
        async def _gen() -> AsyncIterator:
            from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent

            yield StreamEvent(
                entry_id="1-0",
                event="stream_end",
                data={"assistant_message_id": None, "usage": {}, "cancelled": False},
            )

        return _gen()


def _enforce(monkeypatch, mod, *, on: bool) -> None:
    """Point the router's ``get_settings`` at a stub carrying the flag."""
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(billing_enforced=on))


def _patch_run_internals(mod):
    """Patch the side-effecting collaborators the POST path calls AFTER the gate.

    Returns the context-manager bundle; the executor stub records submitted run
    ids so a test can assert the run did (or did not) proceed past the gate.
    """
    submitted: list[str] = []

    class _FakeExecutor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    return submitted, _FakeExecutor


# ---------------------------------------------------------------------------
# 1 — enforced + balance 0 -> 402 and NO run/ChatRunDoc created.
# ---------------------------------------------------------------------------


async def test_enforced_zero_balance_blocks_with_402_and_creates_no_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    _enforce(monkeypatch, mod, on=True)
    submitted, _FakeExecutor = _patch_run_internals(mod)
    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    # Workspace w1 has no wallet -> balance() returns 0 -> gate must fire.
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

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "credits.insufficient"
    # The gate ran before create_run / submit: no run reached the executor and
    # no ChatRunDoc was written.
    assert submitted == []
    assert await ChatRunDoc.find_all().count() == 0


# ---------------------------------------------------------------------------
# 2 — enforced + balance > 0 -> proceeds past the gate (run created/submitted).
# ---------------------------------------------------------------------------


async def test_enforced_positive_balance_proceeds(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    # Fund the wallet so check_balance is a no-op.
    await credits_service.grant("w1", 100, cause="test.seed", idempotency_key="seed-1")

    _enforce(monkeypatch, mod, on=True)
    submitted, _FakeExecutor = _patch_run_internals(mod)
    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c2"},
        )

    assert resp.status_code == 200
    # Proceeded past the gate: a run was created and submitted to the executor.
    assert len(submitted) == 1
    assert await ChatRunDoc.find_all().count() == 1


# ---------------------------------------------------------------------------
# 3 — NOT enforced (default) -> never gates, even at balance 0.
# ---------------------------------------------------------------------------


async def test_not_enforced_never_gates_at_zero_balance(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    # Default posture: flag OFF. Wallet is empty (balance 0) — must still proceed.
    _enforce(monkeypatch, mod, on=False)
    submitted, _FakeExecutor = _patch_run_internals(mod)
    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c3"},
        )

    assert resp.status_code == 200
    assert len(submitted) == 1
    assert await ChatRunDoc.find_all().count() == 1


# ---------------------------------------------------------------------------
# 4 — check_balance unit behaviour.
# ---------------------------------------------------------------------------


async def test_check_balance_raises_at_zero(mongo_db):  # noqa: ARG001 — Beanie init
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits
    from pocketpaw_ee.cloud.credits import service as credits_service

    # No wallet -> balance 0 -> raises 402 credits.insufficient.
    with pytest.raises(InsufficientCredits) as exc:
        await credits_service.check_balance("w-empty")
    assert exc.value.status_code == 402
    assert exc.value.code == "credits.insufficient"


async def test_check_balance_raises_when_negative(mongo_db):  # noqa: ARG001 — Beanie init
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits
    from pocketpaw_ee.cloud.credits import service as credits_service

    # Drive the wallet negative via a metered (allow_negative) debit, then assert
    # the gate still blocks at < 0.
    await credits_service.grant("w-neg", 10, cause="seed", idempotency_key="g1")
    await credits_service.debit(
        "w-neg", 25, cause="overage", idempotency_key="d1", allow_negative=True
    )
    assert await credits_service.balance("w-neg") == -15
    with pytest.raises(InsufficientCredits):
        await credits_service.check_balance("w-neg")


async def test_check_balance_noop_when_positive(mongo_db):  # noqa: ARG001 — Beanie init
    from pocketpaw_ee.cloud.credits import service as credits_service

    await credits_service.grant("w-funded", 5, cause="seed", idempotency_key="g1")
    # No exception -> returns None.
    assert await credits_service.check_balance("w-funded") is None


# ---------------------------------------------------------------------------
# 5 — enforced + OVER monthly quota (wallet still funded) -> 402
#     credits.quota_exceeded and NO ChatRunDoc created.
#
# ``check_balance`` runs FIRST and must pass (the wallet is funded), then the
# new ``check_quota`` fast-reject fires. We stub ``check_quota`` to raise so the
# test isolates the chunk-3 WIRING (the quota math lives in test_quota.py).
# ---------------------------------------------------------------------------


async def test_enforced_over_quota_blocks_with_402_and_creates_no_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init
    monkeypatch,
):
    from pocketpaw_ee.cloud._core.errors import QuotaExceeded
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    # Fund w1 so the balance guard is a no-op and we reach the quota gate.
    await credits_service.grant("w1", 100, cause="test.seed", idempotency_key="seed-q1")

    async def _over_quota(workspace_id):
        raise QuotaExceeded(ceiling=1000, spent=1000)

    monkeypatch.setattr(credits_service, "check_quota", _over_quota)

    _enforce(monkeypatch, mod, on=True)
    submitted, _FakeExecutor = _patch_run_internals(mod)
    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "cq1"},
        )

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "credits.quota_exceeded"
    # The gate ran before create_run / submit: no run reached the executor and
    # no ChatRunDoc was written (clean fast-reject, no DB trace).
    assert submitted == []
    assert await ChatRunDoc.find_all().count() == 0


# ---------------------------------------------------------------------------
# 6 — enforced + UNDER quota -> proceeds past the gate (run created/submitted).
# ---------------------------------------------------------------------------


async def test_enforced_under_quota_proceeds(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    await credits_service.grant("w1", 100, cause="test.seed", idempotency_key="seed-q2")

    quota_calls: list[str] = []

    async def _under_quota(workspace_id):
        quota_calls.append(workspace_id)  # no-op (under ceiling)

    monkeypatch.setattr(credits_service, "check_quota", _under_quota)

    _enforce(monkeypatch, mod, on=True)
    submitted, _FakeExecutor = _patch_run_internals(mod)
    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "cq2"},
        )

    assert resp.status_code == 200
    assert quota_calls == ["w1"]  # the quota gate ran and passed
    assert len(submitted) == 1
    assert await ChatRunDoc.find_all().count() == 1


# ---------------------------------------------------------------------------
# 7 — NOT enforced -> the quota gate never fires (check_quota not called).
# ---------------------------------------------------------------------------


async def test_not_enforced_never_calls_check_quota(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    quota_calls: list[str] = []

    async def _should_not_run(workspace_id):
        quota_calls.append(workspace_id)
        raise AssertionError("check_quota must NOT be called when the flag is OFF")

    monkeypatch.setattr(credits_service, "check_quota", _should_not_run)

    _enforce(monkeypatch, mod, on=False)
    submitted, _FakeExecutor = _patch_run_internals(mod)
    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "cq3"},
        )

    assert resp.status_code == 200
    assert quota_calls == []  # the flag-off path never gates
    assert len(submitted) == 1
    assert await ChatRunDoc.find_all().count() == 1
