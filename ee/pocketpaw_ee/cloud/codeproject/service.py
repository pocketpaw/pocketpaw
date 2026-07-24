# service.py — Code Mode durable-project registry business logic (CM-2a).
# Created 2026-07-16 (feat/code-mode): the DURABLE half of Code Mode's two-
# lifecycle model. This module IS the repository (ee/cloud Rule 1) and the ONLY
# module allowed to import the CodeProject Beanie doc (Rule 2). Every read carries
# a tenant + owner filter (Rule 7); every mutation validates at entry (Rule 6) and
# emits an event or is marked ``# no-event:`` (Rule 9). Errors are CloudError
# subclasses, never HTTPException (Rule 10).
#
# The project is the deep-linkable, reap-surviving identity behind ``/code/<id>``;
# the ephemeral Daytona sandbox (a WebSandbox row) is bound via
# ``current_sandbox_id`` and resolved lazily by ``codeproject/lifecycle.open_project``
# (reuse the bound row if it's live, else provision a fresh one and rebind). The
# orchestration lives in lifecycle.py so this module stays the sole doc writer.
#
# Modified 2026-07-22 (fix/starter-project-collision): ``create_project``'s
# idempotency is now provider-dependent. It was keyed on
# (workspace, user, provider, repo) for every provider, which is only correct
# while ``repo`` names an IDENTITY. A starter project puts a TEMPLATE id there
# and the catalog has four, so "build me a todo app" and "build me a blog" both
# planned to ``react``, hit the idempotency check, and the second create returned
# the FIRST project — the user's name was discarded, they were navigated to the
# older project, and the projects tab still showed one row. Scaffold providers
# now always insert; identity providers keep the existing behaviour exactly, so a
# returning user still lands back on the same ``/code/<id>`` for a repo.
#
# Modified 2026-07-24 (feat/code-durable-project-store): added the project-keyed
# durability pointer ops — ``set_project_snapshot`` (records the S3 snapshot
# pointer and CLEARS the overlay, since a full snapshot supersedes it),
# ``set_project_overlay_entry`` / ``drop_project_overlay_entry`` /
# ``move_project_overlay_entry`` (the incremental per-file tier). These mirror the
# WebSandbox service ops one-for-one but anchor the pointer on the durable PROJECT
# row instead of the ephemeral sandbox row, so a project's files round-trip
# through blob storage independent of any runtime. All are owner-scoped (Rule 7),
# and all are ``# no-event:`` durability bookkeeping (same reasoning as
# ``set_snapshot`` on WebSandbox — no lifecycle transition to announce). This
# module stays the sole writer of the CodeProject doc (Rule 2). The orchestration
# that drives them (tar/untar + S3 upload) lives in ``websandbox/durability.py``.
#
# Modified 2026-07-25 (B2, feat/code-daytona-project-anchor): added the two ops the
# Daytona cutover needs, because the runtime knows only its EPHEMERAL sandbox row
# and the durable state now lives on the PROJECT.
#   * ``find_project_for_sandbox`` — the reverse of ``bind_current_sandbox``: given
#     a WebSandbox row id, which owned project is currently bound to it? The
#     terminal socket has a row id and needs a project id to mirror/snapshot
#     against. Ordered by ``last_opened_at`` desc because the bind is not unique:
#     a WebSandbox row is keyed (workspace, user, repo), so two projects built from
#     the same starter template can point at ONE row — the most recently opened is
#     the one the user is actually looking at.
#   * ``adopt_legacy_durability`` — the rollout backfill. Copies a legacy
#     sandbox-keyed ``snapshot_file_id`` / ``overlay`` onto the project so an
#     existing repo project's uncommitted work survives the cutover. Idempotent and
#     NON-destructive by construction: it writes only when the project holds no
#     durable state of its own, so a second call (or a later open) can never
#     clobber state the project has since accumulated. The WebSandbox fields are
#     left readable — this PR moves WHO the Daytona path writes, not what exists.
#
# Modified 2026-07-24 (feat/code-initial-prompt): ``create_project`` now persists
# the optional ``initial_prompt`` (the WHAT-to-build description) on an actual
# INSERT only — a github idempotent hit returns the existing row untouched, so a
# repeat create never overwrites a project's prompt. The prompt + its
# ``initial_prompt_consumed`` flag flow doc -> view -> wire (mirroring
# ``snapshot_file_id``). Added ``mark_initial_prompt_consumed`` — an owner-scoped
# (Rule 7) op that sets the consumed flag on build-turn START and re-arms it to
# False on a retry-build; idempotent (already-in-state is a clean no-op) and
# ``# no-event:`` (a build-turn bookkeeping flag, not a lifecycle transition).
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    CodeProjectCreated,
    CodeProjectDeleted,
    CodeProjectOpened,
    CodeProjectRenamed,
)
from pocketpaw_ee.cloud.codeproject.domain import (
    CodeProjectId,
    CodeProjectView,
    is_scaffold_provider,
)
from pocketpaw_ee.cloud.codeproject.dto import (
    CodeProjectResponse,
    ConsumePromptRequest,
    CreateProjectRequest,
    RenameProjectRequest,
)
from pocketpaw_ee.cloud.models.code_project import CodeProject as _CodeProjectDoc

