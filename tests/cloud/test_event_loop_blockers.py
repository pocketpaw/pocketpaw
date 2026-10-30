# tests/cloud/test_event_loop_blockers.py
# Guards the "keep blocking work off the event loop" fixes: a slow kb-go
# subprocess must not freeze other coroutines, the audit webhook fan-out is
# capped by a semaphore and resolves DNS through the loop's async resolver
# (SSRF check intact), the decision-graph reads run in a worker thread, and
# the three process-global caches stay bounded.

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.audit import webhooks
from pocketpaw_ee.cloud.kb import router as kb_router


async def _ticks_during(coro, *, tick: float = 0.01) -> int:
    """Run ``coro`` next to a ticker and return how often the ticker ran."""
    count = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal count
        while not done.is_set():
            count += 1
            await asyncio.sleep(tick)

    t = asyncio.create_task(ticker())
    try:
        await coro
    finally:
        done.set()
        await t
    return count


async def test_kb_route_does_not_block_the_loop(monkeypatch) -> None:
    def slow_kb(*_args, **_kwargs):
        time.sleep(0.3)  # a stuck kb-go child, as seen from the caller
        return {"articles": 1}

    monkeypatch.setattr(kb_router, "_kb", slow_kb)
    ticks = await _ticks_during(kb_router.kb_stats(workspace_id="w1", user_id="u1"))
    # A blocking call inline would let the ticker run once; off the loop it
    # keeps ticking for the whole 300 ms.
    assert ticks >= 10


async def test_kb_subprocess_has_its_own_timeout(monkeypatch) -> None:
    """``wait_for`` around ``to_thread`` can't kill a child, so ``_kb`` must pass
    ``timeout=`` to ``subprocess.run`` itself."""
    import subprocess

    from pocketpaw_ee.cloud.agents import knowledge

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(knowledge.subprocess, "run", fake_run)
    knowledge._kb("list", "--scope", "workspace:w1")
    assert seen.get("timeout")


async def test_webhook_fanout_is_capped(monkeypatch) -> None:
    running = 0
    peak = 0
    release = asyncio.Event()

    async def fake_deliver(_event) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    monkeypatch.setattr(webhooks, "deliver", fake_deliver)
    total = webhooks._MAX_CONCURRENT_DELIVERIES + 4
    for _ in range(total):
        webhooks.schedule_delivery(MagicMock())
    for _ in range(5):
        await asyncio.sleep(0)
    assert peak == webhooks._MAX_CONCURRENT_DELIVERIES
    release.set()
    await asyncio.gather(*list(webhooks._inflight_deliveries))
    assert running == 0  # the queued ones ran too; none were dropped


async def test_webhook_ssrf_check_uses_async_resolver(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", ("10.0.0.5", 0))])
    )
    with pytest.raises(Forbidden):
        await webhooks._validate_url_safety("https://siem.example.com/in")
    loop.getaddrinfo.assert_awaited_once()

    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])
    )
    await webhooks._validate_url_safety("https://siem.example.com/in")  # public: allowed


async def test_decision_graph_reads_run_off_the_loop() -> None:
    from uuid import uuid4

    from pocketpaw_ee.cloud.decisions.service import DecisionGraph

    loop_thread = threading.get_ident()
    seen: list[int] = []
    store = MagicMock()
    store.get_decision.side_effect = lambda _id: seen.append(threading.get_ident())
    graph = DecisionGraph(store=store)
    assert await graph.get(uuid4()) is None
    assert seen and seen[0] != loop_thread


# --- cache bounds ------------------------------------------------------------


async def test_action_override_cache_is_bounded(monkeypatch) -> None:
    from pocketpaw_ee.cloud.workspace import service as ws_service
    from pocketpaw_ee.guards import deps

    monkeypatch.setattr(deps, "_ACTION_OVERRIDE_MAX", 3)
    monkeypatch.setattr(deps, "_ACTION_OVERRIDE_CACHE", {})
    monkeypatch.setattr(ws_service, "get_member_action_overrides", AsyncMock(return_value=[]))
    # An expired entry is evicted first, ahead of the oldest live one.
    deps._ACTION_OVERRIDE_CACHE[("w", "expired")] = (0.0, [])
    for i in range(5):
        await deps._has_action_override("w", f"u{i}", "x")
    assert len(deps._ACTION_OVERRIDE_CACHE) <= 3
    assert ("w", "expired") not in deps._ACTION_OVERRIDE_CACHE
    assert ("w", "u4") in deps._ACTION_OVERRIDE_CACHE


async def test_compiled_with_cache_is_bounded(monkeypatch) -> None:
    from pocketpaw_ee.cloud.files import content_search

    monkeypatch.setattr(content_search, "_COMPILED_WITH_MAX", 3)
    monkeypatch.setattr(content_search, "_compiled_with_cache", {"old": (0.0, {})})
    monkeypatch.setattr(content_search, "_default_kb_list", AsyncMock(return_value=[]))
    for i in range(5):
        await content_search._compiled_with_for_scope(f"s{i}", None)
    assert len(content_search._compiled_with_cache) <= 3
    assert "old" not in content_search._compiled_with_cache
    assert "s4" in content_search._compiled_with_cache


def test_composio_tools_cache_is_bounded(monkeypatch) -> None:
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
    from pocketpaw_ee.cloud.composio import providers

    from pocketpaw.config import Settings

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        composio_api_key="ck",
        composio_enterprise_id="ent",
        composio_toolkits=["gmail"],
    )
    users = iter(f"user{i}" for i in range(5))
    monkeypatch.setattr(
        providers,
        "_resolve_ctx",
        lambda: RequestContext(
            user_id=next(users),
            workspace_id="w",
            request_id="r",
            scope=ScopeKind.NONE,
            started_at=datetime.now(UTC),
        ),
    )
    monkeypatch.setattr(providers, "_build_session_and_tools", lambda *a: ["tool"])
    monkeypatch.setattr(providers, "_TOOLS_CACHE_MAX", 3)
    monkeypatch.setattr(providers, "_tools_cache", {})
    for _ in range(5):
        assert providers.build_tools_for_backend("claude_agent_sdk", settings=settings) == ["tool"]
    assert len(providers._tools_cache) <= 3
    assert ("ent:user4", "claude_agent_sdk") in providers._tools_cache
