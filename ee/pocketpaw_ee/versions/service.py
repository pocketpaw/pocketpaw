# ee/pocketpaw_ee/versions/service.py
# Created: 2026-06-18 (feat/branch-primitive-versions, BP-1) — the version
# SERVICE spine for the universal Branch primitive. Artifact-GENERIC: it
# never imports pockets/sites and keys everything off (scope_type, scope_id),
# so the same spine serves any versionable artifact and can be lifted into an
# OSS protocol later.
#
# Operations (BP-1):
#   * write_draft(...)   — create the next draft version on a branch.
#   * branch(...)        — open a candidate lineage off the current head.
#   * publish(...)       — flip a version to status="published" (the pointer).
#   * get_draft(...)     — derived pointer: latest draft on a branch.
#   * get_published(...) — derived pointer: latest published on a branch.
#   * list_versions(...) — the version log for (scope, branch).
#   * revert(...)        — STUB. NotImplementedError — TODO(BP-4) owns revert.
#
# Pointer storage: DERIVED from the versions collection (no ArtifactRef doc).
# See models.py header for the why — single source of truth, indexed
# "latest of status X" query, cleaner OSS extraction.
#
# Journal: write_draft and branch each emit a Journal event via the org
# journal (``pocketpaw.journal_dep.get_journal``) so tests can override the
# cache. These are NOT Decision-Graph chain events, so we append DIRECTLY
# (the chain helper ``record_decision_event`` is only for the 4 chain
# actions). Scope is always stamped non-empty:
# ``[f"{scope_type}:{scope_id}", f"workspace:{workspace_id}"]``.
#   * artifact.version.created  — on write_draft
#   * artifact.version.branched — on branch
# We do NOT build a read projection here — that is BP-4.
#
# Updated: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — the merge
# gate state transitions land here so the Instinct executor stays thin:
#   * mark_merged(version_id)  — flip an ACCEPTED candidate to status="merged"
#                                (the merge gate's approve path calls publish()
#                                on the target first, then marks the candidate
#                                merged). Emits artifact.version.merged.
#   * discard(version_id)      — flip a REJECTED candidate to status="reverted"
#                                so it leaves the draft pointer; the published
#                                pointer is untouched. Emits artifact.version.
#                                discarded.
# Both are minimal + idempotent-friendly (a missing row raises ValueError, same
# defensive contract as publish()).
#
# Updated: 2026-06-18 (feat/branch-primitive-revert-history, BP-4) — revert +
# the publish event so the history view sees the full lifecycle:
#   * revert(version_id)  — revert an artifact to a prior snapshot by writing a
#                           NEW draft whose content == the target version's
#                           content (revert moves FORWARD; history is never
#                           mutated). A follow-up publish() then takes the
#                           reverted content live. Emits artifact.version.reverted
#                           (carrying ``reverted_from`` = the target version id).
#   * publish() now also emits artifact.version.published so the BP-4
#     VersionProjection (versions/projection.py) can fold the published-pointer
#     move into the per-artifact history timeline alongside created / branched /
#     merged / discarded / reverted. The projection is the event-history read; the
#     ordered version timeline an endpoint shows is served by list_versions (the
#     ArtifactVersion rows ARE the ordered log — see sites/router.py
#     /history).
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pymongo

from pocketpaw_ee.versions.models import ArtifactVersion

logger = logging.getLogger(__name__)

__all__ = [
    "branch",
    "discard",
    "get_draft",
    "get_published",
    "list_versions",
    "mark_merged",
    "publish",
    "revert",
    "write_draft",
]


# ---------------------------------------------------------------------------
# Internal helpers — version_no assignment + head lookup (all key off
# (scope_type, scope_id, branch); they ride the compound index in models.py).
# ---------------------------------------------------------------------------


async def _latest_in_branch(
    *, scope_type: str, scope_id: str, branch_name: str
) -> ArtifactVersion | None:
    """The highest-``version_no`` row in (scope, branch), or None.

    This is the branch head regardless of status — used to assign the next
    monotonic ``version_no`` and to find the parent for a new row.
    """
    return (
        await ArtifactVersion.find(
            ArtifactVersion.scope_type == scope_type,
            ArtifactVersion.scope_id == scope_id,
            ArtifactVersion.branch == branch_name,
        )
        .sort(("version_no", pymongo.DESCENDING))
        .first_or_none()
    )