logger = logging.getLogger(__name__)


def _short_name(repo: str) -> str:
    """Derive a friendly default display name from a repo URL / "owner/repo".

    Strips a trailing ``.git`` and any path, leaving the last path segment (the
    repo's short name). Falls back to the raw value if there's nothing to strip.
    """
    trimmed = repo.strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    tail = trimmed.rsplit("/", 1)[-1]
    return tail or trimmed


def _registry_key(provider: str) -> str:
    """The immutable tail of the registry key for a new row.

    Empty for an identity provider, so every project for a given repo shares one
    key and the collection's unique index enforces one-project-per-repo. A fresh
    random token for a scaffold provider, so two projects built from the same
    starter are two rows rather than a duplicate-key collision.

    The token is minted here, at insert, and never rewritten — the whole point of
    keying on it instead of on ``name`` is that nothing a user does later can
    move it.
    """
    return secrets.token_hex(8) if is_scaffold_provider(provider) else ""


def _doc_to_view(doc: _CodeProjectDoc) -> CodeProjectView:
    """Map a persisted, tenant-checked row to its read model."""
    return CodeProjectView(
        id=CodeProjectId(str(doc.id)),
        workspace_id=doc.workspace_id,
        user_id=doc.user_id,
        name=doc.name,
        provider=doc.provider,
        repo=doc.repo,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        snapshot_file_id=doc.snapshot_file_id,
        overlay=dict(doc.overlay or {}),
        initial_prompt=doc.initial_prompt,
        initial_prompt_consumed=doc.initial_prompt_consumed,
        current_sandbox_id=doc.current_sandbox_id,
        last_opened_at=doc.last_opened_at,
    )


def view_to_wire(view: CodeProjectView) -> CodeProjectResponse:
    """Map a view to the camelCase wire response (Rule 8 — mapping lives here)."""
    return CodeProjectResponse(
        id=view.id,
        workspaceId=view.workspace_id,
        userId=view.user_id,
        name=view.name,
        provider=view.provider,
        repo=view.repo,
        initialPrompt=view.initial_prompt,
        initialPromptConsumed=view.initial_prompt_consumed,
        snapshotFileId=view.snapshot_file_id,
        currentSandboxId=view.current_sandbox_id,
        lastOpenedAt=view.last_opened_at.isoformat() if view.last_opened_at else None,
        createdAt=view.created_at.isoformat(),
        updatedAt=view.updated_at.isoformat(),
    )


