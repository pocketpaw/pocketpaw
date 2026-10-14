# ee/pocketpaw_ee/integrations/bindings.py: which provider a site pocket uses.
#
# A ``SiteIntegration`` row links one site pocket to one provider and, for connect
# kinds, to one workspace ``IntegrationConnection``. A connection can serve many
# sites; unlinking a site never revokes the grant, and revoking a grant flips its
# sites to ``needs_connection``.
#
# Authz copies ``sites.site_secrets``: a pocket outside the caller's workspace is a
# 404 (ids cannot be probed), binding reads and writes need edit access to the pocket
# (``pockets.service.has_edit_access``). Visitor PII is owner (or admin) only: the
# newsletter CSV is owner only, and order customer emails are masked for editors
# (``webhooks.list_orders``). Linking a grant needs its creator or a workspace admin.
# The site delete cascade calls ``purge_for_pocket``. Workspace RBAC and the
# ``sites`` plan feature run in the router.
#
# Every link / unlink / connect / reconnect emits ``site.integration_changed``
# {pocket_id, provider, status}. Linking also mints the site's ``PAW_GATEWAY_TOKEN``
# secret (once) so a project Worker gets it as a secret_text binding on deploy.
#
# Settings are validated against the provider's ``settings_fields`` (settings.py).
# On link, on enable, on a settings change, and for every bound site after a
# reconnect, the provider's ``site_setup`` hook runs (idempotent; it may write
# readonly settings that pass their field's check; stored with an atomic $set).
# A failure is stored as ``setup_error`` and the site reads ``setup_required``;
# it never fails the request. The gateway also stores one (``mark_setup_error``)
# when a handler finds the provider-side setup gone (a deleted sheet); a later
# successful call clears it (``clear_setup_error``). ``claim_binding_lease``
# serialises provider writes that must not interleave (the vault's lease pattern).
#
# Status (``binding_status``), first match wins: disabled (has actions, none on),
# needs_connection / needs_reconnect (connect kinds), setup_required (a setup
# failure or a required setting without a valid value; so an embed is
# ``connected`` exactly when its link is valid), the provider's ``site_status``
# hook, connected.
#
# Row writes are atomic ``$set``s of the fields a call changed (``_persist``), and
# settings are written key by key, so a stale request can never wipe a value
# another writer (a provider's setup, its encrypted secret) stored meanwhile.
#
# Unlinking a site, or purging it, runs the provider's optional ``site_teardown``
# hook (best effort) for state the provider created elsewhere (a Turnstile widget).

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.integrations import settings as settings_rules
from pocketpaw_ee.integrations import vault
from pocketpaw_ee.integrations.dto import ProviderView, SiteIntegrationPut, SiteIntegrationView
from pocketpaw_ee.integrations.providers import all_providers, get_provider
from pocketpaw_ee.integrations.providers.base import IntegrationProvider, SetupContext

logger = logging.getLogger(__name__)

GATEWAY_TOKEN_NAME = "PAW_GATEWAY_TOKEN"
MAX_SETTINGS_BYTES = 8 * 1024
MAX_PURPOSE_CHARS = 300


def _doc():
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    return SiteIntegration


# ------------------------------------------------------------------ access


