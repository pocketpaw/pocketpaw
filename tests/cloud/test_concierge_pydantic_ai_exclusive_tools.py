# tests/cloud/test_concierge_pydantic_ai_exclusive_tools.py — the concierge gets only its own tools.
# Updated 2026-09-27 (same branch, with the fix): step 2 now goes through the
# real ``AgentPool.run`` rather than filtering kwargs itself, so the pool's new
# ``exclusive_tools`` forward is under test too. Added the site-pocket override
# test and the claude_sdk ``_build_options`` test. Mutations:
# tests/mutations/concierge_exclusive_tools.json.
# Created 2026-09-27 (fix/concierge-web-tool-deny, second fix): the Paw Bar
# concierge answers anonymous visitors on a customer's site. On the pydantic_ai
# backend its deny list only ever reached tools it named, so the run was still
# offered every bridged builtin in ``_TENANT_SAFE_TOOLS``: the owner's connectors,
# memory writes, deliver_artifact, flows, paid media APIs, add_widget. A deny list
# cannot keep up with that set; the concierge has to be offered ONLY its own
# tools: this widget's ``pawbar_<verb>`` actions and ``pawbar_request_human``.
#
# The test is end to end on the tool path:
#   1. ``run_core._drive_agent_loop`` runs a real CONCIERGE context whose profile
#      comes from ``resolve_profile`` with a widget that declares an action, and
#      the kwargs it hands ``AgentPool.run`` are captured.
#   2. Those kwargs go to the real ``AgentPool.run`` over a real
#      ``PydanticAIBackend``, so the pool's own forwarding decides what reaches
#      the backend, and the tools the MODEL is offered are read from ``AgentInfo``.
# The bridged builtins are the real ones. The in-process MCP servers are a stub
# shaped like the bridge's output, with names derived from the real
# ``pawbar_tool_id`` / ``handoff_tool_id``, because the real pawbar server needs
# a bound concierge run to build its tools.
#
# A control run with a /chat profile proves the builtins really are on the
# surface, so the concierge assertion cannot pass by them not existing.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pydantic_ai", reason="pocketpaw[pydantic-ai] not installed")

from pocketpaw_ee.agent.mcp_servers.pawbar import handoff_tool_id, pawbar_tool_id  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind  # noqa: E402
from pocketpaw_ee.cloud.chat.runs import run_core  # noqa: E402
from pocketpaw_ee.cloud.surface import (  # noqa: E402
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    resolve_profile,
)
from pydantic_ai.messages import ModelMessage  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.toolsets import FunctionToolset, PrefixedToolset  # noqa: E402

from pocketpaw.agents.pool import AgentPool  # noqa: E402
from pocketpaw.agents.pydantic_ai import PydanticAIBackend, _normalize_tool_id  # noqa: E402
from pocketpaw.config import Settings  # noqa: E402

_ACTIONS = [
    {"verb": "add_to_cart", "policy": "auto", "args": {"product_id": "str", "qty": "int"}},
]

# The concierge's own tools, as the pydantic_ai backend names them.
_OWN = {
    _normalize_tool_id(pawbar_tool_id("add_to_cart")),
    _normalize_tool_id(handoff_tool_id()),
}

# A sample of what a public visitor must never reach: the owner's integrations,
# delivery, flows, paid APIs and the pocket write tools. Memory writes are left
# out of the SAMPLE only: ``remember`` / ``forget`` are replaced by the soul_*
# tools whenever a soul is active, which another test in the same process can
# switch on, so the control would flake. The exact-set assertion still excludes
# every memory tool under either name.
_NEVER = {
    "connector_execute",
    "connector_connect",
    "deliver_artifact",
    "start_flow",
    "run_step_pipeline",
    "image_generate",
    "text_to_speech",
    "add_widget",
    "remove_widget",
    "create_pocket",
    "web_search",
}


def _inprocess_stub() -> list:
    """Toolsets shaped like ``build_inprocess_mcp_toolsets`` output.

    The pawbar server carries the widget's action tool plus the handoff tool; a
    second server stands in for the pocket-lifecycle tools every other surface
    gets, so the MCP gate is exercised in both directions.
    """

    def _make(name: str):
        async def _tool(q: str) -> str:
            return ""

        _tool.__name__ = name
        _tool.__doc__ = f"The {name} tool."
        return _tool

    pawbar = [pawbar_tool_id("add_to_cart"), handoff_tool_id()]
    pawbar_names = [tid.split("__")[-1] for tid in pawbar]
    return [
        PrefixedToolset(FunctionToolset([_make(n) for n in pawbar_names]), "pawbar_actions"),
        PrefixedToolset(FunctionToolset([_make("get_pocket")]), "pocketpaw_pocket"),
    ]


