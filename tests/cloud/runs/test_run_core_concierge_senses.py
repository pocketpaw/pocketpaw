# tests/cloud/runs/test_run_core_concierge_senses.py
# Created 2026-08-02 (Sense Phase 2, SP2-4 — concierge surface opt-in for carried
# senses) — pins the FAIL-CLOSED conditional grant that gives a public site
# concierge hands, and only when its owner mounted senses on the agent:
#   * _concierge_sense_policy (pure unit): CONCIERGE + non-empty config.senses
#     unions the two sense tool ids into the surface allow-list; CONCIERGE +
#     empty/absent senses DENIES both ids instead (deny, not mere absence, is
#     what makes SP2-3's "empty = inherit" unreachable on a public surface);
#     every non-concierge scope returns its inputs untouched; dict AND object
#     configs both work; an unrestricted (None) allow-list is never turned into
#     a restriction.
#   * _drive_agent_loop wiring: the run_kwargs a concierge run hands the pool
#     carry the grant / the deny accordingly, an EXCLUSIVE concierge agent that
#     declares the ids itself still cannot get them past the deny when unmounted
#     (the airtight case), and a SESSION run is byte-identical to today.
# The raw connector tool ids are asserted ABSENT throughout — this slice opens
# the sense surface only.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.agent.mcp_servers.connectors import (  # noqa: E402
    CONNECTOR_EXECUTE_TOOL_ID,
    LIST_CONNECTOR_ACTIONS_TOOL_ID,
    LIST_SENSES_TOOL_ID,
    SENSE_EXECUTE_TOOL_ID,
)
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind  # noqa: E402
from pocketpaw_ee.cloud.chat.runs import run_core  # noqa: E402

SENSE_IDS = frozenset({LIST_SENSES_TOOL_ID, SENSE_EXECUTE_TOOL_ID})
RAW_CONNECTOR_IDS = frozenset({LIST_CONNECTOR_ACTIONS_TOOL_ID, CONNECTOR_EXECUTE_TOOL_ID})


def _instance(config: Any) -> Any:
    return SimpleNamespace(config=config)


def _ctx(kind: ScopeKind = ScopeKind.CONCIERGE) -> ScopeContext:
    return ScopeContext(
        kind=kind,
        scope_id="pk_site_1",
        workspace_id="w1",
        user_id="anon_visitor",
        members=[],
        target_agent_id="a_concierge",
        pocket_id="pk_site_1",
    )


# ---------------------------------------------------------------------------
# _concierge_sense_policy (pure unit)
# ---------------------------------------------------------------------------


def test_mounted_concierge_gets_the_sense_tools():
    """A site agent carrying senses has the two ids unioned into the surface
    allow-list, so they survive the concierge lockdown."""
    deny, allow = run_core._concierge_sense_policy(
        _ctx(),
        _instance({"senses": ["paw.email.v1"]}),
        frozenset({"WebFetch"}),
        frozenset({"mcp__pawbar_actions__pawbar_book"}),
    )
    assert allow is not None
    assert SENSE_IDS <= allow
    # The widget's own action tool is preserved, not replaced.
    assert "mcp__pawbar_actions__pawbar_book" in allow
    # Deny is untouched, and the RAW connector surface is never granted.
    assert deny == frozenset({"WebFetch"})
    assert not (RAW_CONNECTOR_IDS & allow)


def test_unmounted_concierge_is_denied_not_merely_ungranted():
    """The load-bearing half: an EMPTY mount list DENIES the ids. Absence from
    the allow-list alone would leave other grant paths able to re-add them."""
    deny, allow = run_core._concierge_sense_policy(
        _ctx(),
        _instance({"senses": []}),
        frozenset({"WebFetch"}),
        frozenset(),
    )
    assert SENSE_IDS <= deny
    assert "WebFetch" in deny
    assert allow == frozenset()


def test_concierge_with_no_senses_key_is_denied():
    """A config predating the field (no ``senses`` key) is unmounted → denied.
    Fail-closed is the default for anything that isn't an explicit opt-in."""
    deny, allow = run_core._concierge_sense_policy(_ctx(), _instance({}), frozenset(), frozenset())
    assert SENSE_IDS <= deny
    assert allow == frozenset()


def test_object_config_is_supported_both_ways():
    """The reader tolerates an object config (getattr path), like _agent_tool_policy."""
    mounted = SimpleNamespace(senses=["paw.calendar.v1"])
    _, allow = run_core._concierge_sense_policy(
        _ctx(), _instance(mounted), frozenset(), frozenset()
    )
    assert allow is not None and SENSE_IDS <= allow

    unmounted = SimpleNamespace(senses=[])
    deny, _ = run_core._concierge_sense_policy(
        _ctx(), _instance(unmounted), frozenset(), frozenset()
    )
    assert SENSE_IDS <= deny


def test_unrestricted_allow_list_is_not_turned_into_a_restriction():
    """``allow_mcp is None`` means the surface set NO MCP restriction. Unioning
    would invent one and strip every other tool, so a mounted agent leaves it
    None (the tools already flow)."""
    deny, allow = run_core._concierge_sense_policy(
        _ctx(), _instance({"senses": ["paw.email.v1"]}), frozenset(), None
    )
    assert allow is None
    assert deny == frozenset()


