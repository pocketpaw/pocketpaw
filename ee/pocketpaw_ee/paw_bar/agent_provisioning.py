# ee/pocketpaw_ee/paw_bar/agent_provisioning.py — every Paw Site's concierge is
# answered by an agent that exists FOR that site, never a shared/universal one.
#
# Nothing here runs automatically: a concierge is never created as a side effect.
# The owner's explicit create (``POST /paw-bar/admin/site/{id}/concierge``) calls
# ``ensure_site_widget_row`` (resolve-or-mint the bar, empty spec, no actions) and,
# on the legacy runtime, ``ensure_site_agent`` (bind the site's dedicated agent).
# ``rebind_site_agent`` points a bar at a different, named agent (an empty id is a
# 422). ``sync_concierge_identity`` keeps the dedicated agent's name and persona in
# step with the owner's ``Site.concierge_name``.
#
# INVARIANTS:
#   * IDEMPOTENT. A widget already bound to a LIVE agent is returned unchanged,
#     so a manual bind is never overwritten and a retry never mints a second
#     agent. Only ``rebind_site_agent`` replaces a live bind, and only because a
#     caller asked for that by name.
#   * THE SLUG IS DETERMINISTIC on the site id (``concierge-<site_id>``), so a
#     create that races or retries after a failed bind RESOLVES the same agent
#     instead of duplicating it. The slug and the ``concierge`` + ``site:<id>``
#     tags are also what keeps the agent off tenant listings
#     (``agents.service.is_concierge_agent``).
#   * AGENTS ARE CREATED THROUGH THE AGENTS SERVICE, never by a direct Beanie
#     write, and in the SITE's workspace owned by the site's owner — the bind is
#     workspace-scoped at every step so it can never reach across tenants.
#   * A RENAME TOUCHES ONLY GENERATED VALUES. ``sync_concierge_identity`` changes
#     the dedicated agent's name or persona only while it still reads as the one
#     this module generated, so an owner's own edit and a hand-bound agent are
#     never overwritten.
#   * ``widget_for_agent`` is the REVERSE lookup and reads the real binding
#     (``widget.agent_id``), not the slug, so a hand-bound agent counts too.

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


def concierge_name(site_name: str, guided_name: str = "") -> str:
    """The agent display name: the owner's guided name (``Site.concierge_name``)
    when set, else ``<Site name> Concierge`` (``Site Concierge`` when the site has
    no name). Truncated to the Agent.name cap."""
    chosen = " ".join((guided_name or "").split())
    if chosen:
        return chosen[:_MAX_AGENT_NAME]
    stripped = (site_name or "").strip()
    name = f"{stripped} Concierge" if stripped else "Site Concierge"
    return name[:_MAX_AGENT_NAME]


def concierge_persona(site_name: str, guided_name: str = "") -> str:
    """The soul persona seeded on the concierge agent. It names the concierge
    when the owner gave it a name, so it introduces itself by that name."""
    subject = (site_name or "").strip() or "this site"
    chosen = " ".join((guided_name or "").split())[:_MAX_AGENT_NAME]
    if chosen:
        return (
            f"You are {chosen}, the concierge for {subject}. Introduce yourself as "
            f"{chosen}. You answer visitors' questions about this site, grounded in "
            "its own knowledge."
        )
    return (
        f"You are the concierge for {subject}. You answer visitors' questions "
        "about this site, grounded in its own knowledge."
    )


def _guided_name(site: Any) -> str:
    return str(getattr(site, "concierge_name", "") or "")


def derive_conversation_starters(widget: Any, *, catalog_count: int | None = None) -> list[str]:
    """Derive up to four plain visitor questions from the widget.

    Rules (order preserved, capped at ``_MAX_STARTERS``):
      * a non-empty catalog adds ``"What do you sell?"``. ``catalog_count`` is the
        catalog store's count; without it a (deprecated) spec catalog decides;
      * each GATED action carrying a label adds ``"<label>?"`` (e.g. a
        ``book_table`` action labelled "Book a table" → "Book a table?");
      * if nothing derived, a single generic ``"What can you help me with?"``.
    """
    spec = getattr(widget, "spec", None)
    starters: list[str] = []
    has_catalog = (
        catalog_count > 0 if catalog_count is not None else bool(getattr(spec, "catalog", None))
    )
    if has_catalog:
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


def _seed_identity(body: Any, site: Any, widget: Any, catalog_count: int | None = None) -> None:
    """Seed the ASG-1 identity fields on a create body IF the model supports them.

    ``welcome_message`` ← ``Site.concierge_greeting`` (when non-empty),
    ``conversation_starters`` ← ``derive_conversation_starters(widget)``. The
    Agent create DTO carries both fields, so both are seeded; the ``hasattr``
    guards only matter for a body that lacks one, which degrades to a debug log.
    """
    greeting = (getattr(site, "concierge_greeting", "") or "").strip()
    starters = derive_conversation_starters(widget, catalog_count=catalog_count)

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
            name=concierge_name(site.name, _guided_name(site)),
            slug=slug,
            visibility="workspace",
            persona=concierge_persona(site.name, _guided_name(site)),
            soul_archetype=_CONCIERGE_ARCHETYPE,
            # soul_enabled defaults True — the concierge carries a soul.
        )
        _seed_tags(body, site_id)
        catalog_count: int | None = None
        if getattr(widget, "id", ""):
            try:
                catalog_count = await _store().catalog_count(widget.id)
            except Exception:  # noqa: BLE001 — a starter is cosmetic; never block the bind
                logger.warning("paw-bar concierge: catalog count failed for %s", widget.id)
        _seed_identity(body, site, widget, catalog_count)
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


