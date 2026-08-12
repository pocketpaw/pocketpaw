# ee/pocketpaw_ee/cloud/storage/service.py — the storage USAGE resolver + cap gate
# (feat/billing-storage-caps).
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule, mirroring
# ``credits.service`` / ``entitlements.service``). Public API:
#   * ``workspace_storage_usage(workspace_id)`` — sum the workspace's live
#     (non-deleted) ``FileUpload`` blob sizes in BYTES. This is the S3 usage
#     backing the Files → Knowledge Base store.
#   * ``storage_cap_exceeded(workspace_id, incoming_bytes)`` — would a NEW blob
#     of ``incoming_bytes`` push the workspace over its plan's
#     ``max_storage_bytes``? Returns ``(exceeded, used, limit)``; the UPLOAD seam
#     (``uploads.service.upload_many`` / ``write_text_file``) raises
#     ``StorageLimitError`` (402) when exceeded. GATED on ``billing_enforced``:
#     OSS / self-host tenants (billing off) always get ``(False, 0, None)``.
#   * ``assert_storage_available(workspace_id, incoming_bytes)`` — convenience
#     wrapper that raises ``StorageLimitError`` when the cap would be exceeded
#     (used by the programmatic ``write_text_file`` writer).
#   * ``resolve_storage_usage(workspace_id)`` — the read the Settings storage
#     page renders: used vs the plan cap + remaining + percent. The READ is NOT
#     gated on ``billing_enforced`` (it is informational — a Go workspace shows
#     "15 GB" whether or not enforcement is on); only an uncapped plan
#     (Enterprise, ``max_storage_bytes=None``) reports a None cap.
#
# READ-ONLY: no writes, no emit (EE cloud rule 9 only fires on mutation; this
# entity mutates nothing). Tenancy: every query filters on the explicit
# ``workspace_id`` (the ``FileUpload.workspace`` column), so there is no
# cross-tenant read here — a caller only ever sees the storage of the workspace
# it asked for. Imports of config / entitlements / the FileUpload model are LAZY
# (inside the functions) so this module stays off the heavy import graph at load
# and the uploads.service that calls into it never pays a Beanie import just to
# import the module.
#
# The $sum aggregation runs server-side in the DB (NOT a pull-all-then-sum in
# Python). The raw ``get_pymongo_collection().aggregate()`` returns DIFFERENT
# shapes per driver — a COROUTINE under real Motor (async for raised TypeError on
# this very endpoint in prod), a directly-iterable latent cursor under the
# mongomock-motor test harness — so the code discriminates with
# ``inspect.isawaitable`` (the repo's cross-driver idiom in ``files/router.py``,
# ``connectors/registry.py``). Do NOT switch to Beanie's ``Document.aggregate()``
# classmethod: its internal ``await`` breaks under the mongomock-motor harness.
# Soft-deleted rows (``deleted_at`` set) are excluded — deleting a file frees its
# bytes from the used total.
#
# Created 2026-08-08 (feat/billing-storage-caps): new entity.
# Updated 2026-08-08 (prod fix): real Motor ``aggregate()`` returns a coroutine;
#   the original ``async for`` form passed the mongomock harness but 500'd live.
#   Now awaits when the cursor is awaitable, iterates directly otherwise.

from __future__ import annotations

import inspect

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.storage.domain import StorageUsage