def test_unmounted_concierge_is_denied_even_with_no_restriction():
    """The deny half still applies when the allow-list is unrestricted — that is
    the path an ``exclusive`` agent or a missing profile would otherwise take."""
    deny, allow = run_core._concierge_sense_policy(
        _ctx(), _instance({"senses": []}), frozenset(), None
    )
    assert SENSE_IDS <= deny
    assert allow is None


@pytest.mark.parametrize(
    "kind",
    [ScopeKind.SESSION, ScopeKind.POCKET, ScopeKind.DM, ScopeKind.GROUP],
)
def test_non_concierge_scopes_are_untouched(kind):
    """DM / pocket / session runs keep SP2-3's documented empty=inherit
    semantics — this slice changes nothing off the public surface."""
    deny_in, allow_in = frozenset({"WebFetch"}), frozenset({"mcp__x__y"})
    for config in ({"senses": []}, {"senses": ["paw.email.v1"]}, {}):
        deny, allow = run_core._concierge_sense_policy(
            _ctx(kind), _instance(config), deny_in, allow_in
        )
        assert deny == deny_in
        assert allow == allow_in


# ---------------------------------------------------------------------------
# _drive_agent_loop → run_kwargs wiring
# ---------------------------------------------------------------------------


def _profile(*, allow_mcp: frozenset[str] | None, deny: frozenset[str] = frozenset()) -> Any:
    return SimpleNamespace(
        deny_mcp_tool_ids=deny,
        allowed_sdk_tools=frozenset(),
        allow_mcp_tool_ids=allow_mcp,
        system_message_override=None,
        skill_names=frozenset(),
    )


async def _drain_run_kwargs(monkeypatch, ctx, agent_config: dict[str, Any]) -> dict[str, Any]:
    """Drive _drive_agent_loop far enough to capture the run_kwargs the pool.run
    seam receives. Mirrors the harness in test_run_core_agent_tool_policy.py."""
    captured: dict[str, Any] = {}

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config=agent_config, agent_name="Concierge")

        def run(self, agent_id, content, session_key, **run_kwargs):
            captured["run_kwargs"] = run_kwargs

            async def _gen():
                return
                yield  # pragma: no cover

            return _gen()

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)

    async def _never_cancelled():
        return False

    gen = run_core._drive_agent_loop(
        ctx,
        user_content="do you deliver on sundays?",
        attachments_in=None,
        mentions_in=None,
        history=None,
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    )
    async for _ in gen:
        pass
    return captured


@pytest.mark.asyncio
async def test_run_kwargs_grant_sense_tools_for_mounted_concierge(monkeypatch):
    ctx = _ctx()
    ctx.resolved_profile = _profile(allow_mcp=frozenset({"mcp__pawbar_actions__pawbar_book"}))
    rk = (await _drain_run_kwargs(monkeypatch, ctx, {"senses": ["paw.email.v1"]}))["run_kwargs"]

    assert SENSE_IDS <= rk["allow_mcp_tool_ids"]
    assert "mcp__pawbar_actions__pawbar_book" in rk["allow_mcp_tool_ids"]
    assert not (SENSE_IDS & rk["deny_mcp_tool_ids"])
    # Raw connector tools are NOT part of this unlock.
    assert not (RAW_CONNECTOR_IDS & rk["allow_mcp_tool_ids"])


@pytest.mark.asyncio
async def test_run_kwargs_deny_sense_tools_for_unmounted_concierge(monkeypatch):
    ctx = _ctx()
    ctx.resolved_profile = _profile(allow_mcp=frozenset())
    rk = (await _drain_run_kwargs(monkeypatch, ctx, {"senses": []}))["run_kwargs"]

    assert SENSE_IDS <= rk["deny_mcp_tool_ids"]
    assert not (SENSE_IDS & rk["allow_mcp_tool_ids"])


@pytest.mark.asyncio
async def test_unmounted_exclusive_concierge_cannot_self_grant(monkeypatch):
    """The airtight case. An EXCLUSIVE agent's declared tools OVERRIDE the
    surface allow-list (CX-2), so the allow-list alone could not stop it. The
    deny half still does: the backend subtracts deny from the final tool list
    before any grant re-adds anything, so an unmounted public concierge cannot
    hand itself the sense surface by declaring the ids."""
    ctx = _ctx()
    ctx.resolved_profile = _profile(allow_mcp=frozenset())
    rk = (
        await _drain_run_kwargs(
            monkeypatch,
            ctx,
            {"tool_mode": "exclusive", "tools": list(SENSE_IDS), "senses": []},
        )
    )["run_kwargs"]

    assert rk["exclusive_mcp_tools"] is True
    # It kept its declared ids on the allow-list (CX-2 precedence is unchanged) …
    assert SENSE_IDS <= rk["allow_mcp_tool_ids"]
    # … and they are denied anyway, which is what the backend applies first.
    assert SENSE_IDS <= rk["deny_mcp_tool_ids"]


@pytest.mark.asyncio
async def test_non_concierge_run_is_byte_identical(monkeypatch):
    """A SESSION run with the same agent config is untouched — no grant, no deny."""
    ctx = _ctx(ScopeKind.SESSION)
    ctx.resolved_profile = _profile(allow_mcp=frozenset({"mcp__surface__tool"}))
    rk = (await _drain_run_kwargs(monkeypatch, ctx, {"senses": []}))["run_kwargs"]

    assert rk["allow_mcp_tool_ids"] == frozenset({"mcp__surface__tool"})
    assert rk["deny_mcp_tool_ids"] == frozenset()
