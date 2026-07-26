# ee/pocketpaw_ee/paw_bar/agent_provisioning.py — auto-provision a DEDICATED
# concierge agent per Paw Site.
#
# Created 2026-07-23 (feat/site-dedicated-agent): every site's concierge is
# answered by an agent that exists FOR that site — never a shared/universal
# agent. ``ensure_site_agent(site, widget)`` is idempotent: a widget already
# bound to a LIVE agent is returned unchanged (manual binds are never
# overwritten); otherwise ONE dedicated agent is created (via the agents service
# — never a direct Beanie write) and bound to the widget through the paw-bar
# store's ``update_fields`` path. Two triggers funnel here: widget-create (no
# agent_id + the pocket resolves to a Site) and concierge-enable (the site's
# widget is still unbound). Both are FAILURE-SOFT — a provisioning failure logs
# and leaves the widget unbound rather than 500-ing the caller (chat still 409s;
# the dashboard offers a manual create).
#
# ASG-1 identity fields (welcome_message, conversation_starters) and agent
# ``tags`` do NOT exist on the Agent/AgentConfig model on this branch, so the
# seeding of those degrades gracefully to a no-op (see ``_seed_identity`` /
# ``_seed_tags``). The derivation helpers still run + are unit-tested so the wire
# is in place the moment the fields land; the derived values are logged, never
# dropped silently.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The soul archetype every site concierge carries.
_CONCIERGE_ARCHETYPE = "The Site Concierge"

# Cap on conversation starters surfaced to a visitor (matches the frame config cap).
_MAX_STARTERS = 4

# Max length of the generated agent display name (Agent.name is capped at 100).
_MAX_AGENT_NAME = 100


def _store():
    """The paw-bar widget store (the sole owner of widget writes)."""
    from pocketpaw_ee.api import get_paw_bar_store

    return get_paw_bar_store()


def concierge_slug(site_id: str) -> str:
    """Deterministic, workspace-unique slug for a site's concierge agent.

    Deterministic on the site id so a retry after a partial provision (agent
    created but the bind failed) RESOLVES the same agent by slug instead of
    minting a duplicate — the idempotency backstop below relies on this.
    """
    return f"concierge-{site_id}"


def concierge_name(site_name: str) -> str:
    """The agent display name: ``<Site name> Concierge`` (``Site Concierge`` when
    the site has no name). Truncated to the Agent.name cap."""
    stripped = (site_name or "").strip()
    name = f"{stripped} Concierge" if stripped else "Site Concierge"
    return name[:_MAX_AGENT_NAME]


def concierge_persona(site_name: str) -> str:
    """The soul persona seeded on the concierge agent."""
    subject = (site_name or "").strip() or "this site"
    return (
        f"You are the concierge for {subject}. You answer visitors' questions "
        "about this site, grounded in its own knowledge."
    )


def derive_conversation_starters(widget: Any) -> list[str]:
    """Derive up to four plain visitor questions from the widget spec.

    Rules (order preserved, capped at ``_MAX_STARTERS``):
      * a non-empty catalog adds ``"What do you sell?"``;
      * each GATED action carrying a label adds ``"<label>?"`` (e.g. a
        ``book_table`` action labelled "Book a table" → "Book a table?");
      * if nothing derived, a single generic ``"What can you help me with?"``.
    """
    spec = getattr(widget, "spec", None)
    starters: list[str] = []
    if spec is not None and getattr(spec, "catalog", None):
        starters.append("What do you sell?")
    for action in getattr(spec, "actions", None) or []:
        if len(starters) >= _MAX_STARTERS:
            break
        label = (getattr(action, "label", "") or "").strip()
        if getattr(action, "policy", "") == "gated" and label:
            starters.append(f"{label}?")
    if not starters:
        starters.append("What can you help me with?")
    return starters[:_MAX_STARTERS]


