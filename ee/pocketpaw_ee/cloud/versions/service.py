# ee/pocketpaw_ee/cloud/versions/service.py — the draft/published state machine
# over the pocket-content version log (pocketpaw#1345 Phase 1, plan §5/§6).
#
# Operations (plan §6):
#   * record_draft  — edit/refine: write a NEW draft version (the working one).
#   * publish_draft — promote the current draft to published (tag it). The deploy
#                     + "Live" flip is the CALLER's job (sites/service.publish);
#                     this only moves the version state, keeping deploy a separate
#                     axis (architect review #2).
#   * rollback      — clone an old version's content into a NEW draft.
#   * get_draft / get_published / get_draft_content — read the working/live snap.
#   * status_for    — derive {status, draft_version, published_version} from the
#                     log. A pocket is "draft" when draft != published (there are
#                     unpublished edits), "published" when draft == published, and
#                     "none" when it has no versions yet.
#
# Source of truth: the ``pocket_versions`` collection. The Pocket doc's
# draft_version_no / published_version_no are a denormalized cache this module
# keeps in sync (best-effort — a missing Pocket, e.g. a site keyed on a source
# pocket id, is skipped, never fatal). status_for always reads the collection so
# correctness never depends on the cache.
#
# Engine-aware: every snapshot carries content (rippleSpec) AND source (svelte
# map) AND engine together, so a svelte site — which lives entirely in ``source``
# — versions and rolls back correctly (architect review #5).
#
# OSS/EE seam: the persistence sits behind ``VersionStoreProtocol`` so an OSS
# local store could implement the same async surface later; the EE default is
# Beanie/Mongo. Phase 1 ships only the Beanie store.
#
# Created 2026-06-06 (feat/1345-draft-published).
# Updated 2026-06-06 (code review BLOCK-1): record_draft is now race-safe. The
# (workspace, pocket_id, version_no) index is UNIQUE, so two concurrent
# record_draft calls that compute the same version_no no longer both succeed —
# the loser gets a DuplicateKeyError. record_draft catches it and retries (re-read
# latest_version_no → re-increment → re-insert) up to a small bound, so two quick
# "Refine" clicks both land as distinct versions instead of one surfacing a 500.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud.models.pocket_version import PocketVersion

# How many times record_draft re-derives version_no after losing the unique-index
# race before giving up. Contention is between a user's own near-simultaneous
# edits (a double "Refine" click), so a handful of attempts is plenty.
_RECORD_DRAFT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class VersionStatus:
    """The derived draft/published state of a pocket's content.

    ``status`` is one of:
      * "none"      — the pocket has no versions yet.
      * "draft"     — the latest version is newer than the published one (there
                      are unpublished edits), or nothing is published yet.
      * "published" — the latest version IS the published one (no pending edits).
    """

    status: str
    draft_version: int | None
    published_version: int | None


class VersionStoreProtocol(Protocol):
    """Async persistence surface for pocket-content versions. The EE default is
    Beanie/Mongo; an OSS local store could implement the same shape later."""

    async def latest_version_no(self, workspace_id: str, pocket_id: str) -> int | None: ...
    async def latest(self, workspace_id: str, pocket_id: str) -> PocketVersion | None: ...
    async def latest_published(self, workspace_id: str, pocket_id: str) -> PocketVersion | None: ...
    async def get(
        self, workspace_id: str, pocket_id: str, version_no: int
    ) -> PocketVersion | None: ...
    async def insert(self, version: PocketVersion) -> PocketVersion: ...
    async def list_versions(self, workspace_id: str, pocket_id: str) -> list[PocketVersion]: ...


