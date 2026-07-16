"""WebSandbox document — the Sandbox Registry for the Web Cursor browser IDE.

Created 2026-07-15 (WC-1, feat/websandbox-registry): the tenancy/auth oracle
that maps ``(workspace_id, user_id, repo) -> sandbox_id + lifecycle state +
installation pointer``. One row per ``(workspace_id, user_id, repo)`` (enforced
by a unique compound index AND app-level upsert in the service). This is the
security foundation every later Web Cursor slice authorizes against.

Only ``ee.cloud.websandbox.service`` imports this doc class directly
(import-linter "WebSandbox" contract, same discipline as AuditEvent).

The ``installation_id`` is a plain optional str here — a pointer to a future
GitHub App installation. WC-6 encrypts it at rest; until then it is stored in
the clear (never a token, only an installation identifier).

Changed 2026-07-15 (WC-S3, feat/websandbox-s3-durability): added
``snapshot_file_id`` — a pointer to the tenant's most recent workspace snapshot
in blob storage (an ``EEUploadService`` FileRecord id). The VM is scratch and
gets reaped; this durable pointer lets a user's uncommitted work + workspace
state be restored into a fresh VM. Null until the first snapshot is taken. No
new index needed — it's read only via the owner-scoped registry row.

Changed 2026-07-15 (WC-5a, feat/websandbox-edit-agent): added ``branch`` — the
auto-created ``paw/edit-<hex>`` feature branch the repo is checked out onto in
the VM at open time, so AI edits never touch the checked-out default branch.
Null until the provisioner creates it (right after the clone). No new index
needed — it's read only via the owner-scoped registry row.

Changed 2026-07-16 (CM-2a′ write-through, feat/code-mode): added ``overlay`` —
the incremental durability tier. Every editor save (``file.write``) mirrors the
file to the tenant's blob storage and records ``relpath -> FileRecord id`` here,
so a crash / idle-out between full snapshots doesn't lose edits: ``open_project``
replays the overlay onto the fresh clone. ``set_snapshot`` CLEARS it — a full
workspace snapshot supersedes the incremental overlay (and clearing on snapshot
is what avoids replaying a stale write over a since-deleted file). No new index
needed — read only via the owner-scoped registry row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class WebSandbox(Document):
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    user_id: Indexed(str)  # type: ignore[valid-type]
    # A repo identifier / URL the sandbox is opened against.
    repo: str
    # The Daytona sandbox id — null until the VM is cold-provisioned (WC-2).
    sandbox_id: str | None = None
    # Lifecycle state: pending | opening | ready | stopped | reaped.
    status: str = "pending"
    # Pointer to a future GitHub App installation (WC-6 encrypts this at rest;
    # plain optional str for now — an installation id, never a token).
    installation_id: str | None = None
    # Pointer to the latest durable workspace snapshot in the tenant's blob
    # storage (an EEUploadService FileRecord id). Null until the first snapshot;
    # set by ``service.set_snapshot`` from the WC-S3 durability module.
    snapshot_file_id: str | None = None
    # The auto-created ``paw/edit-<hex>`` feature branch checked out in the VM at
    # open time (WC-5a). Null until the provisioner creates it after the clone.
    branch: str | None = None
    # The write-through durability overlay (CM-2a′): ``relpath -> FileRecord id``
    # for each editor-saved file mirrored to blob storage since the last full
    # snapshot. Replayed onto a fresh clone on reopen; cleared by ``set_snapshot``.
    overlay: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "web_sandboxes"
        indexes = [
            # One sandbox per (workspace, user, repo) — the registry key.
            IndexModel(
                [("workspace_id", 1), ("user_id", 1), ("repo", 1)],
                unique=True,
                name="ws_user_repo_unique",
            ),
            # The per-user list read path (list_sandboxes).
            IndexModel([("workspace_id", 1), ("user_id", 1)]),
            # The authorize-by-Daytona-id read path (authorize_sandbox).
            IndexModel([("workspace_id", 1), ("sandbox_id", 1)]),
        ]