def _seed_identity(body: Any, site: Any, widget: Any) -> None:
    """Seed the ASG-1 identity fields on a create body IF the model supports them.

    ``welcome_message`` ← ``Site.concierge_greeting`` (when non-empty),
    ``conversation_starters`` ← ``derive_conversation_starters(widget)``. On this
    branch the Agent create DTO carries NEITHER field, so this is a no-op beyond a
    debug log — do NOT add the fields here (that is an Agent-model change, out of
    scope). When the agent-studio identity fields land, this seeds them with no
    other change.
    """
    greeting = (getattr(site, "concierge_greeting", "") or "").strip()
    starters = derive_conversation_starters(widget)

    seeded = False
    if greeting and hasattr(body, "welcome_message"):
        body.welcome_message = greeting
        seeded = True
    if hasattr(body, "conversation_starters"):
        body.conversation_starters = starters
        seeded = True

    if not seeded:
        logger.debug(
            "paw-bar concierge: ASG-1 identity fields absent on Agent create DTO; "
            "skipping welcome_message/conversation_starters seeding "
            "(would have set greeting=%r, starters=%r)",
            greeting,
            starters,
        )


def _seed_tags(body: Any, site_id: str) -> None:
    """Stamp the ``["concierge", "site:<id>"]`` tags IF the create DTO supports a
    free-form ``tags`` field. The Agent model on this branch has no such field
    (``scopes`` is a hierarchical SCOPE-tag list with its own validator, NOT a
    label bag), so this is a graceful no-op here."""
    if hasattr(body, "tags"):
        body.tags = ["concierge", f"site:{site_id}"]
    else:
        logger.debug(
            "paw-bar concierge: Agent create DTO has no free-form 'tags' field; "
            "skipping tag seeding for site %s",
            site_id,
        )


async def _seed_connectors(agent: Any, site: Any) -> None:
    """Reserved seam for the connector-distribution wave (per-site connector
    auto-binding). Intentionally a no-op today — the concierge is public and runs
    fail-closed with NO connectors until the untrusted/public claude_sdk lockdown
    mode ships (see ``concierge_chat``'s connector-lockdown guard). Present so the
    provision path has one obvious place to wire connectors when that lands."""
    return None


async def ensure_site_agent(site: Any, widget: Any) -> str | None:
    """Idempotently ensure ``widget`` is bound to a dedicated agent for ``site``.

    Returns the bound agent id, or ``None`` when provisioning could not complete
    (the caller keeps the widget unbound — chat still 409s). Never overwrites a
    manual bind: if the widget already carries an ``agent_id`` that resolves to a
    LIVE agent, that id is returned unchanged. Otherwise ONE dedicated agent is
    created in the SITE's workspace, owned by the site owner, and bound to the
    widget through the store — mirroring how the agents service derives ownership,
    never cross-tenant.
    """
    from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    workspace_id = site.workspace
    owner_id = site.owner
    site_id = str(site.id)

    # (1) Respect an existing LIVE bind — a manual agent_id is never replaced.
    existing_id = getattr(widget, "agent_id", "") or ""
    if existing_id:
        try:
            await agents_service.get(existing_id)
            return existing_id
        except NotFound:
            # Stale bind (agent deleted) — fall through and re-provision.
            logger.info(
                "paw-bar concierge: widget %s bound to missing agent %s; re-provisioning",
                widget.id,
                existing_id,
            )

    # (2) Resolve-or-create the dedicated agent. The slug is deterministic on the
    # site id, so a create that races or retries after a failed bind RESOLVES the
    # same agent instead of raising a duplicate-slug conflict.
    slug = concierge_slug(site_id)
    ctx = agents_service.legacy_ctx(owner_id, workspace_id)

    agent = None
    try:
        agent = await agents_service.get_by_slug(workspace_id, slug)
    except NotFound:
        agent = None

    if agent is None:
        body = CreateAgentRequest(
            name=concierge_name(site.name),
            slug=slug,
            visibility="workspace",
            persona=concierge_persona(site.name),
            soul_archetype=_CONCIERGE_ARCHETYPE,
            # soul_enabled defaults True — the concierge carries a soul.
        )
        _seed_tags(body, site_id)
        _seed_identity(body, site, widget)
        try:
            agent = await agents_service.create(ctx, workspace_id, body)
        except ConflictError:
            # Lost a create race on the deterministic slug — adopt the winner.
            agent = await agents_service.get_by_slug(workspace_id, slug)

    # Connector seam (no-op today).
    await _seed_connectors(agent, site)

    # (3) Bind the agent to the widget through the store's whitelisted update path.
    updated = await _store().update_fields(
        widget.id, {"agent_id": agent.id}, workspace_id=workspace_id
    )
    if updated is None:
        logger.warning(
            "paw-bar concierge: agent %s created but bind to widget %s returned no row",
            agent.id,
            widget.id,
        )
        return None
    return agent.id


