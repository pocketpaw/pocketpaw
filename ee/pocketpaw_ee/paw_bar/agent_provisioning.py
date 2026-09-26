# ee/pocketpaw_ee/paw_bar/agent_provisioning.py — every Paw Site's concierge is
# answered by an agent that exists FOR that site, never a shared/universal one.
#
# Updated 2026-09-26 (fix/pawbar-public-starters-sync-status): comments only. The
#   ASG-1 identity fields exist on the Agent model and create DTO, so
#   ``_seed_identity`` does seed welcome_message + conversation_starters (and
#   ``_seed_tags`` the tags); the notes below that called them no-ops were stale.
#
# ``ensure_site_agent(site, widget)`` is the funnel, and the single place "which
# agent is this pocket's canonical concierge?" is decided. FOUR triggers end
# here, and adding a fifth means calling this, not re-deciding it:
#   * widget-create — a dashboard widget whose pocket resolves to a Site;
#   * concierge-enable — the site's widget is still unbound;
#   * ``ensure_site_widget`` — publish time, for a site whose widget never
#     existed (an agent-built site passes through neither of the above);
#   * ``provision_foreign_concierge`` — a BOUGHT foreign concierge, which never
#     publishes and has no Worker, so no other trigger can ever fire for it.
#
# INVARIANTS:
#   * IDEMPOTENT. A widget already bound to a LIVE agent is returned unchanged,
#     so a manual bind is never overwritten and a retry never mints a second
#     agent. Only ``rebind_site_agent`` replaces a live bind, and only because a
#     caller asked for that by name.
#   * THE SLUG IS DETERMINISTIC on the site id (``concierge-<site_id>``), so a
#     create that races or retries after a failed bind RESOLVES the same agent
#     instead of duplicating it. A lost create race adopts the winner.
#   * AGENTS ARE CREATED THROUGH THE AGENTS SERVICE, never by a direct Beanie
#     write, and in the SITE's workspace owned by the site's owner — the bind is
#     workspace-scoped at every step so it can never reach across tenants.
#   * FAILURE-SOFT AT THE EDGES. Each trigger swallows its errors and leaves the
#     widget unbound rather than 500-ing a widget-create, a settings PATCH or a
#     publish. Chat still 409s and the dashboard still offers a manual create.
#   * ``widget_for_agent`` is the REVERSE lookup and reads the real binding
#     (``widget.agent_id``), not the slug, so a hand-bound agent counts too.
#
# ASG-1 identity fields (welcome_message, conversation_starters) are seeded on
# create by ``_seed_identity``; both it and ``_seed_tags`` guard on ``hasattr`` of
# the create body, so a DTO without a field degrades to a logged no-op.

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
    ``conversation_starters`` ← ``derive_conversation_starters(widget)``. The
    Agent create DTO carries both fields, so both are seeded; the ``hasattr``
    guards only matter for a body that lacks one, which degrades to a debug log.
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
    free-form ``tags`` field. The create DTO and the Agent model carry one (ASG-1;
    not to be confused with ``scopes``, a hierarchical SCOPE-tag list with its own
    validator), so this stamps them; a body without the field is a logged no-op."""
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


def _default_booking_action() -> Any:
    """The one action a MINTED site widget declares: a gated booking request.

    A spec with no gated-with-args actions renders no form-card instructions in
    the concierge preamble, leaving the agent unable to take a booking at all —
    a service site's one job. ``gated`` never executes; it raises an Instinct
    proposal for the owner (SS-2: ``auto`` is reserved for visitor-scoped
    verbs). Applied ONLY when minting: an existing widget's actions belong to
    its owner.
    """
    from pocketpaw.paw_bar.models import PawBarActionSpec

    return PawBarActionSpec(
        verb="booking_request",
        policy="gated",
        args={
            "name": "str",
            "phone": "str",
            "address": "str",
            "issue": "str",
            "preferred_window": "str",
        },
        label="Book a service visit",
    )


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
            # The glass bar renders its own chat surface; blocks stay empty. The
            # spec ships with the default gated booking action — an empty
            # ``actions`` yields a concierge that cannot take a booking.
            spec=PawBarSpec(
                widget_id="pending",
                pocket_id=site.pocket_id,
                blocks=[],
                actions=[_default_booking_action()],
            ),
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


async def provision_foreign_concierge(site: Any, workspace_id: str) -> str | None:
    """Foreign-bind trigger (the fourth): give a BOUGHT foreign concierge its agent.

    A foreign site is bought, never published: there is no Worker, no deploy and
    no dashboard widget-create, so none of the other three triggers can ever fire
    for it. Without this the buyer paid for a concierge with no agent behind it,
    and the key resolver would hand every visitor a bar that cannot answer.

    Funnels into ``ensure_site_widget`` — resolve-or-mint the pocket's bar, then
    ``ensure_site_agent`` — rather than deciding the agent here, so a foreign
    concierge and a published one for the same pocket converge on the SAME
    canonical agent instead of racing to mint two.

    Returns the bound agent id, or ``None`` when provisioning could not complete.
    FAILURE-SOFT like its siblings: a bind that has already been PAID for must
    not be rolled back because the agent could not be minted, and the caller can
    re-run this (it is idempotent) once whatever failed is fixed.
    """
    widget = await ensure_site_widget(site, workspace_id)
    if widget is None:
        return None
    return str(getattr(widget, "agent_id", "") or "") or None


async def rebind_site_agent(
    site: Any,
    workspace_id: str,
    *,
    agent_id: str = "",
    widget_id: str = "",
) -> str | None:
    """Point a site's bar at a DIFFERENT agent, or re-run the funnel for it.

    The one path allowed to REPLACE a live bind. ``ensure_site_agent`` refuses to
    (a manual bind is somebody's deliberate choice), so an owner who wants a
    different concierge — or who wants a bar re-provisioned after deleting its
    agent — has no way through the funnel. This is that way, and it is explicit
    rather than a flag on the funnel so nothing reaches it by accident.

    ``agent_id`` names the replacement. It is resolved through
    ``get_for_viewer(agent_id, workspace_id, None)``, so an agent in another
    tenant raises ``NotFound`` and the bind is refused — a rebind is the obvious
    place to try to attach a victim tenant's agent to a bar you control, and an
    unchecked id here would serve that agent's answers to your visitors.

    ``agent_id`` empty means RE-PROVISION: clear the stale bind and let
    ``ensure_site_agent`` resolve-or-mint the canonical one again.

    ``widget_id`` picks the bar explicitly; omitted, the site's pocket resolves
    it (``site_widget``). Either way the bar must belong to the SITE'S pocket —
    the store scopes a widget lookup to the workspace and no further, so an
    explicit id could otherwise name a colleague's bar and the re-provision arm
    would repoint their published page at this site's concierge.

    Nothing about the Site row is touched either way — the
    ``signed_key`` the customer has already embedded, the tier they paid for and
    the subscription all survive a rebind, which is the whole point: swapping the
    answering agent must never cost the buyer their credential or their month.

    Returns the newly bound agent id, or ``None`` when there is no widget to bind
    or the rebind could not complete.
    """
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.agents import service as agents_service

    if widget_id:
        widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    else:
        widget = await site_widget(site.pocket_id, workspace_id)
    if widget is None:
        logger.warning(
            "paw-bar concierge: rebind found no widget for site %s",
            getattr(site, "id", "?"),
        )
        return None

    # THE BAR MUST BE THIS SITE'S BAR. ``get_widget`` is scoped to the workspace
    # and no further, so a caller-supplied ``widget_id`` could name a COLLEAGUE'S
    # bar — and the re-provision arm below would clear that bar's agent and bind
    # it to this site's ``concierge-<site_id>``. The published page it belongs to
    # would then be answered by a concierge grounded in somebody else's pocket,
    # with nothing in the response saying so. The ``agent_id`` arm is tenancy-
    # gated; this arm was not gated by anything.
    if str(getattr(widget, "pocket_id", "") or "") != str(getattr(site, "pocket_id", "") or ""):
        logger.warning(
            "paw-bar concierge: refused a rebind of widget %s (pocket %s) to site %s (pocket %s)",
            widget_id,
            getattr(widget, "pocket_id", "?"),
            getattr(site, "id", "?"),
            getattr(site, "pocket_id", "?"),
        )
        # Forbidden rather than the module's usual NotFound-for-a-stranger. Both
        # rows are inside ONE workspace and the caller can list the bars in it
        # anyway, so hiding the widget buys no secrecy — while "that bar is not
        # this site's" is a mistake a UI can correct and a 404 is not.
        raise Forbidden(
            "sites.widget_pocket_mismatch",
            "That bar belongs to a different pocket and cannot be bound to this concierge.",
        )

    if agent_id:
        # Tenancy gate. Raises NotFound for a cross-workspace or unreadable
        # agent, which is deliberately indistinguishable from a missing one.
        await agents_service.get_for_viewer(agent_id, workspace_id, None)
        updated = await _store().update_fields(
            widget.id, {"agent_id": agent_id}, workspace_id=workspace_id
        )
        if updated is None:
            logger.warning(
                "paw-bar concierge: rebind of widget %s to agent %s wrote no row",
                widget.id,
                agent_id,
            )
            return None
        return agent_id

    # Re-provision: drop the stale bind so ``ensure_site_agent`` stops honouring
    # it, then let the funnel resolve-or-mint the canonical agent. Passing the
    # CLEARED widget rather than re-reading keeps the funnel's "already bound?"
    # check reading the value we just wrote.
    cleared = await _store().update_fields(widget.id, {"agent_id": ""}, workspace_id=workspace_id)
    return await ensure_site_agent(site, cleared or widget)


__all__ = [
    "concierge_name",
    "concierge_persona",
    "concierge_slug",
    "derive_conversation_starters",
    "ensure_site_agent",
    "ensure_site_widget",
    "provision_foreign_concierge",
    "provision_on_concierge_enable",
    "provision_widget_on_create",
    "rebind_site_agent",
    "site_widget",
    "widget_for_agent",
]
