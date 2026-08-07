# ee/pocketpaw_ee/cloud/mandates/soul_link.py
# Created: 2026-06-11 (feat/belt-mandates, slice 6 — soul wiring, demo bar).
#
# Updated: 2026-08-07 (feat/coupling-t17-foreman-agent) — this module was the
#   mandate ↔ SOUL bridge; it is now the mandate ↔ AGENT ↔ soul bridge. The
#   foreman used to be an anonymous LLM shell-out whose accumulated shift memory
#   landed in whatever free-form ``MandateDoc.soul_path`` someone typed. It now
#   runs under a real Agent identity, so its memory belongs on THAT agent's
#   soul — the same file the AgentPool writes when the agent chats.
#
#   Added here:
#     * ``ForemanAgent``          — the frozen slice of an Agent the foreman
#                                   needs (identity + system_prompt + scopes).
#     * ``resolve_foreman_agent`` — mandate's ``agent_id`` → the workspace's
#                                   seeded default ``pocketpaw`` agent → None.
#     * ``agent_soul_path``       — the AgentPool convention, nothing new.
#     * ``resolve_soul_path``     — the precedence rule + optional materialize.
#
#   SOUL PATH PRECEDENCE (the migration / compat story — read this before
#   changing anything):
#     1. ``MandateDoc.soul_path``, when set. A mandate created BEFORE this
#        change bound a free-form path and has real memories in that file. It
#        keeps winning, verbatim, forever. Nothing is migrated, moved, or
#        rewritten — an existing mandate's shifts read and write exactly where
#        they always did.
#     2. Otherwise the bound agent's soul at the AgentPool convention
#        ``~/.pocketpaw/souls/{workspace}/{slug}.soul``
#        (``src/pocketpaw/agents/pool.py::_init_soul``). This is the path every
#        NEW mandate takes, since new mandates leave ``soul_path`` unset.
#     3. Otherwise ``None`` — recall returns empty, remember no-ops. A mandate
#        with no agent and no path still runs its shift; it just accumulates no
#        long-lived memory.
#   ``soul_path`` is therefore an OVERRIDE, not a parallel feature, and the
#   field stays on the doc for exactly that reason. Zero-migration by design.
#
#   MATERIALIZATION: ``seed_default_agent`` inserts its Agent doc directly and
#   never ran the eager-soul step, so the default ``pocketpaw`` agent in every
#   existing workspace has a doc but NO soul file on disk — and both functions
#   below skip a missing file. Resolving with ``materialize=True`` (the write
#   path) asks the agents service to create it via the pool's own
#   ``ensure_soul``, so an agent-resolved shift memory actually lands instead of
#   silently no-op'ing forever.
#
# The mandate ↔ soul bridge. The foreman RECALLS context from the resolved soul
# before planning, and every finished shift APPENDS an episodic summary to it —
# the mandate accumulates long-lived judgment context across shifts, now on the
# identity that did the judging.
#
# Clean, narrow interface so the transport can be swapped without touching the
# foreman/service:
#   * ``resolve_foreman_agent(workspace_id, agent_id)`` -> ForemanAgent | None
#   * ``resolve_soul_path(...)``                        -> str | None
#   * ``recall_for_planning(soul_path, query)``         -> list[str]
#   * ``remember_shift(soul_path, summary)``            -> bool
#
# Implementation rides the real soul-protocol API (``Soul.awaken`` →
# ``recall``/``remember`` → ``save_local``). EVERYTHING is best-effort: a
# missing agent, a missing file, a corrupt soul, or a protocol error logs and
# degrades to an empty recall / no-op remember. THE SHIFT IS THE VALUABLE
# THING; the soul write is expendable. Nothing in this module may raise.

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# How many memories a planning recall pulls.
_RECALL_LIMIT = 5

# The slug ``seed_default_agent`` gives every workspace's default agent
# (``cloud/agents/service.py::seed_default_agent``, called from
# ``cloud/workspace/service.py`` on workspace create). The chat path resolves
# the same way in ``cloud/chat/agent_service.py::_get_default_workspace_agent_id``
# — we go through the agents service's public ``get_by_slug`` rather than
# re-querying the Beanie doc, so this is the SAME resolution, not a third one.
_DEFAULT_AGENT_SLUG = "pocketpaw"