def _scope_tags(*, scope_type: str, scope_id: str, workspace_id: str) -> list[str]:
    """Tenancy/scope tags stamped on every emitted Journal event.

    Always non-empty (the journal enforces min_length=1). The artifact tag
    comes first so a scope-prefix query can target one artifact.
    """
    return [f"{scope_type}:{scope_id}", f"workspace:{workspace_id}"]


def _emit_version_event(
    *,
    action: str,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    author: str | None,
    payload: dict,
) -> None:
    """Append a version Journal event directly (best-effort).

    NOT a Decision-Graph chain event, so we call ``journal.append`` directly
    rather than ``record_decision_event``. Lazy imports keep the soul-protocol
    journal types out of module import for static-analysis cleanliness and so
    a journal-less context (or a fork without the dep) degrades gracefully
    instead of failing the version write — the ArtifactVersion row is already
    the durable record; the event is the audit echo.
    """
    try:
        from soul_protocol.spec.journal import Actor, EventEntry

        from pocketpaw.journal_dep import get_journal
    except Exception:  # noqa: BLE001 — journal dep unavailable on a fork
        logger.debug("journal dep unavailable — skipping %s event", action)
        return

    # The actor is the author when known, else the system. ``scope_context``
    # mirrors the event scope so visibility filters downstream agree.
    scope = _scope_tags(scope_type=scope_type, scope_id=scope_id, workspace_id=workspace_id)
    actor = (
        Actor(kind="user", id=author, scope_context=scope)
        if author
        else Actor(kind="system", id="system:versions", scope_context=scope)
    )
    event = EventEntry(
        id=uuid4(),
        ts=datetime.now(UTC),
        actor=actor,
        action=action,
        scope=scope,
        payload=payload,
    )
    try:
        get_journal().append(event)
    except Exception:  # noqa: BLE001 — audit echo must not break the write
        logger.warning(
            "versions: failed to append %s for %s:%s — version row still written",
            action,
            scope_type,
            scope_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def write_draft(
    *,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    content: dict,
    branch: str = "main",  # noqa: A002 — domain term; not the builtin
    author: str | None = None,
    label: str | None = None,
) -> ArtifactVersion:
    """Create the next draft version on ``branch`` and make it the head.

    The new row gets the next monotonic ``version_no`` within
    (scope_type, scope_id, branch), ``status="draft"``, and a
    ``parent_version_id`` pointing at the prior branch head (None for the
    first version). Becomes the working draft (``get_draft`` returns it).

    Emits ``artifact.version.created``.
    """
    head = await _latest_in_branch(scope_type=scope_type, scope_id=scope_id, branch_name=branch)
    version_no = (head.version_no + 1) if head else 1

    row = ArtifactVersion(
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        branch=branch,
        version_no=version_no,
        content=content,
        parent_version_id=str(head.id) if head else None,
        status="draft",
        label=label,
        author=author,
    )
    await row.insert()

    _emit_version_event(
        action="artifact.version.created",
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        author=author,
        payload={
            "version_id": str(row.id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "branch": branch,
            "version_no": version_no,
            "parent_version_id": row.parent_version_id,
            "status": "draft",
            "label": label,
        },
    )
    return row


async def branch(
    *,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    new_branch: str,
    from_branch: str = "main",
    author: str | None = None,
    label: str | None = None,
) -> ArtifactVersion:
    """Open a candidate lineage ``new_branch`` off ``from_branch``'s head.

    Snapshots the source branch's current head (published if present, else
    latest) into a fresh ``version_no=1`` draft row on ``new_branch`` whose
    ``parent_version_id`` points back at the source head. This is the
    "propose" step of the Branch primitive: a candidate that BP-3's merge
    gate later reviews and (BP-3) merges back to ``from_branch``.

    Emits ``artifact.version.branched``.
    """
    # Prefer the published head as the branch point so a candidate starts from
    # what is live; fall back to the latest row when nothing is published yet.
    source = await get_published(
        scope_type=scope_type, scope_id=scope_id, branch=from_branch
    ) or await _latest_in_branch(scope_type=scope_type, scope_id=scope_id, branch_name=from_branch)
    source_content = source.content if source else {}
    source_id = str(source.id) if source else None

    row = ArtifactVersion(
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        branch=new_branch,
        version_no=1,
        content=source_content,
        parent_version_id=source_id,
        status="draft",
        label=label,
        author=author,
    )
    await row.insert()

    _emit_version_event(
        action="artifact.version.branched",
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        author=author,
        payload={
            "version_id": str(row.id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "branch": new_branch,
            "from_branch": from_branch,
            "parent_version_id": source_id,
            "version_no": 1,
            "status": "draft",
            "label": label,
        },
    )
    return row


async def publish(
    *,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    version_id: str,
) -> ArtifactVersion:
    """Mark ``version_id`` as the published pointer for its artifact.

    Flips the target row's ``status`` to ``"published"``. The published
    pointer (``get_published``) is derived as the latest ``published`` row,
    so promoting a newer version simply publishes it; older published rows
    stay as history. Deploy / Instinct wiring is NOT this function's job
    (TODO(BP-3)) — this is purely the state transition + pointer move.

    Raises ``ValueError`` if the version does not exist or belongs to a
    different artifact/workspace (defensive scope check).

    Emits ``artifact.version.published`` (BP-4) so the VersionProjection can
    fold the published-pointer move into the per-artifact history timeline.
    """
    row = await _scoped_row(
        scope_type=scope_type, scope_id=scope_id, workspace_id=workspace_id, version_id=version_id
    )
    row.status = "published"
    await row.save()
    _emit_version_event(
        action="artifact.version.published",
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        author=row.author,
        payload={
            "version_id": str(row.id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "branch": row.branch,
            "version_no": row.version_no,
            "status": "published",
        },
    )
    return row


async def _scoped_row(
    *, scope_type: str, scope_id: str, workspace_id: str, version_id: str
) -> ArtifactVersion:
    """Load a version by id and assert it belongs to (scope, workspace).

    The shared defensive scope check used by ``publish`` / ``mark_merged`` /
    ``discard``: a version mutation must never cross to another artifact or
    tenant. Raises ``ValueError`` on a missing row or a scope/workspace
    mismatch (the caller surfaces it). This is a SERVICE-level belt-and-braces
    check; the Instinct router's ``_assert_artifact_change_workspace`` is the
    primary tenant gate (it 403s before any state mutation).
    """
    row = await ArtifactVersion.get(version_id)
    if row is None:
        raise ValueError(f"artifact version not found: {version_id}")
    if row.scope_type != scope_type or row.scope_id != scope_id or row.workspace_id != workspace_id:
        raise ValueError(
            f"version {version_id} does not belong to "
            f"{scope_type}:{scope_id} in workspace {workspace_id}"
        )
    return row


async def mark_merged(
    *,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    version_id: str,
) -> ArtifactVersion:
    """Flip an ACCEPTED branch candidate to ``status="merged"`` (BP-3).

    The merge gate's APPROVE path calls :func:`publish` on the merge target
    first (that moves the published pointer), then marks the candidate row
    ``merged`` so the version log records that this candidate was accepted into
    the published lineage. Pure state transition + audit echo — it does NOT move
    the published pointer (publish already did).

    Raises ``ValueError`` if the version does not exist or belongs to a
    different artifact/workspace (same defensive scope check as ``publish``).

    Emits ``artifact.version.merged``.
    """
    row = await _scoped_row(
        scope_type=scope_type, scope_id=scope_id, workspace_id=workspace_id, version_id=version_id
    )
    row.status = "merged"
    await row.save()
    _emit_version_event(
        action="artifact.version.merged",
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        author=row.author,
        payload={
            "version_id": str(row.id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "branch": row.branch,
            "version_no": row.version_no,
            "status": "merged",
        },
    )
    return row


async def discard(
    *,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    version_id: str,
) -> ArtifactVersion:
    """Discard a REJECTED branch candidate (BP-3).

    The merge gate's REJECT path calls this: it flips the candidate row to
    ``status="reverted"`` so it no longer reads as the working draft, and leaves
    the PUBLISHED pointer entirely untouched (a rejection must never move what is
    live). This is the minimal, correct discard for BP-3.

    TODO(BP-4): revert/discard semantics deepen here — BP-4 owns reverting the
    published pointer to a prior snapshot and the Journal history projection.
    BP-3 only abandons the candidate draft.

    Raises ``ValueError`` if the version does not exist or belongs to a
    different artifact/workspace (same defensive scope check as ``publish``).

    Emits ``artifact.version.discarded``.
    """
    row = await _scoped_row(
        scope_type=scope_type, scope_id=scope_id, workspace_id=workspace_id, version_id=version_id
    )
    row.status = "reverted"
    await row.save()
    _emit_version_event(
        action="artifact.version.discarded",
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        author=row.author,
        payload={
            "version_id": str(row.id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "branch": row.branch,
            "version_no": row.version_no,
            "status": "reverted",
        },
    )
    return row


async def revert(
    *,
    scope_type: str,
    scope_id: str,
    workspace_id: str,
    version_id: str,
    author: str | None = None,
    label: str | None = None,
) -> ArtifactVersion:
    """Revert an artifact to a prior version (BP-4).

    Reverting writes a NEW draft on the target version's branch whose
    ``content`` is a copy of the target version's content, and makes it the
    head. Revert moves FORWARD — it never mutates history. The user can then
    ``publish()`` the new draft to take the reverted content live, exactly as
    they would publish any other draft. This keeps the version log append-only
    and makes "revert" a first-class, auditable lineage step rather than a
    destructive rewrite.

    ``label`` defaults to a human-readable "Revert to v<n>" so the history view
    reads cleanly; callers can override it.

    Raises ``ValueError`` if the target version does not exist or belongs to a
    different artifact/workspace (the shared ``_scoped_row`` defensive check —
    a revert must never cross tenants or artifacts).

    Emits ``artifact.version.reverted`` (carrying ``reverted_from`` = the target
    version id + the new draft's id) so the VersionProjection records the revert
    as its own lifecycle event in the history timeline.
    """
    target = await _scoped_row(
        scope_type=scope_type, scope_id=scope_id, workspace_id=workspace_id, version_id=version_id
    )

    revert_label = label or f"Revert to v{target.version_no}"

    # Write the new draft on the SAME branch as the target so the revert lands
    # on the lineage it is reverting (typically "main"). Snapshot the target's
    # content (a shallow dict copy so a later mutation of one row never bleeds
    # into the other — content is a small full snapshot, not a diff).
    head = await _latest_in_branch(
        scope_type=scope_type, scope_id=scope_id, branch_name=target.branch
    )
    version_no = (head.version_no + 1) if head else 1

    new_draft = ArtifactVersion(
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        branch=target.branch,
        version_no=version_no,
        content=dict(target.content),
        parent_version_id=str(head.id) if head else None,
        status="draft",
        label=revert_label,
        author=author,
    )
    await new_draft.insert()

    _emit_version_event(
        action="artifact.version.reverted",
        scope_type=scope_type,
        scope_id=scope_id,
        workspace_id=workspace_id,
        author=author,
        payload={
            "version_id": str(new_draft.id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "branch": target.branch,
            "version_no": version_no,
            "parent_version_id": new_draft.parent_version_id,
            "reverted_from": str(target.id),
            "reverted_from_version_no": target.version_no,
            "status": "draft",
            "label": revert_label,
        },
    )
    return new_draft


# ---------------------------------------------------------------------------
# Pointer reads (derived from the versions collection)
# ---------------------------------------------------------------------------


async def get_draft(
    *,
    scope_type: str,
    scope_id: str,
    branch: str = "main",  # noqa: A002
) -> ArtifactVersion | None:
    """The current working draft: latest ``draft`` row in (scope, branch)."""
    return (
        await ArtifactVersion.find(
            ArtifactVersion.scope_type == scope_type,
            ArtifactVersion.scope_id == scope_id,
            ArtifactVersion.branch == branch,
            ArtifactVersion.status == "draft",
        )
        .sort(("version_no", pymongo.DESCENDING))
        .first_or_none()
    )


async def get_published(
    *,
    scope_type: str,
    scope_id: str,
    branch: str = "main",  # noqa: A002
) -> ArtifactVersion | None:
    """The published pointer: latest ``published`` row in (scope, branch)."""
    return (
        await ArtifactVersion.find(
            ArtifactVersion.scope_type == scope_type,
            ArtifactVersion.scope_id == scope_id,
            ArtifactVersion.branch == branch,
            ArtifactVersion.status == "published",
        )
        .sort(("version_no", pymongo.DESCENDING))
        .first_or_none()
    )


async def list_versions(
    *,
    scope_type: str,
    scope_id: str,
    branch: str | None = None,  # noqa: A002
    limit: int = 100,
) -> list[ArtifactVersion]:
    """The version log for an artifact, newest first.

    Scoped to one ``branch`` when given, else all branches of the artifact.
    """
    query = [
        ArtifactVersion.scope_type == scope_type,
        ArtifactVersion.scope_id == scope_id,
    ]
    if branch is not None:
        query.append(ArtifactVersion.branch == branch)
    return (
        await ArtifactVersion.find(*query)
        .sort(("version_no", pymongo.DESCENDING))
        .limit(limit)
        .to_list()
    )