async def _canonical_site_for_pocket(workspace_id: str, pocket_id: str) -> Any | None:
    """The canonical Site doc for (workspace, pocket_id), or None (dedupe-aware).

    Reuses the sites service's canonical resolver so a pocket that still carries
    pre-dedupe duplicate Site docs resolves the SAME live doc the rest of the
    stack uses. Best-effort import: if the sites service can't be reached, no
    site is resolved (provisioning is skipped, the widget stays a plain widget).
    """
    if not pocket_id:
        return None
    try:
        from pocketpaw_ee.sites import service as sites_service

        return await sites_service.canonical_site_for_pocket(workspace_id, pocket_id)
    except Exception:  # noqa: BLE001 — never fail the caller on a resolver hiccup
        logger.warning(
            "paw-bar concierge: site resolution failed for pocket %s", pocket_id, exc_info=True
        )
        return None


async def provision_widget_on_create(widget: Any, workspace_id: str) -> Any:
    """Widget-create trigger: provision a concierge agent when the widget's pocket
    is a published Site and the widget is unbound.

    Returns the widget with ``agent_id`` set on success, or the ORIGINAL widget
    unchanged when there is no site for the pocket (plain widgets stay possible)
    or when provisioning fails. FAILURE-SOFT: any error logs and returns the
    original widget so widget-create never 500s on a provisioning problem.
    """
    if getattr(widget, "agent_id", ""):
        return widget
    try:
        site = await _canonical_site_for_pocket(workspace_id, widget.pocket_id)
        if site is None:
            return widget  # no site for this pocket — a plain widget, not a concierge
        await ensure_site_agent(site, widget)
        refreshed = await _store().get_widget(widget.id, workspace_id=workspace_id)
        return refreshed or widget
    except Exception:  # noqa: BLE001 — provisioning must never 500 widget-create
        logger.warning(
            "paw-bar concierge: auto-provision on widget-create failed for widget %s "
            "(returning unbound)",
            getattr(widget, "id", "?"),
            exc_info=True,
        )
        return widget


async def provision_on_concierge_enable(site: Any, workspace_id: str) -> None:
    """Concierge-enable trigger: provision the site's widget when it is unbound.

    Resolves the site's paw-bar widget (workspace-scoped) and provisions a
    dedicated agent when that widget exists and carries no agent yet. FAILURE-SOFT:
    any error logs and returns so the settings PATCH never 500s. A no-op when the
    site has no widget yet (the concierge is wired later) or the widget already has
    an agent (manual or previously provisioned).
    """
    try:
        if not site.pocket_id:
            return
        widgets = await _store().list_widgets(
            pocket_id=site.pocket_id, workspace_id=workspace_id, limit=1
        )
        widget = widgets[0] if widgets else None
        if widget is None or getattr(widget, "agent_id", ""):
            return
        await ensure_site_agent(site, widget)
    except Exception:  # noqa: BLE001 — provisioning must never 500 the settings PATCH
        logger.warning(
            "paw-bar concierge: auto-provision on concierge-enable failed for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


__all__ = [
    "concierge_name",
    "concierge_persona",
    "concierge_slug",
    "derive_conversation_starters",
    "ensure_site_agent",
    "provision_on_concierge_enable",
    "provision_widget_on_create",
]