@dataclass(frozen=True)
class ForemanAgent:
    """The slice of an Agent the mandate foreman actually needs.

    A frozen projection (not the Beanie doc, not the full domain object) so the
    foreman/prompt layer never reaches into agent internals. ``system_prompt``
    and ``scopes`` are what the planning prompt inherits; ``trust_level`` and
    ``skill_refs`` ride along so the governance story is legible at the call
    site even though the demo-bar prompt only renders the first two.
    """

    id: str
    workspace_id: str
    name: str
    slug: str
    system_prompt: str = ""
    scopes: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    trust_level: int = 3
    soul_enabled: bool = True


async def resolve_foreman_agent(workspace_id: str, agent_id: str | None) -> ForemanAgent | None:
    """Resolve the agent whose identity the mandate's foreman runs under.

    Order: the mandate's bound ``agent_id`` → the workspace's seeded default
    ``pocketpaw`` agent → ``None``.

    The bound agent is REJECTED (and we fall through to the default) when it no
    longer exists, was soft-disabled, or belongs to another workspace — a
    deleted or revoked agent must never wedge a running shift, and a
    cross-tenant id must never bind. ``None`` is a legitimate outcome (a
    workspace with no default agent at all): the caller runs the shift anyway
    with no inherited prompt and no soul.

    Never raises.
    """
    if not workspace_id:
        return None

    if agent_id:
        resolved = await _load_agent(workspace_id, agent_id)
        if resolved is not None:
            return resolved
        logger.warning(
            "mandate foreman: bound agent %s is unusable (missing / disabled / "
            "cross-tenant) — falling back to the workspace default",
            agent_id,
        )

    return await _load_default_agent(workspace_id)


async def _load_agent(workspace_id: str, agent_id: str) -> ForemanAgent | None:
    """Load one agent by id and tenancy-check it. ``None`` on any miss."""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(agent_id)
    except Exception:  # noqa: BLE001 — a missing/malformed id is a fallback, not an error
        logger.debug("mandate foreman: agent %s lookup failed", agent_id, exc_info=True)
        return None
    return _project(agent, workspace_id)


async def _load_default_agent(workspace_id: str) -> ForemanAgent | None:
    """Load the workspace's seeded default ``pocketpaw`` agent. ``None`` on a
    miss — a workspace whose seed never ran degrades, it does not fail."""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get_by_slug(workspace_id, _DEFAULT_AGENT_SLUG)
    except Exception:  # noqa: BLE001 — no default agent is a degraded, runnable state
        logger.warning(
            "mandate foreman: workspace %s has no default '%s' agent — the shift "
            "runs with no inherited identity",
            workspace_id,
            _DEFAULT_AGENT_SLUG,
        )
        return None
    return _project(agent, workspace_id)


def _project(agent: object, workspace_id: str) -> ForemanAgent | None:
    """Map an agents-service domain object onto ``ForemanAgent``.

    Refuses a soft-disabled agent (AW-4 revokes it everywhere, including here)
    and a cross-workspace agent (tenancy). Returns ``None`` for either, so the
    caller falls through to the default."""
    try:
        if getattr(agent, "disabled", False):
            return None
        agent_workspace = str(getattr(agent, "workspace_id", "") or "")
        if agent_workspace != workspace_id:
            return None
        config = getattr(agent, "config", None)
        return ForemanAgent(
            id=str(getattr(agent, "id", "") or ""),
            workspace_id=agent_workspace,
            name=str(getattr(agent, "name", "") or ""),
            slug=str(getattr(agent, "slug", "") or ""),
            system_prompt=str(getattr(config, "system_prompt", "") or ""),
            scopes=tuple(getattr(config, "scopes", ()) or ()),
            skill_refs=tuple(getattr(config, "skill_refs", ()) or ()),
            trust_level=int(getattr(config, "trust_level", 3) or 3),
            soul_enabled=bool(getattr(config, "soul_enabled", True)),
        )
    except Exception:  # noqa: BLE001 — a malformed agent degrades to unbound
        logger.debug("mandate foreman: agent projection failed", exc_info=True)
        return None