async def create_project(
    workspace_id: str,
    user_id: str,
    body: CreateProjectRequest | dict,
) -> CodeProjectView:
    """Register (or return) the durable project for a create request.

    Idempotency is PROVIDER-DEPENDENT, because ``repo`` means two different
    things depending on who filled it in.

    For an identity provider (``github``) the repo string names the project:
    cloning ``acme/widgets`` twice is the same project, so a second create
    returns the EXISTING row unchanged and a returning user lands back on the
    same ``/code/<id>``. That behaviour is deliberate and unchanged.

    For a scaffold provider (``starter``) the repo string names the TEMPLATE the
    project starts from. The catalog holds four, so two unrelated prompts sharing
    one starter is the ordinary case, not a duplicate — deduplicating there
    silently swallowed the user's second project. Scaffold creates therefore
    always insert. Guarding a double-submit is the caller's job (the ``/code``
    landing page latches its confirm button and navigates away on success); it is
    not something the registry can infer from a template id.

    Only an actual insert emits ``CodeProjectCreated`` (an idempotent hit is not
    a mutation).
    """
    body = CreateProjectRequest.model_validate(body)

    if not is_scaffold_provider(body.provider):
        existing = await _CodeProjectDoc.find_one(
            {  # Rule 7 tenant + owner filter
                "workspace_id": workspace_id,
                "user_id": user_id,
                "provider": body.provider,
                "repo": body.repo,
            }
        )
        if existing is not None:
            # no-event: idempotent hit — nothing was written, so nothing to announce.
            return _doc_to_view(existing)

    doc = _CodeProjectDoc(
        workspace_id=workspace_id,
        user_id=user_id,
        name=body.name or _short_name(body.repo),
        provider=body.provider,
        repo=body.repo,
        registry_key=_registry_key(body.provider),
        # The WHAT-to-build description, recorded ONLY on this actual insert — an
        # idempotent hit returned above unchanged, so a repeat create never
        # overwrites an existing project's prompt.
        initial_prompt=body.initial_prompt,
        initial_prompt_consumed=False,
    )
    await doc.insert()

    await emit(
        CodeProjectCreated(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": doc.repo,
                "provider": doc.provider,
            }
        )
    )
    return _doc_to_view(doc)


async def list_projects(workspace_id: str, user_id: str) -> list[CodeProjectView]:
    """List every project owned by the caller, most-recently-touched first.

    Tenant-filtered by ``workspace_id`` AND owner-filtered by ``user_id`` so one
    user never sees another's projects even within a shared workspace. Sorted by
    ``updated_at`` desc (bumped on every open) — a mongomock-safe find + sort, no
    aggregate.
    """
    docs = (
        await _CodeProjectDoc.find(
            {"workspace_id": workspace_id, "user_id": user_id}  # Rule 7 tenant filter
        )
        .sort([("updated_at", -1)])
        .to_list()
    )
    return [_doc_to_view(d) for d in docs]


async def get_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
) -> CodeProjectView:
    """Read one project by its registry id, tenant- and owner-scoped.

    Raises ``NotFound`` when no row matches the caller's workspace + user — a row
    owned by another tenant is indistinguishable from a missing one.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)
    return _doc_to_view(doc)


async def rename_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
    body: RenameProjectRequest | dict,
) -> CodeProjectView:
    """Rename a project's display name, tenant- and owner-scoped.

    Validates + trims the new name (Rule 6). Raises ``NotFound`` for a row the
    caller doesn't own. A no-op rename (same name) still returns the view but does
    not emit. Emits ``CodeProjectRenamed`` on an actual change (Rule 9).
    """
    body = RenameProjectRequest.model_validate(body)
    new_name = body.name.strip()

    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)

    if doc.name == new_name:
        # no-event: nothing changed.
        return _doc_to_view(doc)

    doc.name = new_name
    doc.updated_at = datetime.now(UTC)
    await doc.save()

    await emit(
        CodeProjectRenamed(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "name": new_name,
            }
        )
    )
    return _doc_to_view(doc)


async def mark_initial_prompt_consumed(
    workspace_id: str,
    user_id: str,
    project_id: str,
    body: ConsumePromptRequest | dict,
) -> CodeProjectView:
    """Set (or re-arm) the project's ``initial_prompt_consumed`` flag.

    Tenant- and owner-scoped (Rule 7 via ``_read_owned``): a caller can only touch
    a project they own; a foreign / missing id raises ``NotFound``. The frontend
    calls this with ``consumed=True`` when a build turn STARTS so a reopen doesn't
    re-run the same auto-build, and with ``consumed=False`` on a retry-build to
    re-arm the prompt. Validated at entry (Rule 6). Idempotent: setting the flag to
    the value it already holds writes nothing and returns the current view.
    """
    body = ConsumePromptRequest.model_validate(body)

    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)

    if doc.initial_prompt_consumed == body.consumed:
        # no-event: already in the requested state — nothing was written.
        return _doc_to_view(doc)

    doc.initial_prompt_consumed = body.consumed
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    # no-event: the consumed flag is build-turn bookkeeping the frontend owns end
    # to end (it sets it on turn start, re-arms it on retry) — no downstream
    # handler reacts to it, and a projects-grid fan-out must not read it as a
    # lifecycle transition. Same reasoning as the durability pointer writes above.
    return _doc_to_view(doc)


async def delete_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
) -> CodeProjectView:
    """Delete a project row, tenant- and owner-scoped. Returns the deleted view.

    Only removes the durable CodeProject doc — tearing down the bound ephemeral
    sandbox VM is the caller's job (``codeproject/lifecycle.delete_project``), which
    has the Daytona client; this module stays the sole doc writer. Raises
    ``NotFound`` for a row the caller doesn't own. Emits ``CodeProjectDeleted``
    (Rule 9) so a projects-grid fan-out can drop the card.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)

    view = _doc_to_view(doc)
    await doc.delete()

    await emit(
        CodeProjectDeleted(
            data={
                "id": view.id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": view.repo,
            }
        )
    )
    return view