async def pocket_in_workspace(workspace_id: str, pocket_id: str):
    """The pocket doc, or NotFound when it is missing or in another workspace."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.pocket import Pocket

    try:
        doc = await Pocket.get(PydanticObjectId(pocket_id))
    except Exception:  # noqa: BLE001 - a malformed id is a missing pocket
        doc = None
    if doc is None or not workspace_id or doc.workspace != workspace_id:
        raise NotFound("pocket", pocket_id)
    return doc


async def require_edit(workspace_id: str, user_id: str, pocket_id: str):
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    doc = await pocket_in_workspace(workspace_id, pocket_id)
    if not await pockets_service.has_edit_access(pocket_id, user_id):
        _deny(user_id, workspace_id, pocket_id, "sites.integration.write", "pocket.access_denied")
        raise Forbidden("pocket.access_denied", "You do not have access to this site.")
    return doc


async def require_owner(workspace_id: str, user_id: str, pocket_id: str):
    doc = await pocket_in_workspace(workspace_id, pocket_id)
    if doc.owner != user_id:
        _deny(user_id, workspace_id, pocket_id, "sites.integration.export", "sites.not_owner")
        raise Forbidden("sites.not_owner", "Only the site owner can do that.")
    return doc


def _deny(user_id: str, workspace_id: str, pocket_id: str, action: str, code: str) -> None:
    try:
        from pocketpaw_ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action=action,
            code=code,
            resource_id=pocket_id,
            workspace_id=workspace_id,
        )
    except Exception:  # noqa: BLE001 - auditing never changes the answer
        logger.debug("integrations: denial audit failed", exc_info=True)


def provider_or_404(provider_id: str) -> IntegrationProvider:
    provider = get_provider(provider_id)
    if provider is None:
        raise NotFound("integration_provider", provider_id)
    return provider


# ------------------------------------------------------------------- views


def provider_view(provider: IntegrationProvider) -> ProviderView:
    return ProviderView(
        id=provider.id,
        name=provider.name,
        kind=provider.kind,
        category=provider.category,
        plan=provider.plan,
        availability=provider.availability(),
        actions=[
            {
                "name": name,
                "public": spec.public,
                "write": spec.write,
                "description": spec.description,
            }
            for name, spec in provider.actions.items()
        ],
        settings_fields=settings_rules.field_views(provider),
        requires_connection=provider.kind == "connect",
    )


def list_provider_views() -> list[ProviderView]:
    return [provider_view(p) for p in all_providers()]


_HOOK_STATUSES = {"connected", "needs_connection", "needs_reconnect", "setup_required"}


def binding_status(binding: Any, provider: IntegrationProvider, conn: Any | None) -> str:
    if provider.actions and not binding.enabled_actions:
        return "disabled"
    if provider.kind == "connect":
        if conn is None or conn.status == "revoked":
            return "needs_connection"
        if conn.status == "needs_reconnect":
            return "needs_reconnect"
    if getattr(binding, "setup_error", "") or settings_rules.missing_required(
        provider, dict(binding.settings or {})
    ):
        return "setup_required"
    hook = getattr(provider, "site_status", None)
    if hook is not None:
        try:
            status = hook(binding, conn)
        except Exception:  # noqa: BLE001 - a broken hook must not break the list
            logger.debug("integrations: site_status hook failed", exc_info=True)
            status = None
        if status in _HOOK_STATUSES:
            return status
    return "connected"


def _setup_due(binding: Any, provider: IntegrationProvider, conn: Any | None) -> bool:
    if provider.actions and not binding.enabled_actions:
        return False
    return provider.kind != "connect" or (conn is not None and conn.status == "active")


async def run_setup(binding: Any, provider: IntegrationProvider, conn: Any | None, user_id: str):
    """Run the provider's ``site_setup`` and store the outcome. Never raises.

    Only readonly (server-set) keys the provider declares are kept, and only values
    that pass the field's own check (a readonly link is still an http(s) URL).
    Persists ``settings`` + ``setup_error`` with one atomic ``$set``, never the
    whole loaded document, so a concurrent PUT's other fields survive."""
    hook = getattr(provider, "site_setup", None)
    if hook is None or not _setup_due(binding, provider, conn):
        return
    ctx = SetupContext(
        workspace_id=binding.workspace,
        pocket_id=binding.pocket_id,
        user_id=user_id,
        provider=provider.id,
        connection=conn,
        get_token=(
            (lambda rejected="": vault.get_valid_token(conn, rejected=rejected))
            if conn is not None
            else None
        ),
    )
    try:
        written = await hook(binding, ctx) or {}
        kept = settings_rules.server_values(provider, dict(written))
        settings = _check_settings({**dict(binding.settings or {}), **kept})
        error = ""
    except Exception as exc:  # noqa: BLE001 - a setup failure is a status, not a crash
        logger.info("integrations: %s site_setup failed: %s", provider.id, type(exc).__name__)
        settings, error = dict(binding.settings or {}), "setup_failed"
    update: dict[str, Any] = {}
    current = dict(binding.settings or {})
    for key, value in settings.items():
        if current.get(key) != value:
            update[f"settings.{key}"] = value
    if error != (getattr(binding, "setup_error", "") or ""):
        update["setup_error"] = error
    if not update:
        return
    await _doc().get_pymongo_collection().update_one({"_id": binding.id}, {"$set": update})
    binding.settings = settings
    binding.setup_error = error


