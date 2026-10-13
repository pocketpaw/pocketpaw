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

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.integrations import vault
from pocketpaw_ee.integrations.dto import ProviderView, SiteIntegrationPut, SiteIntegrationView
from pocketpaw_ee.integrations.providers import all_providers, get_provider
from pocketpaw_ee.integrations.providers.base import IntegrationProvider

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
    )


def list_provider_views() -> list[ProviderView]:
    return [provider_view(p) for p in all_providers()]


def binding_status(binding: Any, provider: IntegrationProvider, conn: Any | None) -> str:
    if not binding.enabled_actions:
        return "disabled"
    if provider.kind != "connect":
        return "connected"
    if conn is None or conn.status == "revoked":
        return "needs_connection"
    if conn.status == "needs_reconnect":
        return "needs_reconnect"
    return "connected"


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
        settings=dict(binding.settings or {}),
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
    if "connection_id" in body.model_fields_set:
        if body.connection_id:
            await _link_connection(
                workspace_id, provider, body.connection_id, user_id=user_id, is_admin=is_admin
            )
        row.connection_id = body.connection_id or None
    if body.settings is not None:
        row.settings = _check_settings(body.settings)
    if body.enabled_actions is not None:
        row.enabled_actions = _check_actions(provider, body.enabled_actions)
    await row.save()
    await ensure_gateway_token(workspace_id, pocket_id)
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
    await row.save()
    await ensure_gateway_token(workspace_id, pocket_id)
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
    await row.save()
    await ensure_gateway_token(workspace_id, pocket_id)
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
    await emit_changed(pocket, provider_id, "unlinked", user_id)


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
        await row.save()
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