async def bind_current_sandbox(
    workspace_id: str,
    user_id: str,
    project_id: str,
    sandbox_row_id: str,
) -> CodeProjectView:
    """Bind the project to its CURRENT ephemeral sandbox and stamp last-opened.

    Tenant- and owner-scoped: a caller can only bind a sandbox onto a project they
    own. ``sandbox_row_id`` is a WebSandbox registry id.
    ``codeproject/lifecycle.open_project`` calls this after resolving (reusing or
    provisioning) the sandbox, so the durable project always points at the latest
    runtime. Emits ``CodeProjectOpened`` — the "project now has a live sandbox"
    transition a projects-grid fan-out reacts to.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)

    now = datetime.now(UTC)
    doc.current_sandbox_id = sandbox_row_id
    doc.last_opened_at = now
    doc.updated_at = now
    await doc.save()

    await emit(
        CodeProjectOpened(
            data={
                "id": str(doc.id),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "repo": doc.repo,
                "sandbox_id": sandbox_row_id,
            }
        )
    )
    return _doc_to_view(doc)


async def find_project_for_sandbox(
    workspace_id: str,
    user_id: str,
    sandbox_row_id: str,
) -> CodeProjectView | None:
    """Which owned project is currently bound to this ephemeral sandbox row?

    The reverse of ``bind_current_sandbox``, and the seam the Daytona runtime
    needs: a terminal socket is opened against a WebSandbox ROW id, but durable
    state is anchored on the PROJECT, so the socket has to resolve its owner
    before it can mirror or snapshot.

    Returns ``None`` rather than raising when nothing is bound — a sandbox opened
    outside the project flow (the plain ``/websandbox`` REST surface) has no owning
    project, and that is a normal state the caller degrades on, not an error.

    The bind is deliberately NOT treated as unique. A WebSandbox row is keyed
    (workspace, user, repo) while a project is not, so two scaffold projects
    started from the same template id can legitimately point at one row. Ordering
    by ``last_opened_at`` desc picks the project the user most recently opened —
    which is the one whose editor this socket belongs to, since ``open_project``
    re-stamps that field on every open right before the socket connects. ``_id``
    desc breaks a tie: BSON datetimes are millisecond-precision, so two binds
    inside one millisecond would otherwise order arbitrarily, and an arbitrary
    durable anchor is not something to leave to chance.

    Tenant- AND owner-filtered (Rule 7): another tenant's project bound to the same
    row id is invisible here.
    """
    docs = (
        await _CodeProjectDoc.find(
            {  # Rule 7 tenant + owner filter
                "workspace_id": workspace_id,
                "user_id": user_id,
                "current_sandbox_id": sandbox_row_id,
            }
        )
        .sort([("last_opened_at", -1), ("_id", -1)])
        .to_list()
    )
    if not docs:
        return None
    return _doc_to_view(docs[0])


# ---------------------------------------------------------------------------
# Project-keyed durability pointers (feat/code-durable-project-store).
#
# The durable half of Code Mode's build-and-persist loop: a project's file
# snapshot + per-file overlay live in the tenant's blob storage, and these ops
# record/mutate the POINTERS on the durable project row. They mirror the
# WebSandbox service ops (``set_snapshot`` / ``set_overlay_entry`` /
# ``drop_overlay_entry`` / ``move_overlay_entry``) one-for-one, so the project
# store behaves identically to the sandbox store — only the anchor differs. The
# tar/untar + S3 upload orchestration that calls these lives in
# ``websandbox/durability.py`` (the layer allowed to import EEUploadService);
# this module stays the sole writer of the CodeProject doc (Rule 2). All are
# owner-scoped (Rule 7 via ``_read_owned``) and ``# no-event:`` (durability
# bookkeeping, not a lifecycle transition — same reasoning as WebSandbox's
# ``set_snapshot``).
# ---------------------------------------------------------------------------


async def set_project_snapshot(
    workspace_id: str,
    user_id: str,
    project_id: str,
    file_id: str,
) -> CodeProjectView:
    """Record the durable project-snapshot pointer; clear the overlay.

    Tenant- and owner-scoped (Rule 7 via ``_read_owned``): only the owning caller
    can bind a snapshot to their own project. ``file_id`` is the blob-storage
    ``FileRecord`` id produced by ``EEUploadService.upload`` in the durability
    module — a durable pointer to the tarball of the project's files. A full
    snapshot supersedes the incremental overlay, so the overlay is CLEARED here
    (mirroring ``WebSandbox.set_snapshot``): everything the overlay held is now
    inside the snapshot tar, and clearing it is what keeps restore from replaying
    a stale write over a since-deleted file. Raises ``NotFound`` when no owned row
    matches.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)
    doc.snapshot_file_id = file_id
    doc.overlay = {}
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    # no-event: the snapshot pointer is durability bookkeeping, not a lifecycle
    # transition — no downstream handler reacts to it, and emitting a project
    # event would falsely signal a state change to a projects-grid fan-out.
    return _doc_to_view(doc)