async def _persist(row: Any, settings_before: dict | None, *fields: str) -> None:
    """Insert a new row, or ``$set`` only ``fields`` on an existing one. ``settings``
    is written key by key against ``settings_before`` (removed keys are unset)."""
    from datetime import UTC, datetime

    if row.id is None:
        await row.insert()
        return
    now = datetime.now(UTC)
    set_: dict[str, Any] = {"updatedAt": now}
    unset: dict[str, str] = {}
    for name in fields:
        if name != "settings":
            set_[name] = getattr(row, name)
            continue
        before, after = settings_before or {}, dict(row.settings or {})
        for key, value in after.items():
            if before.get(key) != value:
                set_[f"settings.{key}"] = value
        unset.update({f"settings.{key}": "" for key in before if key not in after})
    update: dict[str, Any] = {"$set": set_}
    if unset:
        update["$unset"] = unset
    await _doc().get_pymongo_collection().update_one({"_id": row.id}, update)
    row.updatedAt = now


async def connection_for(binding: Any):
    if not binding.connection_id:
        return None
    try:
        return await vault.get_connection(binding.workspace, binding.connection_id)
    except NotFound:
        return None


async def to_view(binding: Any) -> SiteIntegrationView:
    provider = provider_or_404(binding.provider)
    conn = await connection_for(binding)
    return SiteIntegrationView(
        provider=binding.provider,
        connection_id=binding.connection_id if conn is not None else None,
        status=binding_status(binding, provider, conn),
        settings=settings_rules.visible(provider, dict(binding.settings or {})),
        enabled_actions=list(binding.enabled_actions or []),
        updated_at=vault._utc(binding.updatedAt),
    )


async def _row(workspace_id: str, pocket_id: str, provider_id: str):
    doc = _doc()
    return await doc.find_one(
        doc.workspace == workspace_id, doc.pocket_id == pocket_id, doc.provider == provider_id
    )


async def site_count(connection_id: str) -> int:
    doc = _doc()
    return await doc.find(doc.connection_id == connection_id).count()


# ------------------------------------------------------------------- reads


async def list_for_pocket(
    *, workspace_id: str, user_id: str, pocket_id: str
) -> list[SiteIntegrationView]:
    await require_edit(workspace_id, user_id, pocket_id)
    doc = _doc()
    rows = await doc.find(doc.workspace == workspace_id, doc.pocket_id == pocket_id).to_list()
    known = {p.id for p in all_providers()}
    return [await to_view(r) for r in sorted(rows, key=lambda r: r.provider) if r.provider in known]


# ------------------------------------------------------------------ writes


def _check_settings(settings: dict) -> dict:
    try:
        size = len(json.dumps(settings).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "integrations.settings_invalid", "Settings must be plain JSON."
        ) from exc
    if size > MAX_SETTINGS_BYTES:
        raise ValidationError("integrations.settings_too_large", "Settings are over 8 KB.")
    return settings


def _check_actions(provider: IntegrationProvider, names: list[str]) -> list[str]:
    unknown = [n for n in names if n not in provider.actions]
    if unknown:
        raise ValidationError(
            "integrations.unknown_action",
            f"{provider.name} has no action named {unknown[0]!r}.",
        )
    return list(dict.fromkeys(names))


