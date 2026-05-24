# service.py — Workspace-level KB scope listing.
#
# Updated: 2026-05-24 — Bounded the probe fan-out via a module-level
# ``_PROBE_CONCURRENCY=8`` semaphore. The previous unbounded
# ``asyncio.gather`` could spawn one kb-go subprocess per candidate
# scope; on a workspace with 100 pockets + 50 agents that's 151
# concurrent subprocesses, which exceeds the default-executor
# thread-pool back-pressure (~32) and pressures FS + OS PID limits.
# The cap matches the thread-pool default and stops the fork storm.
#
# Created: 2026-05-24 — Adds the canonical ``list_scopes(workspace_id,
# user_id)`` helper so the /knowledge surface handler can render the
# real KB scopes attached to a workspace instead of the
# ``[f"workspace:{workspace_id}"]`` placeholder it shipped with.
#
# Why a new file in ee/cloud/kb/ rather than a method on
# ``agents.knowledge.KnowledgeService``: the existing class is an
# agent-scoped utility wrapper around the kb-go binary (its methods
# take ``agent_id``). A workspace-wide scope enumerator wants the
# pocket + agent listings the agents/pockets services already own, and
# parks naturally alongside ``workspace_aggregator.py`` in this
# directory — both modules are workspace-level KB aggregations.
"""Workspace-level KB scope listing.

The kb-go binary stores articles against scope strings of the shape
``workspace:{wid}``, ``pocket:{pid}``, ``agent:{aid}``. There's no
native ``list-scopes`` command on the binary — scopes are implicit in
whatever's been ingested. This module enumerates the **candidate** scopes
for a workspace (the workspace itself, every pocket the calling user
can see, every agent in the workspace), then probes each via the kb
list shim and keeps only those that actually carry articles.

Tenancy:
    The caller's ``user_id`` filters the pocket candidates through
    ``pockets_service.list_pockets``, which already gates by
    owner / shared_with / workspace-visible. Agent candidates come from
    ``agents_service.list_agents(workspace_id)`` and are pinned to the
    workspace at the source. Cross-workspace stamps therefore return
    nothing — both list functions filter on the explicit workspace_id.

Failure mode:
    The kb binary may be missing in lightweight deploys. The probe
    callback wraps subprocess calls in try/except; any failure returns
    an empty list for that scope, which excludes the scope from the
    result. The handler treats an empty list as "(no scopes detected)".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


# Cap on concurrent kb-go probes during ``list_scopes``. kb-go is sync
# I/O wrapped via ``asyncio.to_thread``, so each probe consumes a
# default-executor thread (~32 default). On a workspace with 100
# pockets + 50 agents, an unbounded gather would dispatch 150+
# subprocesses at once and pressure FS + OS PID limits even though the
# thread pool throttles wall-clock concurrency. 8 matches what
# typical workloads can sustain without saturating the executor and
# leaves headroom for the rest of the event loop.
_PROBE_CONCURRENCY = 8


# A probe callable returns the kb-go list rows for a given scope. The
# default uses the same kb-go subprocess wrapper the knowledge router
# already calls; tests inject a dict-backed fake so the listing path is
# exercised without a kb binary on the test runner.
KbListFn = Callable[[str], Awaitable[list[Any]] | list[Any]]


async def list_scopes(
    workspace_id: str,
    user_id: str,
    *,
    kb_list: KbListFn | None = None,
) -> list[str]:
    """Return KB scope strings that belong to ``workspace_id``.

    Parameters
    ----------
    workspace_id : str
        Required tenant. Empty string returns ``[]`` — we never probe a
        scope without a workspace pin (would leak across tenants if the
        listing layer ever forgot to filter).
    user_id : str
        The viewer. Pocket visibility is enforced through
        ``pockets_service.list_pockets``, so a user who can't see a
        pocket gets no entry for it in the scope list.
    kb_list : callable, optional
        Probe used to verify a candidate scope actually has articles.
        Async or sync; both call shapes are supported. Defaults to a
        thin wrapper around ``kb list --scope <s>``.

    Returns
    -------
    list[str]
        Scope strings of the form ``workspace:{id}``, ``pocket:{id}``,
        ``agent:{id}`` — in workspace → pocket → agent order. Scopes
        with no articles (or unreachable kb backend) are excluded.
    """
    if not workspace_id:
        return []

    probe = kb_list if kb_list is not None else _default_kb_list
    candidates = await _candidate_scopes(workspace_id, user_id)

    # Probe in parallel under a bounded semaphore — each call is a
    # kb-go subprocess on the default path, so serialising would burn
    # wall-clock for nothing, but unbounded gather would spawn one
    # process per candidate (151 on a 100-pocket + 50-agent workspace)
    # and pressure FS + OS PID limits. ``_PROBE_CONCURRENCY`` matches
    # the default-executor thread-pool size, the natural back-pressure
    # boundary for the kb-go ``asyncio.to_thread`` wrappers.
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _gated(scope: str) -> bool:
        async with sem:
            return await _probe_scope(scope, probe)

    results = await asyncio.gather(
        *(_gated(scope) for scope in candidates),
        return_exceptions=False,
    )
    return [scope for scope, has_articles in zip(candidates, results) if has_articles]


async def _candidate_scopes(workspace_id: str, user_id: str) -> list[str]:
    """Enumerate candidate scopes for the workspace.

    Order is intentional — workspace first, then pockets, then agents.
    Preserves the natural "biggest container → smallest" reading order
    in the surface preamble.
    """
    scopes: list[str] = [f"workspace:{workspace_id}"]

    # Pocket scopes — gated by the calling user's visibility. The
    # pockets service is the only legal reader of ``_PocketDoc`` per
    # the entity rules; we never touch the doc directly here.
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pockets = await pockets_service.list_pockets(workspace_id, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pocket listing failed for workspace=%s: %s", workspace_id, exc)
        pockets = []
    for pocket in pockets:
        pocket_id = pocket.get("_id") if isinstance(pocket, dict) else None
        if pocket_id:
            scopes.append(f"pocket:{pocket_id}")

    # Agent scopes — workspace-pinned at the source. No per-user
    # filter: a workspace member sees every agent in the workspace by
    # convention (the agents page lists them all).
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agents = await agents_service.list_agents(workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent listing failed for workspace=%s: %s", workspace_id, exc)
        agents = []
    for agent in agents:
        agent_id = getattr(agent, "id", None)
        if agent_id:
            scopes.append(f"agent:{agent_id}")

    return scopes


async def _probe_scope(scope: str, probe: KbListFn) -> bool:
    """Return True when ``scope`` carries at least one kb-go article.

    Swallows every exception — the kb backend is optional, and a
    probe failure must not crash the preamble path. Logs at DEBUG so
    repeated failures (a missing kb binary in a dev box) don't spam
    the warning log.
    """
    try:
        result = probe(scope)
        if hasattr(result, "__await__"):
            rows = await result  # type: ignore[assignment]
        else:
            rows = result
    except Exception as exc:  # noqa: BLE001
        logger.debug("kb list failed for scope=%s: %s", scope, exc)
        return False
    return isinstance(rows, list) and len(rows) > 0


def _default_kb_list(scope: str) -> list[Any]:
    """Default kb-go ``list`` probe. Mirrors ``knowledge_router._call_kb_list``.

    Lives inline rather than imported from the router so this module
    has no router dependency — the router can be unmounted in a
    headless deploy and the surface preamble still resolves cleanly.
    """
    try:
        from pocketpaw_ee.cloud.agents.knowledge import _kb

        result = _kb("list", "--scope", scope)
    except Exception as exc:  # noqa: BLE001
        logger.debug("kb list raised for scope=%s: %s", scope, exc)
        return []
    return result if isinstance(result, list) else []


__all__ = ["list_scopes"]
