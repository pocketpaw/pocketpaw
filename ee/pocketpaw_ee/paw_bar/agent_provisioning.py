# ee/pocketpaw_ee/paw_bar/agent_provisioning.py — auto-provision a DEDICATED
# concierge agent per Paw Site.
#
# Updated 2026-07-30 (Paw Bar inbox D5): added ``widget_for_agent(agent_id,
# workspace_id)`` — the REVERSE of the bind ``ensure_site_agent`` writes. D5 lets a
# concierge run read its own ``agent:<id>`` knowledge scope, which makes "is this
# agent answering public site visitors?" a question the agent Knowledge surface has
# to answer out loud (``agents.service.is_visible_to_site_visitors`` is its one
# caller). It reads the real binding — ``widget.agent_id`` — rather than sniffing
# the deterministic ``concierge-<site_id>`` slug, so a MANUALLY bound agent (which
# the provisioner never renames) is recognised too.
#
# Updated 2026-07-30 (publish-time provisioning): added ``ensure_site_widget`` —
# the missing THIRD trigger. A site created AND published by the agent in one
# conversation never passes through widget-create (a dashboard flow) or a
# concierge-enable TRANSITION (the model defaults to enabled), so neither
# existing trigger fired, the publish-time embed's four-gate check failed
# silently, and a brand-new site shipped bar-less with no dedicated agent.
# ``ensure_site_widget`` runs at publish (from ``sites.service._embed_concierge_bar``):
# resolve-or-mint the site's paw-bar widget, then funnel into ``ensure_site_agent``.
# Idempotent + failure-soft like its siblings. Found live in the 2026-07-30 smoke.
#
# Updated 2026-07-30 (feat/paw-bar-autoembed): extracted ``site_widget(pocket_id,
# workspace_id)`` — the "which paw-bar widget belongs to this site's pocket"
# lookup that ``provision_on_concierge_enable`` used to do inline. The publish
# path now needs the SAME answer (to decide whether a site has earned an embedded
# concierge bar and which widget id to put in the snippet), and two copies of a
# tenancy-scoped lookup is exactly the kind of pair that drifts apart. Behaviour is
# unchanged: the same workspace-scoped ``list_widgets(limit=1)``, the same
# empty-pocket_id guard that keeps a blank pocket from widening the query onto a
# sibling's widget.
#
# Created 2026-07-23 (feat/site-dedicated-agent): every site's concierge is
# answered by an agent that exists FOR that site — never a shared/universal
# agent. ``ensure_site_agent(site, widget)`` is idempotent: a widget already
# bound to a LIVE agent is returned unchanged (manual binds are never
# overwritten); otherwise ONE dedicated agent is created (via the agents service
# — never a direct Beanie write) and bound to the widget through the paw-bar
# store's ``update_fields`` path. THREE triggers funnel here: widget-create (no
# agent_id + the pocket resolves to a Site), concierge-enable (the site's widget
# is still unbound), and ``ensure_site_widget`` — the publish-time path that
# covers a site whose widget never existed at all. All are FAILURE-SOFT — a
# provisioning failure logs
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

# How many of a workspace's paw-bar widgets ``widget_for_agent`` will scan for a
# bind. One bar per site, so this is far above any real tenant.
_AGENT_BIND_SCAN_LIMIT = 500


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

    # (4) Give the new agent something to know. A concierge reads ONE KB scope —
    # its site's pocket — and until the site's own pages are in that scope the agent
    # is provisioned knowledge-empty and answers "I don't know" about the business
    # it fronts. Sites published before this existed have never synced, so a bind is
    # the natural catch-up point. Background + failure-soft: never a gate on a bind.
    _schedule_knowledge_sync(site)
    return agent.id