async def _link_connection(
    workspace_id: str,
    provider: IntegrationProvider,
    connection_id: str,
    *,
    user_id: str,
    is_admin: bool,
):
    conn = await vault.get_connection(workspace_id, connection_id)
    if conn.created_by != user_id and not is_admin:
        _deny(user_id, workspace_id, connection_id, "sites.integration.link", "not_grant_owner")
        raise Forbidden(
            "integrations.not_connection_owner",
            "Only the person who connected this account or a workspace admin can link it.",
        )
    if conn.provider != provider.id:
        raise ValidationError(
            "integrations.connection_mismatch", "That connection is for a different provider."
        )
    vault.ensure_not_revoked(conn)
    return conn


async def upsert(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    provider_id: str,
    body: SiteIntegrationPut,
    is_admin: bool = False,
) -> SiteIntegrationView:
    """``PUT /sites/by-pocket/{pocket_id}/integrations/{provider}``."""
    provider = provider_or_404(provider_id)
    pocket = await require_edit(workspace_id, user_id, pocket_id)
    row = await _row(workspace_id, pocket_id, provider.id)
    if row is None:
        row = _doc()(
            workspace=workspace_id,
            pocket_id=pocket_id,
            provider=provider.id,
            enabled_actions=list(provider.actions),
            created_by=user_id,
        )
    # Setup re-runs on link, on enable (disabled -> any action on) and on a settings
    # change; a pure enabled_actions toggle does not touch the provider.
    setup = row.id is None
    before = dict(row.settings or {})
    was_enabled = bool(row.enabled_actions) or not provider.actions
    if "connection_id" in body.model_fields_set:
        if body.connection_id:
            await _link_connection(
                workspace_id, provider, body.connection_id, user_id=user_id, is_admin=is_admin
            )
        setup = setup or (body.connection_id or None) != row.connection_id
        row.connection_id = body.connection_id or None
    if body.settings is not None:
        merged = _check_settings(
            settings_rules.merge_put(provider, body.settings, dict(row.settings or {}))
        )
        setup = setup or merged != dict(row.settings or {})
        row.settings = merged
    if body.enabled_actions is not None:
        row.enabled_actions = _check_actions(provider, body.enabled_actions)
        setup = setup or (not was_enabled and bool(row.enabled_actions))
    await _persist(row, before, "connection_id", "settings", "enabled_actions")
    await ensure_gateway_token(workspace_id, pocket_id)
    if setup:
        await run_setup(row, provider, await connection_for(row), user_id)
    view = await to_view(row)
    await emit_changed(pocket, provider.id, view.status, user_id)
    return view


async def link_connection(*, workspace_id: str, user_id: str, pocket_id: str, conn: Any):
    """The OAuth callback's link step: bind ``conn`` to the site (create if needed)."""
    provider = provider_or_404(conn.provider)
    pocket = await require_edit(workspace_id, user_id, pocket_id)
    row = await _row(workspace_id, pocket_id, provider.id)
    if row is None:
        row = _doc()(
            workspace=workspace_id,
            pocket_id=pocket_id,
            provider=provider.id,
            enabled_actions=list(provider.actions),
            created_by=user_id,
        )
    row.connection_id = str(conn.id)
    await _persist(row, None, "connection_id")
    await ensure_gateway_token(workspace_id, pocket_id)
    await run_setup(row, provider, conn, user_id)
    view = await to_view(row)
    await emit_changed(pocket, provider.id, view.status, user_id)
    return view


