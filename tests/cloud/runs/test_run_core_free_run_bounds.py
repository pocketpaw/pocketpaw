# tests/cloud/runs/test_run_core_free_run_bounds.py: ``_drive_agent_loop``
# hands the free-run bounds ``execute_run`` put on the ctx to ``pool.run``: the
# run is forced onto pydantic_ai, capped at the turn limit, and given a token
# limit the wallet can pay for at the price of the model the forced backend
# will run. A stored key is withheld (the turn was gated and is billed as
# platform), and the session supervisor, which leases a Claude SDK client, is
# skipped. A ctx with no bounds sends none of these kwargs.
#
# The pool is a capture double; ``tests/test_agent_pool_backend_lifecycle.py``
# proves the real pool honours the kwargs.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.byok.service import TurnCredentials
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.credits import guards
from pocketpaw_ee.cloud.metering.domain import RateCard

pytestmark = pytest.mark.asyncio

_MODEL = "claude-haiku-4-5"


class _Ev:
    def __init__(self, type_: str, content: str = "") -> None:
        self.type = type_
        self.content = content
        self.metadata: dict[str, Any] = {}


class _CapturePool:
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.run_kwargs: dict[str, Any] | None = None

    async def get(self, _agent_id):
        return SimpleNamespace(
            config={"backend": self.backend, "model": "claude-opus-4-6"},
            agent_name="A",
            backend=SimpleNamespace(
                settings=SimpleNamespace(pydantic_ai_model=f"litellm:{_MODEL}")
            ),
        )

    def run(self, agent_id, content, session_key, **kwargs):
        self.run_kwargs = kwargs

        async def _gen():
            yield _Ev("message", "hi back")
            yield _Ev("done")

        return _gen()


async def _drive(monkeypatch, ctx: ScopeContext, *, backend: str, creds: TurnCredentials):
    monkeypatch.setenv("POCKETPAW_SESSION_SUPERVISOR", "true")
    pool = _CapturePool(backend)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _empty(*a, **k):
        return ""

    async def _resolve(_ws):
        return creds

    async def _not_guest(_uid):
        return None

    # A VIABLE supervisor (stubs as in test_run_core_byok_wiring.py), so the
    # skip assertion cannot pass on the supervisor's own failure cleanup.
    class _FakeRuntimeService:
        async def get_cli_session_id(self, ws, session, agent):
            return "prior-cli-id"

        async def set_cli_session_id(self, *a, **k):
            return None

    class _FakeStore:
        def __init__(self, workspace_id):
            self.workspace_id = workspace_id

    from pocketpaw.agents.session_supervisor import SessionSupervisor

    monkeypatch.setattr(run_core, "build_knowledge_context", _empty)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "runtime_service", _FakeRuntimeService())
    monkeypatch.setattr(run_core, "MongoSessionStore", _FakeStore)
    monkeypatch.setattr(run_core, "get_session_supervisor", lambda: SessionSupervisor())
    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _resolve)
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.guest_budget.load_guest", _not_guest)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.metering.service.load_rate_card",
        lambda: RateCard(markup=2.5, credit_usd=0.01),
    )

    async def _never():
        return False

    out = [
        ev
        async for ev in run_core._drive_agent_loop(
            ctx,
            user_content="hi",
            attachments_in=None,
            mentions_in=None,
            history=[],
            is_cancelled=_never,
            emit_stream_start=False,
        )
    ]
    return pool, out


def _ctx(bounds: guards.FreeRunBounds | None) -> ScopeContext:
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        session_id="sess-1",
    )
    ctx.credential_source = "platform"
    ctx.free_run_bounds = bounds
    return ctx


@pytest.mark.parametrize("backend", ["claude_agent_sdk", "pydantic_ai"])
async def test_bounded_run_is_forced_capped_and_token_limited(monkeypatch, backend):
    bounds = guards.FreeRunBounds(max_turns=20, balance_micro=7_000_000)
    pool, _out = await _drive(
        monkeypatch, _ctx(bounds), backend=backend, creds=TurnCredentials(source="platform")
    )

    kw = pool.run_kwargs
    assert kw is not None
    assert kw["force_pydantic_ai"] is True
    assert kw["max_requests"] == 20
    expected = guards.free_run_token_limit(
        7_000_000, _MODEL, rate_card=RateCard(markup=2.5, credit_usd=0.01)
    )
    assert expected and kw["total_tokens_limit"] == expected
    assert "session_handle" not in kw and "warm_client" not in kw


async def test_bounded_run_withholds_a_stored_key(monkeypatch):
    """A key stored for a claude_agent_sdk agent did not make the turn BYOK; it
    was gated and is billed as platform, so it runs on our gateway under the
    bounds, never on the key without them."""
    bounds = guards.FreeRunBounds(max_turns=20, balance_micro=7_000_000)
    creds = TurnCredentials(source="byok", api_key="sk-ant-api03-" + "x" * 40, provider="anthropic")
    pool, _out = await _drive(monkeypatch, _ctx(bounds), backend="claude_agent_sdk", creds=creds)

    kw = pool.run_kwargs
    assert kw is not None and kw["force_pydantic_ai"] is True
    assert "byok_api_key" not in kw and "byok_settings_override" not in kw


async def test_unbounded_run_sends_no_limits(monkeypatch):
    pool, _out = await _drive(
        monkeypatch,
        _ctx(None),
        backend="claude_agent_sdk",
        creds=TurnCredentials(source="platform"),
    )
    kw = pool.run_kwargs
    assert kw is not None
    for name in ("force_pydantic_ai", "max_requests", "total_tokens_limit"):
        assert name not in kw
    # The harness supervisor is viable: an unbounded turn does lease it.
    assert "session_handle" in kw