def _schedule_knowledge_sync(site: Any) -> None:
    """Fire the background site→pocket-KB sync, swallowing everything. Imported
    lazily so provisioning keeps working even if the sites KB module cannot be
    loaded in a given deployment."""
    try:
        from pocketpaw_ee.sites.kb_ingest import schedule_site_knowledge_sync

        schedule_site_knowledge_sync(site)
    except Exception:  # noqa: BLE001 — knowledge sync is never a gate on a bind
        logger.warning(
            "paw-bar concierge: could not schedule knowledge sync for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


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


async def site_widget(pocket_id: str, workspace_id: str) -> Any | None:
    """The paw-bar widget for a site's pocket, or ``None``.

    The single resolution both the provisioner and the publish-time bar embed use.
    Workspace-scoped, and an EMPTY ``pocket_id`` returns ``None`` rather than
    querying: an unfiltered ``list_widgets`` would widen to the workspace and hand
    back a SIBLING site's widget (the same guard the router's
    ``_resolve_site_and_widget`` carries). A site has at most one bar, so the limit
    is 1.
    """
    if not pocket_id:
        return None
    widgets = await _store().list_widgets(pocket_id=pocket_id, workspace_id=workspace_id, limit=1)
    return widgets[0] if widgets else None


async def widget_for_agent(agent_id: str, workspace_id: str) -> Any | None:
    """The paw-bar widget bound to ``agent_id`` in ``workspace_id``, or ``None``.

    The reverse of the bind ``ensure_site_agent`` writes, and the only honest way
    to answer "does this agent front a public site bar?" — it reads the binding
    itself (``widget.agent_id``), not the deterministic ``concierge-<site_id>``
    slug, so an agent a human bound by hand in the dashboard counts exactly the
    same as a provisioned one.

    Workspace-scoped, and an empty ``agent_id`` / ``workspace_id`` returns
    ``None`` rather than querying — an unscoped scan would hand back a sibling
    tenant's widget (the same guard ``site_widget`` carries).

    The store has no ``agent_id`` predicate, so this lists the workspace's bars
    and scans. Bounded by ``_AGENT_BIND_SCAN_LIMIT``: a workspace holds one bar
    per site, so the cap is far above any real tenant, and being over it degrades
    to "not visible" rather than to a slow query.
    """
    if not agent_id or not workspace_id:
        return None
    widgets = await _store().list_widgets(workspace_id=workspace_id, limit=_AGENT_BIND_SCAN_LIMIT)
    target = str(agent_id)
    for widget in widgets:
        if str(getattr(widget, "agent_id", "") or "") == target:
            return widget
    return None


async def ensure_site_widget(site: Any, workspace_id: str) -> Any | None:
    """Publish-time trigger: a concierge-enabled site must HAVE a paw-bar widget.

    Resolve-or-mint the widget for ``site``'s pocket, then ensure its dedicated
    agent. This is the third trigger beside widget-create and concierge-enable:
    a site created and published by the agent in one conversation hits neither
    (there is no dashboard widget-create, and ``concierge_enabled`` defaults to
    True so no enable transition ever fires) — without this, the publish-time
    embed found no widget and silently shipped the site bar-less.

    Idempotent: an existing widget is returned as-is (after a best-effort agent
    bind if it is unbound). FAILURE-SOFT: any error logs and returns ``None`` —
    a site going live matters more than its bar.
    """
    try:
        existing = await site_widget(site.pocket_id, workspace_id)
        if existing is not None:
            if not getattr(existing, "agent_id", ""):
                await ensure_site_agent(site, existing)
                refreshed = await _store().get_widget(existing.id, workspace_id=workspace_id)
                return refreshed or existing
            return existing

        from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget

        site_name = str(getattr(site, "name", "") or "").strip() or "This site"
        widget = PawBarWidget(
            pocket_id=site.pocket_id,
            owner=str(getattr(site, "owner", "") or "site"),
            workspace_id=workspace_id,
            name=f"{site_name} concierge",
            # The glass bar renders its own chat surface; blocks stay empty and
            # the spec is the same "pending" shell the dashboard flow starts from.
            spec=PawBarSpec(widget_id="pending", pocket_id=site.pocket_id, blocks=[]),
        )
        created = await _store().create_widget(widget)
        logger.info(
            "paw-bar concierge: minted widget %s for site %s at publish",
            created.id,
            getattr(site, "id", "?"),
        )
        await ensure_site_agent(site, created)
        refreshed = await _store().get_widget(created.id, workspace_id=workspace_id)
        return refreshed or created
    except Exception:  # noqa: BLE001 — provisioning must never break a publish
        logger.warning(
            "paw-bar concierge: publish-time widget provisioning failed for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )
        return None


async def provision_on_concierge_enable(site: Any, workspace_id: str) -> None:
    """Concierge-enable trigger: provision the site's widget when it is unbound.

    Resolves the site's paw-bar widget (workspace-scoped) and provisions a
    dedicated agent when that widget exists and carries no agent yet. FAILURE-SOFT:
    any error logs and returns so the settings PATCH never 500s. A no-op when the
    site has no widget yet (the concierge is wired later) or the widget already has
    an agent (manual or previously provisioned).
    """
    try:
        widget = await site_widget(site.pocket_id, workspace_id)
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
    "ensure_site_widget",
    "provision_on_concierge_enable",
    "provision_widget_on_create",
    "site_widget",
    "widget_for_agent",
]