async def sync_concierge_identity(site: Any, previous_name: str) -> None:
    """Carry a change of ``Site.concierge_name`` onto the site's dedicated agent.

    The legacy runtime answers through that agent, and its name and persona are
    what the dashboard and the agent's soul show. Only the DEDICATED agent (slug
    ``concierge-<site_id>``, in the site's workspace) is touched, never an agent
    the owner bound by hand, and each of the two fields only while it still equals
    what this module generated from ``previous_name``: an owner who renamed the
    agent or rewrote its persona keeps their edit.

    Failure-soft: the settings save has already happened, so a missing agent or a
    failed write is logged, never raised.
    """
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import UpdateAgentRequest

    workspace_id = str(getattr(site, "workspace", "") or "")
    site_id = str(getattr(site, "id", "") or "")
    if not workspace_id or not site_id:
        return
    try:
        agent = await agents_service.get_by_slug(workspace_id, concierge_slug(site_id))
    except NotFound:
        return
    site_name = str(getattr(site, "name", "") or "")
    new_name = _guided_name(site)
    body = UpdateAgentRequest()
    if agent.name == concierge_name(site_name, previous_name):
        body.name = concierge_name(site_name, new_name)
    if agent.config.soul_persona == concierge_persona(site_name, previous_name):
        body.persona = concierge_persona(site_name, new_name)
    if body.name in (None, agent.name) and body.persona in (None, agent.config.soul_persona):
        return
    try:
        # The agent is the site owner's; the caller already passed the site's
        # manage gate, so the write is made as the agent's owner.
        ctx = agents_service.legacy_ctx(str(agent.owner), workspace_id)
        await agents_service.update(ctx, agent.id, body)
    except Exception:  # noqa: BLE001 — the settings are saved; the agent can lag
        logger.warning(
            "paw-bar concierge: could not rename the concierge agent for site %s",
            site_id,
            exc_info=True,
        )


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


async def ensure_site_widget_row(site: Any, workspace_id: str) -> Any:
    """Resolve-or-mint the paw-bar widget for ``site``'s pocket. Binds NO agent.

    Called only by the owner's explicit create. An existing widget is returned
    untouched, actions and all: they belong to its owner. A minted one carries an
    empty spec — no blocks, no catalog and NO actions. The publish-time mint used
    to add a gated ``booking_request`` so a service site could take bookings; a
    concierge's actions are now something its owner declares, not a default the
    platform picks for them.

    Not failure-soft, unlike the triggers it replaced: the caller is an owner
    who asked for exactly this, so a store error should reach them as an error.
    """
    existing = await site_widget(site.pocket_id, workspace_id)
    if existing is not None:
        return existing

    from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget

    site_name = str(getattr(site, "name", "") or "").strip() or "This site"
    widget = PawBarWidget(
        pocket_id=site.pocket_id,
        owner=str(getattr(site, "owner", "") or "site"),
        workspace_id=workspace_id,
        name=f"{site_name} concierge",
        # The glass bar renders its own chat surface; blocks stay empty.
        spec=PawBarSpec(widget_id="pending", pocket_id=site.pocket_id, blocks=[]),
    )
    created = await _store().create_widget(widget)
    logger.info(
        "paw-bar concierge: minted widget %s for site %s on explicit create",
        created.id,
        getattr(site, "id", "?"),
    )
    return created


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

    ``agent_id`` is REQUIRED. An empty one used to mean RE-PROVISION (clear the
    bind and mint the canonical agent again); CR-12 removed that arm, because a
    rebind that can mint an agent is a way to create a concierge nobody asked
    for. It raises ``ValidationError`` before anything is read or written.

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
    from pocketpaw_ee.cloud._core.errors import Forbidden, ValidationError
    from pocketpaw_ee.cloud.agents import service as agents_service

    if not agent_id:
        raise ValidationError(
            "sites.agent_required",
            "Name the agent this bar should use. A rebind no longer creates one.",
        )

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
    # bar and repoint it at this site's concierge agent. The published page it
    # belongs to would then be answered by a concierge grounded in somebody else's
    # pocket, with nothing in the response saying so. The agent is tenancy-gated
    # below; the bar is gated only here.
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


__all__ = [
    "concierge_name",
    "concierge_persona",
    "concierge_slug",
    "derive_conversation_starters",
    "ensure_site_agent",
    "ensure_site_widget_row",
    "rebind_site_agent",
    "site_widget",
    "sync_concierge_identity",
    "widget_for_agent",
]