class _BeanieVersionStore:
    """The EE Mongo-backed store (the only Phase-1 implementation)."""

    async def latest_version_no(self, workspace_id: str, pocket_id: str) -> int | None:
        doc = (
            await PocketVersion.find(
                PocketVersion.workspace == workspace_id,
                PocketVersion.pocket_id == pocket_id,
            )
            .sort(-PocketVersion.version_no)  # type: ignore[operator]
            .first_or_none()
        )
        return doc.version_no if doc else None

    async def latest(self, workspace_id: str, pocket_id: str) -> PocketVersion | None:
        return (
            await PocketVersion.find(
                PocketVersion.workspace == workspace_id,
                PocketVersion.pocket_id == pocket_id,
            )
            .sort(-PocketVersion.version_no)  # type: ignore[operator]
            .first_or_none()
        )

    async def latest_published(self, workspace_id: str, pocket_id: str) -> PocketVersion | None:
        return (
            await PocketVersion.find(
                PocketVersion.workspace == workspace_id,
                PocketVersion.pocket_id == pocket_id,
                PocketVersion.status == "published",
            )
            .sort(-PocketVersion.version_no)  # type: ignore[operator]
            .first_or_none()
        )

    async def get(self, workspace_id: str, pocket_id: str, version_no: int) -> PocketVersion | None:
        return await PocketVersion.find_one(
            PocketVersion.workspace == workspace_id,
            PocketVersion.pocket_id == pocket_id,
            PocketVersion.version_no == version_no,
        )

    async def insert(self, version: PocketVersion) -> PocketVersion:
        await version.insert()
        return version

    async def list_versions(self, workspace_id: str, pocket_id: str) -> list[PocketVersion]:
        return (
            await PocketVersion.find(
                PocketVersion.workspace == workspace_id,
                PocketVersion.pocket_id == pocket_id,
            )
            .sort(-PocketVersion.version_no)  # type: ignore[operator]
            .to_list()
        )


# Module-level default store. Tests can pass their own via the ``_store`` kwarg,
# but the Beanie store works against the in-memory mongomock db, so they don't
# need to.
_default_store: VersionStoreProtocol = _BeanieVersionStore()


async def _sync_pocket_pointers(
    workspace_id: str,
    pocket_id: str,
    *,
    draft_no: int | None,
    published_no: int | None,
    deploy_status: str | None = None,
) -> None:
    """Best-effort denormalized-pointer update on the Pocket doc. A site is keyed
    on its SOURCE pocket id, so the Pocket usually exists; if it doesn't (or the
    id isn't an ObjectId), we skip silently — the collection stays the source of
    truth."""
    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.pocket import Pocket

    try:
        oid = ObjectId(pocket_id)
    except (InvalidId, TypeError):
        return
    doc = await Pocket.find_one(Pocket.id == oid, Pocket.workspace == workspace_id)
    if doc is None:
        return
    doc.draft_version_no = draft_no
    doc.published_version_no = published_no
    if deploy_status is not None:
        doc.last_deploy_status = deploy_status
    await doc.save()


async def record_draft(
    *,
    workspace_id: str,
    pocket_id: str,
    content: dict[str, Any] | None = None,
    source: dict[str, str] | None = None,
    engine: str = "ripple",
    author: str | None = None,
    label: str | None = None,
    origin: str | None = None,
    _store: VersionStoreProtocol | None = None,
) -> PocketVersion:
    """Write a NEW draft version — the edit/refine operation. version_no is the
    previous max + 1; parent_version_no links the chain. The published pointer is
    untouched. Both content (rippleSpec) and source (svelte map) are snapshotted
    so the engine round-trips on rollback.

    Race-safe: version_no is read-then-written, so two concurrent calls can derive
    the same number. The (workspace, pocket_id, version_no) unique index makes the
    loser's insert fail with DuplicateKeyError; we re-read the max and retry up to
    ``_RECORD_DRAFT_MAX_ATTEMPTS`` times so both edits land as distinct versions."""
    store = _store or _default_store
    version: PocketVersion | None = None
    for attempt in range(_RECORD_DRAFT_MAX_ATTEMPTS):
        prev_no = await store.latest_version_no(workspace_id, pocket_id)
        version_no = (prev_no or 0) + 1
        candidate = PocketVersion(
            workspace=workspace_id,
            pocket_id=pocket_id,
            version_no=version_no,
            content=content,
            source=source,
            engine=engine,
            author=author,
            label=label,
            status="draft",
            parent_version_no=prev_no,
            origin=origin or "refine",
        )
        try:
            await store.insert(candidate)
        except DuplicateKeyError:
            # Another writer claimed this version_no first. Re-read and retry.
            if attempt == _RECORD_DRAFT_MAX_ATTEMPTS - 1:
                raise
            continue
        version = candidate
        break
    assert version is not None  # the loop either sets this or re-raises
    published = await store.latest_published(workspace_id, pocket_id)
    await _sync_pocket_pointers(
        workspace_id,
        pocket_id,
        draft_no=version_no,
        published_no=published.version_no if published else None,
    )
    return version