def _backend() -> tuple[PydanticAIBackend, set[str]]:
    backend = PydanticAIBackend(
        Settings(
            pydantic_ai_model="litellm:test-model",
            litellm_api_base="http://localhost:4000",
            litellm_api_key="sk-test",
            pydantic_ai_skills_enabled=False,
            # The harness adds its own tools (write_plan, read_tool_result). They
            # are not what this test is about, and leaving them on would make
            # "exactly the concierge's tools" fail for the wrong reason.
            pydantic_ai_harness_enabled=False,
        )
    )
    backend._mcp_tools = _inprocess_stub()
    seen: set[str] = set()

    async def capture(messages: list[ModelMessage], info: AgentInfo):
        seen.update(t.name for t in info.function_tools)
        seen.update(type(t).__name__ for t in info.model_request_parameters.native_tools or [])
        yield "ok"

    model = FunctionModel(stream_function=capture)
    backend._build_model = lambda *_a, **_k: model  # type: ignore[method-assign]
    return backend, seen


class _CapturingPool:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "pydantic_ai"}})()

    def run(self, *args, **kwargs):
        self.run_kwargs = kwargs

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


def _ctx(kind: SurfaceKind, meta: SurfaceMeta) -> ScopeContext:
    ctx = ScopeContext(
        kind=ScopeKind.CONCIERGE if kind is SurfaceKind.CONCIERGE else ScopeKind.SESSION,
        scope_id="pk-1",
        workspace_id="ws-1",
        user_id="visitor-1",
        members=[],
        target_agent_id="agent-1",
        pocket_id="pk-1",
        surface_context=SurfaceContext(
            workspace_id="ws-1",
            user_id="visitor-1",
            kind=kind,
            meta=meta,
            preamble="",
        ),
    )
    ctx.resolved_profile = resolve_profile(kind, meta)
    return ctx


async def _pool_kwargs(monkeypatch, ctx: ScopeContext) -> dict[str, Any]:
    """What ``run_core`` hands ``AgentPool.run`` for this context."""
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _no_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _no_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    async def _not_cancelled():
        return False

    async for _ in run_core._drive_agent_loop(
        ctx,
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_not_cancelled,
        emit_stream_start=False,
    ):
        pass
    assert pool.run_kwargs is not None, "run_core never reached AgentPool.run"
    return pool.run_kwargs


async def _offered(
    monkeypatch, backend: PydanticAIBackend, seen: set[str], pool_kwargs: dict
) -> set[str]:
    """Hand ``run_core``'s kwargs to the REAL ``AgentPool.run`` over ``backend``.

    The pool decides which kwargs reach the backend (withhold-when-empty plus a
    signature gate per kwarg), so it has to be the real one: a test that
    forwarded the kwargs itself would pass with the pool dropping the flag.
    """
    pool = AgentPool()
    inst = SimpleNamespace(
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "P", "system_prompt": ""},
        last_active=datetime.now(UTC),
        active_runs=0,
    )

    async def _fake_get(agent_id):
        return inst

    monkeypatch.setattr(pool, "get", _fake_get)
    seen.clear()
    events = [ev async for ev in pool.run("agent-1", "hi", "session:s1", **pool_kwargs)]
    errors = [getattr(ev, "content", ev) for ev in events if getattr(ev, "type", "") == "error"]
    assert not errors, f"run failed before the model saw a request: {errors}"
    return set(seen)


async def test_concierge_on_pydantic_ai_is_offered_only_its_own_tools(monkeypatch):
    meta = SurfaceMeta(widget_id="widget-1", pawbar_actions=_ACTIONS)

    control_kwargs = await _pool_kwargs(monkeypatch, _ctx(SurfaceKind.CHAT, SurfaceMeta()))
    backend, seen = _backend()
    control = await _offered(monkeypatch, backend, seen, control_kwargs)
    assert _NEVER <= control, (
        f"control: /chat should offer every sampled builtin; missing {sorted(_NEVER - control)}"
    )

    concierge_kwargs = await _pool_kwargs(monkeypatch, _ctx(SurfaceKind.CONCIERGE, meta))
    backend, seen = _backend()
    offered = await _offered(monkeypatch, backend, seen, concierge_kwargs)
    assert offered == _OWN, (
        f"concierge must be offered exactly {sorted(_OWN)}; "
        f"extra {sorted(offered - _OWN)}, missing {sorted(_OWN - offered)}"
    )


