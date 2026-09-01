# tests/cloud/runs/test_run_core_byok_wiring.py — the turn path FINALLY calls
# resolve_turn_credentials, and what it does with the answer.
#
# Created 2026-09-01 (feat/byok-guest-backend). The byok service's own header
# has said since 2026-08-28 that "the TURN PATH calls resolve_turn_credentials"
# — nothing did. These tests pin the wiring in ``_drive_agent_loop``:
#
#   * byok resolves       -> ``pool.run`` receives ``byok_api_key=<plaintext>``.
#   * model mismatch      -> a clear ``byok.model_provider_mismatch`` error
#                            frame, and pool.run is NEVER invoked.
#   * platform + guest    -> ``guest_key_required`` error frame, no pool.run —
#                            the captain's no-keyless-turns rule; a guest's
#                            degrade-to-platform is a silent platform bill.
#   * platform + normal   -> byte-identical legacy call (no byok kwarg).
#   * byok + supervisor ON-> session_handle / warm_client / on_client_built are
#                            ALL withheld (the warm-client credential-bleed
#                            guard) while byok_api_key IS threaded.
#   * the key never lands in a log record (redaction at the seam).
#
# Harness cloned from test_run_core_session_supervisor.py (capture pool +
# stubbed collaborators; hermetic, no mongod).

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.byok.service import TurnCredentials
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio

_PLAINTEXT = "sk-ant-api03-" + "wired" * 9


class _Ev:
    def __init__(self, type_: str, content: str = "") -> None:
        self.type = type_
        self.content = content
        self.metadata: dict[str, Any] = {}


class _CapturePool:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None
        self.run_called = False

    async def get(self, _agent_id):
        return SimpleNamespace(
            config={"backend": "claude_agent_sdk", "model": ""}, agent_name="A"
        )

    def run(self, agent_id, content, session_key, **kwargs):
        self.run_called = True
        self.run_kwargs = kwargs

        async def _gen():
            yield _Ev("message", "hi back")
            yield _Ev("done")

        return _gen()


def _ctx(*, model_override: str | None = None, user_id: str = "u1") -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id=user_id,
        members=[user_id],
        target_agent_id="a1",
        model_override=model_override,
    )


async def _not_guest(user_id):
    return None


async def _drive(
    monkeypatch,
    ctx: ScopeContext,
    *,
    creds: TurnCredentials,
    guest_loader=_not_guest,
    supervisor_flag: bool = False,
) -> tuple[_CapturePool, list[tuple[str, dict]]]:
    if supervisor_flag:
        monkeypatch.setenv("POCKETPAW_SESSION_SUPERVISOR", "true")
    else:
        monkeypatch.delenv("POCKETPAW_SESSION_SUPERVISOR", raising=False)

    pool = _CapturePool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None)

    if supervisor_flag:
        # Make the supervisor path VIABLE (stubs cloned from
        # test_run_core_session_supervisor.py) — otherwise its own failure
        # cleanup pops session_handle/warm_client and the byok-skip assertion
        # passes vacuously even with the guard deleted.
        class _FakeRuntimeService:
            async def get_cli_session_id(self, ws, session, agent):
                return "prior-cli-id"

            async def set_cli_session_id(self, *a, **k):
                return None

        class _FakeStore:
            def __init__(self, workspace_id):
                self.workspace_id = workspace_id

        from pocketpaw.agents.session_supervisor import SessionSupervisor

        monkeypatch.setattr(run_core, "runtime_service", _FakeRuntimeService())
        monkeypatch.setattr(run_core, "MongoSessionStore", _FakeStore)
        monkeypatch.setattr(run_core, "get_session_supervisor", lambda: SessionSupervisor())

    async def _resolve(workspace_id):
        return creds

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _resolve
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.guest_budget.load_guest", guest_loader)

    async def _never_cancelled():
        return False

    out: list[tuple[str, dict]] = []
    gen = run_core._drive_agent_loop(
        ctx,
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    )
    async for ev in gen:
        out.append(ev)
    return pool, out


# ---------------------------------------------------------------------------


async def test_a_stored_key_is_threaded_into_pool_run(monkeypatch):
    pool, out = await _drive(
        monkeypatch,
        _ctx(),
        creds=TurnCredentials(source="byok", api_key=_PLAINTEXT, provider="anthropic"),
    )
    assert pool.run_called is True
    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("byok_api_key") == _PLAINTEXT