def agent_soul_path(workspace_id: str, slug: str) -> str | None:
    """The agent's soul file, at the AgentPool convention.

    ``~/.pocketpaw/souls/{workspace}/{slug}.soul`` — the EXACT layout
    ``src/pocketpaw/agents/pool.py::_init_soul`` writes, so the foreman's shift
    memory lands in the same file the agent's chat runs read. Returns ``None``
    rather than raising if the config dir can't be resolved."""
    if not workspace_id or not slug:
        return None
    try:
        from pocketpaw.config import get_config_dir

        return str(get_config_dir() / "souls" / workspace_id / f"{slug}.soul")
    except Exception:  # noqa: BLE001 — no config dir means no soul, not a crash
        logger.debug("mandate foreman: soul path resolution failed", exc_info=True)
        return None


async def resolve_soul_path(
    *,
    workspace_id: str,
    agent: ForemanAgent | None,
    legacy_soul_path: str | None = None,
    materialize: bool = False,
) -> str | None:
    """Resolve the soul file this mandate's foreman reads and writes.

    Precedence (see the module header for the full compat story):
      1. ``legacy_soul_path`` — an explicitly-bound free-form path from a
         pre-agent mandate. Honored VERBATIM so existing memory keeps working.
      2. The bound agent's soul at the pool convention.
      3. ``None``.

    ``materialize=True`` (use it on the WRITE path) asks the agents service to
    create the agent's soul file when it is missing — the default ``pocketpaw``
    agent is seeded without one, so without this every agent-resolved shift
    memory would silently no-op. Materialization failure is not an error: we
    return the path anyway and let ``remember_shift`` skip it.

    Never raises.
    """
    if legacy_soul_path:
        return legacy_soul_path
    if agent is None or not agent.soul_enabled:
        return None

    path = agent_soul_path(workspace_id, agent.slug)
    if path is None:
        return None

    if materialize and not Path(path).expanduser().exists():
        try:
            from pocketpaw_ee.cloud.agents import service as agents_service

            await agents_service.ensure_soul_materialized(agent.id)
        except Exception:  # noqa: BLE001 — a soul write must never wedge a shift
            logger.warning(
                "mandate foreman: could not materialize the soul for agent %s",
                agent.id,
                exc_info=True,
            )
    return path


async def recall_for_planning(soul_path: str | None, query: str) -> list[str]:
    """Recall up to ``_RECALL_LIMIT`` memory lines relevant to ``query`` from
    the mandate's soul. Empty list when no soul is bound or anything fails."""
    if not soul_path:
        return []
    path = Path(soul_path).expanduser()
    if not path.exists():
        logger.warning("mandate soul: %s does not exist — empty recall", soul_path)
        return []
    try:
        from soul_protocol import Soul

        soul = await Soul.awaken(path)
        entries = await soul.recall(query, limit=_RECALL_LIMIT)
        return [str(e.content) for e in entries]
    except Exception:  # noqa: BLE001 — soul failures must never wedge a shift
        logger.warning("mandate soul: recall failed for %s", soul_path, exc_info=True)
        return []


async def remember_shift(soul_path: str | None, summary: str) -> bool:
    """Append an episodic shift summary to the mandate's soul and save it
    back in place. Returns True on success; False (logged) on any failure."""
    if not soul_path or not summary.strip():
        return False
    path = Path(soul_path).expanduser()
    if not path.exists():
        logger.warning("mandate soul: %s does not exist — remember skipped", soul_path)
        return False
    try:
        from soul_protocol import MemoryType, Soul

        soul = await Soul.awaken(path)
        await soul.remember(summary.strip(), type=MemoryType.EPISODIC, importance=7)
        await soul.save_local(path)
        return True
    except Exception:  # noqa: BLE001 — soul failures must never wedge a shift
        logger.warning("mandate soul: remember failed for %s", soul_path, exc_info=True)
        return False


__all__ = [
    "ForemanAgent",
    "agent_soul_path",
    "recall_for_planning",
    "remember_shift",
    "resolve_foreman_agent",
    "resolve_soul_path",
]
