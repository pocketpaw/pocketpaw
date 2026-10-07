# ee/pocketpaw_ee/sites/site_secrets.py: the per-site secret store.
#
# A site built on the ``project`` engine can need runtime secrets (a Stripe key, a
# Resend key). The agent never sees or writes a value: it calls
# ``request_secret`` (the ``request_site_secret`` tool), which leaves a PENDING row
# the owner fills in from the builder. Publishing hands every SET secret to the
# Worker as a ``secret_text`` binding (``secrets_for_deploy`` ->
# ``bundle_deploy.ProvisionedResources.secrets``).
#
# Rows live in ``cloud.models.site_secret.SiteSecret``, keyed by (workspace,
# pocket_id, name). Values are encrypted with the deployment Fernet key in
# ``cloud._core.crypto``; this module invents no crypto.
#
# INVARIANTS A READER MUST NOT BREAK:
#   * NO VALUE LEAVES THIS MODULE EXCEPT THROUGH ``secrets_for_deploy``. Views carry
#     name, status, description, who requested and timestamps. Never log a value,
#     never put one in an exception message, an event or a view.
#   * Writes (set / delete) are POCKET-OWNER ONLY. Reads and requests need edit
#     access to the pocket (the same rule as ``pockets.service.has_edit_access``).
#     A pocket outside the caller's workspace is a 404, so ids cannot be probed.
#   * The site delete cascade calls ``purge_for_pocket``; it is best effort there.
"""Per-site secret storage: request, set, delete, list names, resolve for deploy."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, PayloadTooLarge, ValidationError

logger = logging.getLogger(__name__)

# Worker env var names, written UPPER_SNAKE: a letter first, at most 64 chars.
NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# Cloudflare allows 5 KiB per secret_text binding today; 8 KiB leaves room for
# PEM blobs without letting a paste of a whole file through.
MAX_VALUE_BYTES = 8 * 1024
MAX_DESCRIPTION_CHARS = 500
# Requests are agent-driven; a cap keeps a looping agent from filling the card list.
MAX_SECRETS_PER_SITE = 50

Requester = Literal["agent", "user"]
Status = Literal["set", "pending"]


class SiteSecretView(BaseModel):
    """What any API or tool may say about one secret. There is no value field."""

    name: str
    status: Status
    description: str = ""
    requested_by: Requester | None = None
    requested_at: datetime | None = None
    updated_at: datetime | None = None


class SiteSecretsView(BaseModel):
    """``GET /sites/by-pocket/{id}/secrets``."""

    pocket_id: str
    # True when the caller owns the pocket and may set / delete values.
    can_manage: bool
    secrets: list[SiteSecretView] = Field(default_factory=list)
    # The subset of ``secrets`` still waiting for a value (status "pending").
    pending: list[SiteSecretView] = Field(default_factory=list)


class SiteSecretPut(BaseModel):
    """``PUT /sites/by-pocket/{id}/secrets/{name}`` body."""

    value: str


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_PATTERN.match(name):
        raise ValidationError(
            "sites.secret_name_invalid",
            "A secret name must be UPPER_SNAKE_CASE (A-Z, 0-9, _), start with a letter "
            "and be at most 64 characters.",
        )
    return name


def _doc():
    from pocketpaw_ee.cloud.models.site_secret import SiteSecret

    return SiteSecret


def _view(row) -> SiteSecretView:
    return SiteSecretView(
        name=row.name,
        status="set" if row.encrypted_value else "pending",
        description=row.description or "",
        requested_by=row.requested_by if row.requested_by in ("agent", "user") else None,
        requested_at=row.requested_at,
        updated_at=row.value_set_at,
    )


async def _pocket_in_workspace(workspace_id: str, pocket_id: str):
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


async def _require_edit(workspace_id: str, user_id: str, pocket_id: str):
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    doc = await _pocket_in_workspace(workspace_id, pocket_id)
    if not await pockets_service.has_edit_access(pocket_id, user_id):
        _deny(user_id, workspace_id, pocket_id, "sites.secret.read", "pocket.access_denied")
        raise Forbidden("pocket.access_denied", "You do not have access to this site.")
    return doc


async def _require_owner(workspace_id: str, user_id: str, pocket_id: str):
    doc = await _pocket_in_workspace(workspace_id, pocket_id)
    if doc.owner != user_id:
        _deny(user_id, workspace_id, pocket_id, "sites.secret.write", "sites.secret_not_owner")
        raise Forbidden(
            "sites.secret_not_owner", "Only the site owner can set or delete its secrets."
        )
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
        logger.debug("site_secrets: denial audit failed", exc_info=True)


def _audit(user_id: str, workspace_id: str, pocket_id: str, action: str, name: str) -> None:
    try:
        from pocketpaw_ee.guards.audit import log_privileged_action

        log_privileged_action(
            actor=user_id,
            action=action,
            resource_id=pocket_id,
            workspace_id=workspace_id,
            secret_name=name,
        )
    except Exception:  # noqa: BLE001
        logger.debug("site_secrets: audit failed", exc_info=True)


async def _emit(kind: str, *, owner: str, user_id: str, data: dict) -> None:
    """Best effort: a missing bus (tests, scripts) must not undo the write."""
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import (
            SiteSecretRequested,
            SiteSecretUpdated,
        )

        cls = SiteSecretRequested if kind == "requested" else SiteSecretUpdated
        await emit(cls(data={**data, "owner": owner, "user_id": user_id}))
    except Exception:  # noqa: BLE001
        logger.debug("site_secrets: %s event not emitted", kind, exc_info=True)


async def _rows(workspace_id: str, pocket_id: str) -> list:
    doc = _doc()
    rows = await doc.find(doc.workspace == workspace_id, doc.pocket_id == pocket_id).to_list()
    return sorted(rows, key=lambda r: r.name)


async def _row(workspace_id: str, pocket_id: str, name: str):
    doc = _doc()
    return await doc.find_one(
        doc.workspace == workspace_id, doc.pocket_id == pocket_id, doc.name == name
    )


# ------------------------------------------------------------------ reads


async def list_secrets(*, workspace_id: str, user_id: str, pocket_id: str) -> SiteSecretsView:
    """Names, status and pending requests. Anyone who may edit the pocket."""
    pocket = await _require_edit(workspace_id, user_id, pocket_id)
    views = [_view(r) for r in await _rows(workspace_id, pocket_id)]
    return SiteSecretsView(
        pocket_id=pocket_id,
        can_manage=pocket.owner == user_id,
        secrets=views,
        pending=[v for v in views if v.status == "pending"],
    )


# ----------------------------------------------------------------- writes


async def set_secret(
    *, workspace_id: str, user_id: str, pocket_id: str, name: str, value: str
) -> SiteSecretView:
    """Encrypt and store a value (owner only). Fills a pending request if there is one."""
    name = validate_name(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("sites.secret_value_empty", f"{name} needs a non-empty value.")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise PayloadTooLarge(
            "sites.secret_too_large",
            f"{name} is over the {MAX_VALUE_BYTES // 1024} KB limit for a site secret.",
        )
    pocket = await _require_owner(workspace_id, user_id, pocket_id)
    token = crypto.encrypt(value)
    now = datetime.now(UTC)

    row = await _row(workspace_id, pocket_id, name)
    if row is None:
        await _check_cap(workspace_id, pocket_id)
        row = _doc()(workspace=workspace_id, pocket_id=pocket_id, name=name)
    row.encrypted_value = token
    row.value_set_by = user_id
    row.value_set_at = now
    await row.save()

    _audit(user_id, workspace_id, pocket_id, "sites.secret.set", name)
    logger.info("site_secrets: %s set on pocket %s", name, pocket_id)
    view = _view(row)
    await _emit(
        "updated",
        owner=pocket.owner,
        user_id=user_id,
        data={"workspace_id": workspace_id, "pocket_id": pocket_id, "name": name, "status": "set"},
    )
    return view


async def delete_secret(*, workspace_id: str, user_id: str, pocket_id: str, name: str) -> None:
    """Remove a secret or a pending request (owner only). 404 when there is none."""
    name = validate_name(name)
    pocket = await _require_owner(workspace_id, user_id, pocket_id)
    row = await _row(workspace_id, pocket_id, name)
    if row is None:
        raise NotFound("site_secret", name)
    await row.delete()
    _audit(user_id, workspace_id, pocket_id, "sites.secret.delete", name)
    logger.info("site_secrets: %s deleted from pocket %s", name, pocket_id)
    await _emit(
        "updated",
        owner=pocket.owner,
        user_id=user_id,
        data={
            "workspace_id": workspace_id,
            "pocket_id": pocket_id,
            "name": name,
            "status": "deleted",
        },
    )


async def request_secret(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    name: str,
    description: str,
    requested_by: Requester = "agent",
) -> SiteSecretView:
    """Leave a pending request the owner fills in the builder.

    Takes no value, by design. A secret that is already set keeps its value; only
    the description is refreshed when the new one is non-empty. Re-requesting a
    pending secret refreshes its description and timestamp (one row, not two)."""
    name = validate_name(name)
    description = (description or "").strip()[:MAX_DESCRIPTION_CHARS]
    pocket = await _require_edit(workspace_id, user_id, pocket_id)
    now = datetime.now(UTC)

    row = await _row(workspace_id, pocket_id, name)
    if row is None:
        await _check_cap(workspace_id, pocket_id)
        row = _doc()(workspace=workspace_id, pocket_id=pocket_id, name=name)
    if description:
        row.description = description
    if not row.encrypted_value:
        row.requested_by = requested_by
        row.requested_by_user = user_id
        row.requested_at = now
    await row.save()

    view = _view(row)
    if view.status == "pending":
        await _emit(
            "requested",
            owner=pocket.owner,
            user_id=user_id,
            data={
                "workspace_id": workspace_id,
                "pocket_id": pocket_id,
                "name": name,
                "status": "pending",
                "description": row.description,
                "requested_by": requested_by,
            },
        )
    return view


async def _check_cap(workspace_id: str, pocket_id: str) -> None:
    doc = _doc()
    count = await doc.find(doc.workspace == workspace_id, doc.pocket_id == pocket_id).count()
    if count >= MAX_SECRETS_PER_SITE:
        raise ValidationError(
            "sites.secret_cap",
            f"A site can hold at most {MAX_SECRETS_PER_SITE} secrets; delete one first.",
        )


# ------------------------------------------------------- deploy + cascade


async def secrets_for_deploy(site) -> dict[str, str]:
    """``{name: plaintext}`` for every SET secret of ``site``'s pocket.

    The only decrypting reader. Its result goes straight into
    ``ProvisionedResources.secrets`` and from there into ``secret_text`` bindings;
    do not log it or keep it. A site with no pocket (a test double, a legacy row)
    has no secrets."""
    workspace_id = str(getattr(site, "workspace", "") or "")
    pocket_id = str(getattr(site, "pocket_id", "") or "")
    if not workspace_id or not pocket_id:
        return {}
    return {
        row.name: crypto.decrypt(row.encrypted_value)
        for row in await _rows(workspace_id, pocket_id)
        if row.encrypted_value
    }


async def purge_for_pocket(*, workspace_id: str, pocket_id: str) -> int:
    """Delete every secret and request of one site. Returns how many rows went."""
    doc = _doc()
    result = await doc.find(doc.workspace == workspace_id, doc.pocket_id == pocket_id).delete()
    return int(getattr(result, "deleted_count", 0) or 0)