async def set_project_overlay_entry(
    workspace_id: str,
    user_id: str,
    project_id: str,
    rel_path: str,
    file_id: str,
) -> CodeProjectView:
    """Record one write-through overlay entry (``rel_path -> FileRecord id``).

    Tenant- and owner-scoped (Rule 7 via ``_read_owned``): only the owning caller
    can mirror a file onto their own project. Called best-effort from the file
    write path after the byte-for-byte VM write, so every editor save is durable
    in blob storage the moment it lands — a crash / idle-out before the next full
    snapshot no longer loses the edit. Last-write-wins per path. Raises
    ``NotFound`` when no owned row matches.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)
    # Reassign (not in-place mutate) so Beanie always sees the field as dirty.
    overlay = dict(doc.overlay or {})
    overlay[rel_path] = file_id
    doc.overlay = overlay
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    # no-event: incremental durability bookkeeping, same reasoning as set_project_snapshot.
    return _doc_to_view(doc)


async def drop_project_overlay_entry(
    workspace_id: str,
    user_id: str,
    project_id: str,
    rel_path: str,
) -> CodeProjectView:
    """Drop overlay entries for ``rel_path`` (and anything under it) from a project.

    Tenant- and owner-scoped (Rule 7 via ``_read_owned``). Called best-effort from
    the file delete path after the VM delete: DROPPING an overlay entry is always
    safe — it just falls back to the snapshot tier on restore, and it stops a
    since-deleted file being resurrected from the overlay. A directory delete
    drops every child under ``rel_path + '/'`` too. Raises ``NotFound`` when no
    owned row matches.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)
    prefix = rel_path.rstrip("/") + "/"
    # Reassign (not in-place mutate) so Beanie always sees the field as dirty.
    overlay = {
        k: v for k, v in (doc.overlay or {}).items() if k != rel_path and not k.startswith(prefix)
    }
    doc.overlay = overlay
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    # no-event: incremental durability bookkeeping, same reasoning as set_project_snapshot.
    return _doc_to_view(doc)