async def test_a_model_from_another_provider_is_a_clear_error_not_a_dead_turn(monkeypatch):
    pool, out = await _drive(
        monkeypatch,
        _ctx(model_override="gpt-4o"),
        creds=TurnCredentials(source="byok", api_key=_PLAINTEXT, provider="anthropic"),
    )
    assert pool.run_called is False, "a mismatched turn must never reach the model"
    errors = [d for name, d in out if name == "error"]
    assert errors and errors[0]["code"] == "byok.model_provider_mismatch"
    assert "gpt-4o" in errors[0]["message"]


async def test_a_matching_model_override_passes(monkeypatch):
    pool, _ = await _drive(
        monkeypatch,
        _ctx(model_override="claude-opus-4-6"),
        creds=TurnCredentials(source="byok", api_key=_PLAINTEXT, provider="anthropic"),
    )
    assert pool.run_called is True
    assert pool.run_kwargs.get("byok_api_key") == _PLAINTEXT


async def test_a_guest_on_platform_credentials_is_refused(monkeypatch):
    """resolve_turn_credentials degrades to platform on a missing or
    undecryptable key BY DESIGN for paying users — for a guest that degrade
    means WE pay, so the turn must refuse with the re-enter-your-key code."""

    async def _a_guest(user_id):
        return SimpleNamespace(is_guest=True)

    pool, out = await _drive(
        monkeypatch,
        _ctx(),
        creds=TurnCredentials(source="platform"),
        guest_loader=_a_guest,
    )
    assert pool.run_called is False, "a keyless guest turn must never bill the platform"
    errors = [d for name, d in out if name == "error"]
    assert errors and errors[0]["code"] == "guest_key_required"


async def test_an_unreadable_user_row_refuses_a_platform_turn_fail_closed(monkeypatch):
    async def _broken(user_id):
        raise RuntimeError("db down")

    pool, out = await _drive(
        monkeypatch,
        _ctx(),
        creds=TurnCredentials(source="platform"),
        guest_loader=_broken,
    )
    assert pool.run_called is False
    errors = [d for name, d in out if name == "error"]
    assert errors and errors[0]["code"] == "guest_key_required"


async def test_a_platform_turn_for_a_normal_user_is_byte_identical(monkeypatch):
    pool, out = await _drive(
        monkeypatch,
        _ctx(),
        creds=TurnCredentials(source="platform"),
    )
    assert pool.run_called is True
    assert "byok_api_key" not in pool.run_kwargs
    assert [name for name, _ in out] == ["chunk"]


async def test_byok_skips_the_supervisor_warm_client_wiring(monkeypatch):
    """The credential-bleed guard: a leased warm client carries PLATFORM
    credentials, and binding a byok-built client as the shared warm slot
    bleeds the other way. A byok turn must run cold — no session_handle, no
    warm_client, no on_client_built — while still carrying the key."""
    pool, _ = await _drive(
        monkeypatch,
        _ctx(),
        creds=TurnCredentials(source="byok", api_key=_PLAINTEXT, provider="anthropic"),
        supervisor_flag=True,
    )
    assert pool.run_called is True
    assert pool.run_kwargs.get("byok_api_key") == _PLAINTEXT
    for forbidden in ("session_handle", "warm_client", "on_client_built"):
        assert forbidden not in pool.run_kwargs, f"{forbidden} must be withheld on a byok turn"


async def test_the_supervisor_still_wires_on_a_platform_turn(monkeypatch):
    """The counterpart that keeps the byok-skip test honest: with the SAME
    stubs and the flag ON, a platform turn DOES get the supervisor wiring —
    so the byok test's empty kwargs can only come from the byok guard."""
    pool, _ = await _drive(
        monkeypatch,
        _ctx(),
        creds=TurnCredentials(source="platform"),
        supervisor_flag=True,
    )
    assert pool.run_called is True
    assert "session_handle" in pool.run_kwargs, (
        "harness must make the supervisor path viable, or the byok-skip test is vacuous"
    )


async def test_the_plaintext_key_never_reaches_a_log_record(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        await _drive(
            monkeypatch,
            _ctx(),
            creds=TurnCredentials(source="byok", api_key=_PLAINTEXT, provider="anthropic"),
        )
    assert _PLAINTEXT not in caplog.text
    for rec in caplog.records:
        assert _PLAINTEXT not in str(rec.args or "")