async def test_a_site_pocket_override_cannot_unlock_the_concierge(monkeypatch):
    """The concierge runs on the site's pocket, and a pocket may carry its own
    ``surface_profile``. ``compose_entity_profile`` rebuilds the profile field by
    field, so a field it forgets is silently reset to its default: here that
    would hand the concierge every builtin back the moment the owner saved any
    override at all."""
    from pocketpaw_ee.cloud.surface import compose_entity_profile

    meta = SurfaceMeta(widget_id="widget-1", pawbar_actions=_ACTIONS)
    ctx = _ctx(SurfaceKind.CONCIERGE, meta)
    ctx.resolved_profile = compose_entity_profile(
        ctx.resolved_profile, {"skill_names": ["github"], "exclusive_tools": False}
    )
    assert ctx.resolved_profile.exclusive_tools is True

    kwargs = await _pool_kwargs(monkeypatch, ctx)
    backend, seen = _backend()
    offered = await _offered(monkeypatch, backend, seen, kwargs)
    assert offered == _OWN, f"extra {sorted(offered - _OWN)}"


async def test_concierge_on_claude_sdk_gets_no_grant_and_no_always_allowed_servers(monkeypatch):
    """The same run_core kwargs on the Claude SDK backend, through the real
    ``_build_options`` allowlist computation.

    Before the profile was exclusive, the restrictive allow set still unioned
    the universal pocket grant, the widget and atlas ids and every
    always-allowed server (composio connectors, pocket lifecycle) back in. The
    candidate pool below holds one of each, so their absence is proven rather
    than assumed. What remains is the concierge's own tools plus the base SDK
    built-ins the deny set does not name (Read, Glob, Grep), pinned here so a
    change to that residue is a visible test edit.
    """
    from pocketpaw.agents.claude_sdk import POCKET_CREATION_GRANT, ClaudeSDKBackend
    from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS
    from pocketpaw.config import get_settings

    own_ids = {pawbar_tool_id("add_to_cart"), handoff_tool_id()}
    meta = SurfaceMeta(widget_id="widget-1", pawbar_actions=_ACTIONS)
    kwargs = await _pool_kwargs(monkeypatch, _ctx(SurfaceKind.CONCIERGE, meta))
    # Every POCKET_CREATION_GRANT id is already in the concierge deny set, so the
    # grant's reach is shown by the widget and atlas ids it unions in with it.
    assert POCKET_CREATION_GRANT <= kwargs["deny_mcp_tool_ids"]
    leaks = {
        "mcp__composio__GMAIL_SEND_EMAIL",
        "mcp__pocketpaw_pocket__get_pocket",
        sorted(WIDGET_TOOL_IDS)[0],
        sorted(ATLAS_TOOL_IDS)[0],
    }
    candidates = sorted(own_ids | leaks)

    backend = ClaudeSDKBackend(get_settings())
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: list(candidates))
    built = await backend._build_options(
        "hi",
        system_prompt="you are a site concierge",
        history=None,
        session_key=None,
        deny_mcp_tool_ids=kwargs.get("deny_mcp_tool_ids", frozenset()),
        allow_sdk_tools=kwargs.get("allow_sdk_tools", frozenset()),
        allow_mcp_tool_ids=kwargs.get("allow_mcp_tool_ids"),
        skill_names=kwargs.get("skill_names", frozenset()),
        stderr_sink=[],
        exclusive_mcp_tools=kwargs.get("exclusive_mcp_tools", False),
    )
    effective = set(built.options_kwargs["allowed_tools"])
    mcp = {t for t in effective if t.startswith("mcp__")}

    assert mcp == own_ids, f"extra {sorted(mcp - own_ids)}, missing {sorted(own_ids - mcp)}"
    assert effective - mcp <= {"Read", "Glob", "Grep"}, sorted(effective - mcp)