async def request_from_agent(
    *, workspace_id: str, user_id: str, pocket_id: str, provider_id: str, purpose: str
) -> SiteIntegrationView:
    """``connect_site_integration``: make sure a binding exists (needs_connection for
    a connect provider with no grant yet) and record why the agent wants it."""
    provider = provider_or_404(provider_id)
    pocket = await require_edit(workspace_id, user_id, pocket_id)
    row = await _row(workspace_id, pocket_id, provider.id)
    created = row is None
    if row is None:
        row = _doc()(
            workspace=workspace_id,
            pocket_id=pocket_id,
            provider=provider.id,
            enabled_actions=list(provider.actions),
            created_by=user_id,
        )
    row.purpose = (purpose or "").strip()[:MAX_PURPOSE_CHARS]
    await _persist(row, None, "purpose")
    await ensure_gateway_token(workspace_id, pocket_id)
    if created:
        await run_setup(row, provider, await connection_for(row), user_id)
    view = await to_view(row)
    if created:
        await emit_changed(pocket, provider.id, view.status, user_id)
    return view


async def unlink(*, workspace_id: str, user_id: str, pocket_id: str, provider_id: str) -> None:
    """``DELETE``: drop the binding. The workspace grant survives."""
    pocket = await require_edit(workspace_id, user_id, pocket_id)
    row = await _row(workspace_id, pocket_id, provider_id)
    if row is None:
        raise NotFound("site_integration", provider_id)
    await row.delete()
    await run_teardown(row)
    await emit_changed(pocket, provider_id, "unlinked", user_id)


async def run_teardown(row: Any) -> None:
    """The provider's optional ``site_teardown(binding)``. Never raises."""
    hook = getattr(get_provider(row.provider), "site_teardown", None)
    if hook is None:
        return
    try:
        await hook(row)
    except Exception:  # noqa: BLE001 - an unlink never fails on provider cleanup
        logger.info("integrations: %s site_teardown failed", row.provider, exc_info=True)


async def ensure_gateway_token(workspace_id: str, pocket_id: str) -> None:
    """Mint ``PAW_GATEWAY_TOKEN`` for the site once. Needs CLOUD_ENCRYPTION_KEY,
    which the vault needs anyway; a failure is logged, not raised (browser callers
    never use it)."""
    try:
        from pocketpaw_ee.sites import site_secrets

        await site_secrets.ensure_generated_secret(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            name=GATEWAY_TOKEN_NAME,
            description="Created by Paw for Sites integrations. Server-side calls only.",
        )
    except Exception:  # noqa: BLE001
        logger.warning("integrations: could not mint the gateway token", exc_info=True)


# -------------------------------------------------- connection fan-out


async def bindings_for_connection(connection_id: str) -> list[Any]:
    doc = _doc()
    return await doc.find(doc.connection_id == connection_id).to_list()


async def setup_bound_sites(conn: Any, user_id: str) -> None:
    """After a connect / reconnect / resume: re-run setup for every bound site."""
    provider = get_provider(conn.provider)
    if provider is None:
        return
    for row in await bindings_for_connection(str(conn.id)):
        await run_setup(row, provider, conn, user_id)


async def notify_connection_status(conn: Any, status: str) -> None:
    """Tell every site bound to ``conn`` that its status changed."""
    for row in await bindings_for_connection(str(conn.id)):
        try:
            pocket = await pocket_in_workspace(row.workspace, row.pocket_id)
        except NotFound:
            continue
        await emit_changed(pocket, row.provider, status, "")


async def revoke_connection(*, workspace_id: str, user_id: str, connection_id: str, is_admin: bool):
    """``DELETE /integrations/connections/{id}``: revoke; bound sites need a new one."""
    conn = await vault.get_connection(workspace_id, connection_id)
    if conn.created_by != user_id and not is_admin:
        raise Forbidden(
            "integrations.not_connection_owner",
            "Only the person who connected this account or a workspace admin can remove it.",
        )
    rows = await bindings_for_connection(str(conn.id))
    await vault.mark_revoked(conn)
    for row in rows:
        row.connection_id = None
        await _persist(row, None, "connection_id")
        try:
            pocket = await pocket_in_workspace(row.workspace, row.pocket_id)
        except NotFound:
            continue
        await emit_changed(pocket, row.provider, "needs_connection", user_id)


