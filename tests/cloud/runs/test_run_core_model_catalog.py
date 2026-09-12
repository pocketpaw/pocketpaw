# tests/cloud/runs/test_run_core_model_catalog.py — a per-send model must name
# something the gateway actually serves.
#
# Created 2026-09-11 (feat/pydantic-ai-model-override). Until the override was
# honoured on pydantic_ai it was a dead control on the cloud's default backend,
# so a free-text model id never reached the platform gateway. Once it works,
# ``model`` on the request is checked against nothing but a regex — the
# composer's picker is three hardcoded presets plus a free-text field, and
# ``provider_allows_model`` runs on the BYOK branch alone.
#
# The catalog is the union of what the proxy describes (/model/info) and what it
# routes (/v1/models), so it is never narrower than the routable set. An id the
# catalog does not hold is one the gateway would refuse anyway; rejecting it here
# turns an opaque upstream 400 into a typed error the composer can show.
#
# The two tests that earn their space are the fail-open ones. A catalog outage
# and an empty catalog must NOT reject the turn: either would make every
# override fail on a deployment whose /model/info is admin-gated or misread,
# which is the dead-control bug this branch exists to remove.
#
# Harness cloned from test_run_core_byok_wiring.py (capture pool + stubbed
# collaborators; hermetic, no mongod, no proxy).

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry
from pocketpaw_ee.cloud.byok.service import TurnCredentials
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio


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
        return SimpleNamespace(config={"backend": "pydantic_ai", "model": ""}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        self.run_called = True
        self.run_kwargs = kwargs

        async def _gen():
            yield _Ev("message", "hi back")
            yield _Ev("done")

        return _gen()


def _entry(model_id: str) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id=model_id,
        display_name=model_id,
        provider="anthropic",
        modality=Modality.CHAT,
        status="available",
    )


def _ctx(*, model_override: str | None = None) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        model_override=model_override,
    )


async def _drive(
    monkeypatch,
    ctx: ScopeContext,
    *,
    catalog,
) -> tuple[_CapturePool, list[tuple[str, dict]]]:
    """Run the loop with *catalog* standing in for ``catalog_service.list_models``.

    Pass a list of entries, or an exception instance to raise.
    """
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

    async def _list_models(**_kwargs):
        if isinstance(catalog, BaseException):
            raise catalog
        return list(catalog)

    monkeypatch.setattr("pocketpaw_ee.catalog.service.list_models", _list_models)

    async def _resolve(workspace_id):
        return TurnCredentials(source="platform")

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _resolve)

    async def _not_guest(user_id):
        return None

    monkeypatch.setattr("pocketpaw_ee.cloud.auth.guest_budget.load_guest", _not_guest)

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


async def test_a_model_the_gateway_never_heard_of_is_refused(monkeypatch):
    """The finding: a per-send model was free text billed to the platform key."""
    pool, out = await _drive(
        monkeypatch,
        _ctx(model_override="gpt-9-ultra-expensive"),
        catalog=[_entry("claude-opus-4-8")],
    )
    assert pool.run_called is False, "an unknown model must never reach the gateway"
    errors = [d for name, d in out if name == "error"]
    assert errors and errors[0]["code"] == "model.not_available"
    assert "gpt-9-ultra-expensive" in errors[0]["message"]


async def test_a_catalogued_model_runs(monkeypatch):
    pool, _ = await _drive(
        monkeypatch,
        _ctx(model_override="claude-opus-4-8"),
        catalog=[_entry("claude-opus-4-8"), _entry("claude-haiku-4-5")],
    )
    assert pool.run_called is True
    assert pool.run_kwargs.get("model_override") == "claude-opus-4-8"


async def test_no_override_never_reads_the_catalog(monkeypatch):
    """An auto send is the common case and must not pay a catalog read."""
    pool, _ = await _drive(
        monkeypatch,
        _ctx(),
        catalog=RuntimeError("the catalog must not be consulted for an auto send"),
    )
    assert pool.run_called is True
    assert "model_override" not in (pool.run_kwargs or {})


async def test_an_unreachable_catalog_does_not_kill_the_turn(monkeypatch, caplog):
    """Fail OPEN, matching the byok resolver ten lines below the check.

    ``/model/info`` is an admin route and can be gated or down on a deployment
    whose chat routing is perfectly healthy. Refusing there would make the
    picker a dead control again, which is the bug this branch removes.
    """
    with caplog.at_level(logging.WARNING):
        pool, out = await _drive(
            monkeypatch,
            _ctx(model_override="claude-opus-4-8"),
            catalog=RuntimeError("proxy unreachable"),
        )
    assert pool.run_called is True
    assert pool.run_kwargs.get("model_override") == "claude-opus-4-8"
    assert not [d for name, d in out if name == "error"]
    assert any("catalog" in r.message.lower() for r in caplog.records)


async def test_an_empty_catalog_refuses_nothing(monkeypatch):
    """A proxy that describes no models must not reject every model.

    ``get_model`` cannot tell "unknown id" from "catalog came back empty", and
    the second one rejecting every send is a far worse failure than the cost
    exposure being checked for.
    """
    pool, out = await _drive(
        monkeypatch,
        _ctx(model_override="claude-opus-4-8"),
        catalog=[],
    )
    assert pool.run_called is True
    assert not [d for name, d in out if name == "error"]