async def move_project_overlay_entry(
    workspace_id: str,
    user_id: str,
    project_id: str,
    src: str,
    dst: str,
) -> CodeProjectView:
    """Re-key overlay entries from ``src`` to ``dst`` (rename == move).

    Tenant- and owner-scoped (Rule 7 via ``_read_owned``). Called best-effort from
    the file move path after the VM move. The re-key is safe: the FileRecord id
    (the actual blob) is unchanged — only its overlay KEY moves — so restore
    replays the same content at the file's new path. A directory move re-keys
    every child under ``src + '/'`` to the matching ``dst + '/'`` path; a path
    with no overlay entry is a clean no-op. Raises ``NotFound`` when no owned row
    matches.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)
    src_prefix = src.rstrip("/") + "/"
    dst_prefix = dst.rstrip("/") + "/"
    overlay: dict[str, str] = {}
    for k, v in (doc.overlay or {}).items():
        if k == src:
            overlay[dst] = v
        elif k.startswith(src_prefix):
            overlay[dst_prefix + k[len(src_prefix) :]] = v
        else:
            overlay[k] = v
    doc.overlay = overlay
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    # no-event: incremental durability bookkeeping, same reasoning as set_project_snapshot.
    return _doc_to_view(doc)


async def adopt_legacy_durability(
    workspace_id: str,
    user_id: str,
    project_id: str,
    snapshot_file_id: str | None,
    overlay: dict[str, str] | None,
) -> CodeProjectView:
    """Adopt legacy sandbox-keyed durable state onto the project (B2 backfill).

    Durability used to be anchored on the EPHEMERAL WebSandbox row. B2 moved the
    Daytona path onto the durable project, so an existing project whose only
    durable state sits on its old sandbox row would otherwise reopen empty. This
    lifts that state across, once.

    Two guarantees make it safe to call on every open (Rule: non-destructive):
      * It writes ONLY when the project holds no durable state of its own — no
        snapshot pointer and an empty overlay. A project that has since snapshotted
        or mirrored anything is returned untouched, so a stale legacy pointer can
        never overwrite fresher work.
      * Nothing is deleted. The WebSandbox fields stay readable for the whole
        rollout window; this copies, it does not move.

    Tenant- and owner-scoped (Rule 7 via ``_read_owned``); raises ``NotFound`` when
    no owned row matches. Passing nothing to adopt is a clean no-op.
    """
    doc = await _read_owned(workspace_id, user_id, project_id)
    if doc is None:
        raise NotFound("code_project", project_id)

    if doc.snapshot_file_id or doc.overlay:
        # no-event: the project already owns durable state — adopting would be a
        # regression, so this is deliberately a read.
        return _doc_to_view(doc)
    legacy_overlay = dict(overlay or {})
    if not snapshot_file_id and not legacy_overlay:
        # no-event: nothing to adopt.
        return _doc_to_view(doc)

    doc.snapshot_file_id = snapshot_file_id
    doc.overlay = legacy_overlay
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    logger.info(
        "codeproject.adopt_legacy_durability: project=%s <- snapshot=%s, overlay=%d file(s)",
        project_id,
        snapshot_file_id,
        len(legacy_overlay),
    )
    # no-event: durability bookkeeping, same reasoning as set_project_snapshot — a
    # migration copy is not a lifecycle transition a projects-grid should react to.
    return _doc_to_view(doc)


async def _read_owned(
    workspace_id: str,
    user_id: str,
    project_id: str,
) -> _CodeProjectDoc | None:
    """Tenant- + owner-scoped fetch by registry id. Returns None if not owned.

    An unparseable ``project_id`` yields None (treated as not-found) rather than
    raising, so a malformed id can't leak a distinct error shape.
    """
    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(project_id)
    except Exception:  # noqa: BLE001 — a bad id is simply "no such owned row"
        return None
    return await _CodeProjectDoc.find_one(
        {"_id": oid, "workspace_id": workspace_id, "user_id": user_id}  # Rule 7
    )


__all__ = [
    "adopt_legacy_durability",
    "bind_current_sandbox",
    "create_project",
    "delete_project",
    "drop_project_overlay_entry",
    "find_project_for_sandbox",
    "get_project",
    "list_projects",
    "mark_initial_prompt_consumed",
    "move_project_overlay_entry",
    "rename_project",
    "set_project_overlay_entry",
    "set_project_snapshot",
    "view_to_wire",
]