async def emit_changed(pocket: Any, provider_id: str, status: str, user_id: str) -> None:
    """Best effort ``site.integration_changed``; a missing bus must not undo a write."""
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import SiteIntegrationChanged

        await emit(
            SiteIntegrationChanged(
                data={
                    "workspace_id": pocket.workspace,
                    "pocket_id": str(pocket.id),
                    "provider": provider_id,
                    "status": status,
                    "owner": pocket.owner,
                    "user_id": user_id,
                }
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("integrations: change event not emitted", exc_info=True)


# ------------------------------------------------------------------ cascade


async def purge_for_pocket(*, workspace_id: str, pocket_id: str) -> int:
    """Delete one site's bindings, newsletter list, orders and demo records.

    Called by the site delete cascade next to ``site_secrets.purge_for_pocket``.
    Workspace grants survive: they may serve other sites."""
    from pocketpaw_ee.cloud.models.integration import (
        IntegrationDemoRecord,
        NewsletterSubscriber,
        SiteIntegration,
        SiteOrder,
    )

    for row in await SiteIntegration.find(
        SiteIntegration.workspace == workspace_id, SiteIntegration.pocket_id == pocket_id
    ).to_list():
        await run_teardown(row)
    removed = 0
    for doc in (SiteIntegration, NewsletterSubscriber, SiteOrder, IntegrationDemoRecord):
        result = await doc.find(doc.workspace == workspace_id, doc.pocket_id == pocket_id).delete()
        removed += int(getattr(result, "deleted_count", 0) or 0)
    return removed


# ------------------------------------------------------------ gateway reads


async def binding_for_site(site: Any, provider_id: str):
    """The binding of a resolved Site for ``provider_id``, or None."""
    if not site.pocket_id:
        return None
    return await _row(site.workspace, site.pocket_id, provider_id)


async def mark_setup_error(binding: Any, error: str) -> None:
    """A handler found the provider-side setup broken (a deleted sheet): the site
    reads ``setup_required`` until a settings change or reconnect re-runs setup."""
    error = (error or "setup_failed")[:80]
    if (getattr(binding, "setup_error", "") or "") == error:
        return
    await (
        _doc()
        .get_pymongo_collection()
        .update_one({"_id": binding.id}, {"$set": {"setup_error": error}})
    )
    binding.setup_error = error


async def clear_setup_error(workspace_id: str, pocket_id: str, provider_id: str) -> None:
    """A call just worked: drop a stale ``setup_error`` (one conditional $set)."""
    await (
        _doc()
        .get_pymongo_collection()
        .update_one(
            {
                "workspace": workspace_id,
                "pocket_id": pocket_id,
                "provider": provider_id,
                "setup_error": {"$nin": ["", None]},
            },
            {"$set": {"setup_error": ""}},
        )
    )


LEASE_SECONDS = 15


async def claim_binding_lease(
    workspace_id: str, pocket_id: str, provider_id: str, *, wait_seconds: float = 5.0
) -> tuple[str, dict] | None:
    """Take the binding's short lease: ``(owner, raw row)``, or None after waiting.

    One atomic ``find_one_and_update`` per try (free or expired lease -> ours), the
    vault's refresh-lease pattern. Release with ``release_binding_lease``."""
    coll = _doc().get_pymongo_collection()
    owner = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        now = datetime.now(UTC)
        row = await coll.find_one_and_update(
            {
                "workspace": workspace_id,
                "pocket_id": pocket_id,
                "provider": provider_id,
                "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}],
            },
            {"$set": {"lease_owner": owner, "lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
            return_document=True,
        )
        if row is not None:
            return owner, row
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def release_binding_lease(
    workspace_id: str, pocket_id: str, provider_id: str, owner: str, update: dict | None = None
) -> None:
    """Drop the lease, writing ``update`` only while we still hold it."""
    await (
        _doc()
        .get_pymongo_collection()
        .update_one(
            {
                "workspace": workspace_id,
                "pocket_id": pocket_id,
                "provider": provider_id,
                "lease_owner": owner,
            },
            {"$set": {**(update or {}), "lease_owner": None, "lease_until": None}},
        )
    )
