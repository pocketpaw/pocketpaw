# tests/cloud/test_concierge_pydantic_ai_web_deny.py — the concierge's web deny on pydantic_ai.
# Created 2026-09-27 (fix/concierge-web-tool-deny): the Paw Bar concierge (public,
# anonymous visitors on a customer's site) denies ``WebSearch`` / ``WebFetch`` in
# the Claude SDK's vocabulary. On the pydantic_ai backend, which new agents
# default to, those names matched nothing: the bridged tools are ``web_search``,
# ``url_extract`` and ``research``, so a concierge run was offered all three.
# With ``pydantic_ai_native_web_tools`` on, the native WebSearch / WebFetch
# capabilities were registered too, because they read the tool list before the
# surface deny is applied.
#
# Both tests use the REAL concierge profile (``resolve_profile``) and the REAL
# bridged tool set (``_build_custom_tools`` is not pinned), and read what the
# MODEL is offered from ``AgentInfo``. A control run with no deny proves the web
# tools are really on the surface, so the concierge assertion cannot pass by
# the tools simply not existing.
#
# Mutations that break them: drop the ``WebSearch`` / ``WebFetch`` rows from
# ``_SURFACE_TOOL_EQUIVALENTS`` (both tests), or drop the deny skip in
# ``_build_web_capabilities`` (the native test only).

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai", reason="pocketpaw[pydantic-ai] not installed")

from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta  # noqa: E402
from pocketpaw_ee.cloud.surface.service import resolve_profile  # noqa: E402
from pydantic_ai.messages import ModelMessage  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from pocketpaw.agents.pydantic_ai import PydanticAIBackend  # noqa: E402
from pocketpaw.config import Settings  # noqa: E402

_WEB_TOOLS = {"web_search", "url_extract", "research"}


def _backend(**overrides) -> tuple[PydanticAIBackend, dict[str, set[str]]]:
    """A backend with the real bridged tools and a model that records its offer.

    MCP toolsets are pinned empty so the test never spawns the developer's
    configured servers; skills are off because they add their own tools. The
    bridged builtins are left to build for real: a ``FunctionModel`` that only
    records ``AgentInfo`` executes none of them.
    """
    settings = Settings(
        pydantic_ai_model="litellm:test-model",
        litellm_api_base="http://localhost:4000",
        litellm_api_key="sk-test",
        pydantic_ai_skills_enabled=False,
        **overrides,
    )
    backend = PydanticAIBackend(settings)
    backend._mcp_tools = []
    seen: dict[str, set[str]] = {"function": set(), "native": set()}

    async def capture(messages: list[ModelMessage], info: AgentInfo):
        seen["function"].update(t.name for t in info.function_tools)
        # Provider-side tools ride ``native_tools`` on the request, not the
        # function tool list: a native capability is invisible to a check that
        # reads ``function_tools`` alone.
        seen["native"].update(
            type(t).__name__ for t in info.model_request_parameters.native_tools or []
        )
        yield "ok"

    model = FunctionModel(stream_function=capture)
    backend._build_model = lambda *_a, **_k: model  # type: ignore[method-assign]
    return backend, seen


async def _offered(backend: PydanticAIBackend, seen: dict[str, set[str]], **run_kwargs):
    seen["function"].clear()
    seen["native"].clear()
    events = [ev async for ev in backend.run("hi", **run_kwargs)]
    errors = [ev.content for ev in events if ev.type == "error"]
    assert not errors, f"run failed before the model saw a request: {errors}"
    return set(seen["function"]), set(seen["native"])


def _concierge_run_kwargs() -> dict:
    """The kwargs ``run_core`` forwards for a concierge run, from the real profile."""
    prof = resolve_profile(SurfaceKind.CONCIERGE, SurfaceMeta())
    return {
        "deny_mcp_tool_ids": prof.deny_mcp_tool_ids,
        "allow_mcp_tool_ids": prof.allow_mcp_tool_ids,
    }


async def test_concierge_run_on_pydantic_ai_is_offered_no_web_tools():
    backend, seen = _backend()

    control, _ = await _offered(backend, seen)
    assert _WEB_TOOLS <= control, (
        f"control: the unrestricted surface should carry {sorted(_WEB_TOOLS)}; "
        f"got {sorted(control & _WEB_TOOLS)}"
    )

    offered, native = await _offered(backend, seen, **_concierge_run_kwargs())
    leaked = offered & _WEB_TOOLS
    assert not leaked, f"concierge deny of WebSearch/WebFetch did not reach: {sorted(leaked)}"
    assert not native, f"native tools offered to the concierge: {sorted(native)}"


async def test_concierge_run_gets_no_native_web_capability():
    """With native web tools on, the capability must honour the surface deny too.

    ``_build_web_capabilities`` reads the bridged tool list itself, so a deny
    applied only to the plain tool list leaves it free to register WebSearch and
    WebFetch provider-side.
    """
    backend, seen = _backend(pydantic_ai_native_web_tools=True)

    _, control_native = await _offered(backend, seen)
    assert {"WebSearchTool", "WebFetchTool"} <= control_native, (
        f"control: native web tools should be on the request; got {sorted(control_native)}"
    )

    offered, native = await _offered(backend, seen, **_concierge_run_kwargs())
    assert not native & {"WebSearchTool", "WebFetchTool"}, (
        f"native web tool offered to the concierge: {sorted(native)}"
    )
    leaked = offered & _WEB_TOOLS
    assert not leaked, f"denied web tool still on the function tool list: {sorted(leaked)}"