async def publish_draft(
    *,
    workspace_id: str,
    pocket_id: str,
    version_no: int | None = None,
    deploy_status: str | None = None,
    _store: VersionStoreProtocol | None = None,
) -> PocketVersion:
    """Promote a version to published (tag it). Defaults to the current draft (the
    latest version); pass ``version_no`` to publish a specific one. Returns the
    promoted version. The caller triggers the deploy and flips "Live" — this only
    moves the version state (deploy is a separate axis, architect review #2).

    ``deploy_status`` is the optional deploy-axis value to mirror onto the Pocket
    pointer cache in the SAME write that advances the version pointers (e.g.
    sites/service.publish passes "live" once its deploy succeeds). Folding it in
    here keeps callers to ONE public call instead of reaching for the private
    pointer sync."""
    store = _store or _default_store
    if version_no is None:
        target = await store.latest(workspace_id, pocket_id)
    else:
        target = await store.get(workspace_id, pocket_id, version_no)
    if target is None:
        raise ValueError(f"no version to publish for pocket {pocket_id}")
    target.status = "published"
    await target.save()
    await _sync_pocket_pointers(
        workspace_id,
        pocket_id,
        draft_no=target.version_no,
        published_no=target.version_no,
        deploy_status=deploy_status,
    )
    return target


async def rollback(
    *,
    workspace_id: str,
    pocket_id: str,
    target_version: int,
    author: str | None = None,
    _store: VersionStoreProtocol | None = None,
) -> PocketVersion:
    """Make an old version the new draft: clone its content/source/engine into a
    fresh draft version (leaving the original row intact). Publishing the result
    goes live. Raises ValueError if the target version doesn't exist."""
    store = _store or _default_store
    src = await store.get(workspace_id, pocket_id, target_version)
    if src is None:
        raise ValueError(f"version {target_version} not found for pocket {pocket_id}")
    return await record_draft(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        content=src.content,
        source=src.source,
        engine=src.engine,
        author=author,
        label=f"rollback to v{target_version}",
        origin="rollback",
        _store=store,
    )


async def get_draft(
    *, workspace_id: str, pocket_id: str, _store: VersionStoreProtocol | None = None
) -> PocketVersion | None:
    """The current working version (the latest version, regardless of status)."""
    store = _store or _default_store
    return await store.latest(workspace_id, pocket_id)


async def get_published(
    *, workspace_id: str, pocket_id: str, _store: VersionStoreProtocol | None = None
) -> PocketVersion | None:
    """The current published (live-candidate) version, or None if never published."""
    store = _store or _default_store
    return await store.latest_published(workspace_id, pocket_id)


async def get_draft_content(
    *, workspace_id: str, pocket_id: str, _store: VersionStoreProtocol | None = None
) -> dict[str, Any] | None:
    """The working version's content for the preview. Returns the rippleSpec
    (``content``) for a ripple pocket, or the svelte ``source`` map for a svelte
    pocket — whichever the latest version carries. None if no versions exist."""
    draft = await get_draft(workspace_id=workspace_id, pocket_id=pocket_id, _store=_store)
    if draft is None:
        return None
    if draft.engine == "svelte":
        return draft.source
    return draft.content


async def status_for(
    *, workspace_id: str, pocket_id: str, _store: VersionStoreProtocol | None = None
) -> VersionStatus:
    """Derive the draft/published state from the version log (source of truth)."""
    store = _store or _default_store
    latest = await store.latest(workspace_id, pocket_id)
    if latest is None:
        return VersionStatus(status="none", draft_version=None, published_version=None)
    published = await store.latest_published(workspace_id, pocket_id)
    published_no = published.version_no if published else None
    # "published" only when the latest version IS the published one (no pending
    # edits). Otherwise there are unpublished edits → "draft".
    if published_no is not None and published_no == latest.version_no:
        status = "published"
    else:
        status = "draft"
    return VersionStatus(
        status=status,
        draft_version=latest.version_no,
        published_version=published_no,
    )


async def list_versions(
    *, workspace_id: str, pocket_id: str, _store: VersionStoreProtocol | None = None
) -> list[PocketVersion]:
    """All versions for a pocket, newest first (Phase-2 history view)."""
    store = _store or _default_store
    return await store.list_versions(workspace_id, pocket_id)


__all__ = [
    "VersionStatus",
    "VersionStoreProtocol",
    "record_draft",
    "publish_draft",
    "rollback",
    "get_draft",
    "get_published",
    "get_draft_content",
    "status_for",
    "list_versions",
]