async def workspace_storage_usage(workspace_id: str) -> int:
    """Total live S3 bytes for ``workspace_id`` (sum of live FileUpload sizes).

    Aggregates ``FileUpload.size`` over the workspace's NON-DELETED rows in the
    DB (a ``$match`` + ``$group`` ``$sum``), so deleting a file frees its bytes
    and a workspace with no files reads 0. Returns an int byte count.
    """
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    pipeline = [
        {"$match": {"workspace": workspace_id, "deleted_at": None}},
        {"$group": {"_id": None, "total": {"$sum": "$size"}}},
    ]
    # The raw ``get_pymongo_collection().aggregate()`` returns DIFFERENT shapes
    # per driver: a COROUTINE under real Motor (must ``await`` to get the
    # AsyncIOMotorCommandCursor) but a directly-iterable latent cursor under the
    # mongomock-motor test harness (where ``await`` raises TypeError). So we
    # discriminate with ``inspect.isawaitable`` — the established cross-driver
    # idiom in this repo (``files/router.py``, ``connectors/registry.py``) —
    # instead of Beanie's ``Document.aggregate()`` classmethod, whose internal
    # ``await`` breaks under the harness. ``async for`` then iterates in BOTH.
    cursor = FileUpload.get_pymongo_collection().aggregate(pipeline)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    async for row in cursor:
        return int(row.get("total") or 0)
    return 0


async def storage_cap_exceeded(
    workspace_id: str,
    incoming_bytes: int,
) -> tuple[bool, int, int | None]:
    """Would ``incoming_bytes`` of new blobs exceed this workspace's plan cap?

    Returns ``(exceeded, used_bytes, limit_bytes)``. GATED on ``billing_enforced``:
    OSS / self-host tenants (billing off) always get ``(False, 0, None)`` — no
    cap, no extra DB read — so a self-hosted deployment behaves exactly as before.
    When enforced, resolves the workspace's plan ``max_storage_bytes`` and sums
    its live ``FileUpload`` bytes. An uncapped plan (Enterprise,
    ``max_storage_bytes=None``) never trips. ``exceeded`` is ``used + incoming >
    limit`` — checked BEFORE the write, so it blocks the upload that WOULD push
    the workspace over, never an existing blob (upload-time only, never
    retroactive). Imports are lazy to keep this module off the config /
    entitlements import graph at load.
    """
    from pocketpaw.config import get_settings

    if not get_settings().billing_enforced:
        return (False, 0, None)

    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    ent = await entitlements_service.resolve_entitlements(workspace_id)
    limit = ent.max_storage_bytes
    if limit is None:
        return (False, 0, None)
    used = await workspace_storage_usage(workspace_id)
    return (used + incoming_bytes > limit, used, limit)


async def assert_storage_available(workspace_id: str, incoming_bytes: int) -> None:
    """Raise ``StorageLimitError`` when ``incoming_bytes`` would exceed the cap.

    Thin convenience over ``storage_cap_exceeded`` for write seams that don't
    need the used/limit tuple (e.g. ``uploads.service.write_text_file``). A
    no-op unless ``billing_enforced`` (OSS / self-host tenants are unaffected).
    """
    from pocketpaw_ee.cloud._core.errors import StorageLimitError

    exceeded, _used, limit = await storage_cap_exceeded(workspace_id, incoming_bytes)
    if exceeded:
        raise StorageLimitError(limit)


async def resolve_storage_usage(workspace_id: str) -> StorageUsage:
    """Resolve a workspace's storage usage vs its plan cap (read surface).

    ``used_bytes`` is always computed. ``max_bytes`` is the workspace's RESOLVED
    plan ``max_storage_bytes`` — read purely from the plan catalog via
    entitlements, NOT gated on ``billing_enforced``. The read is informational:
    a Go workspace shows "15 GB" whether or not upload enforcement is active
    (enforcement is the separate ``storage_cap_exceeded`` gate, which DOES respect
    ``billing_enforced``). Only an uncapped plan (Enterprise, ``max_storage_bytes
    = None``) reports None here — in which case ``remaining`` and ``percent`` are
    also None and the UI renders "Unlimited". ``percent_used`` is rounded to one
    decimal.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("storage.invalid_workspace", "workspace_id is required")

    used = await workspace_storage_usage(workspace_id)

    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    ent = await entitlements_service.resolve_entitlements(workspace_id)
    limit = ent.max_storage_bytes
    if limit is None:
        return StorageUsage(workspace_id, used, None, None, None)

    remaining = max(limit - used, 0)
    percent = round(used / limit * 100, 1) if limit > 0 else 0.0
    return StorageUsage(workspace_id, used, limit, remaining, percent)
