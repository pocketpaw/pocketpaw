"""CodeProject document — the durable project registry for Code Mode (CM-2a).

Created 2026-07-16 (feat/code-mode): the DURABLE half of Code Mode's two-lifecycle
model. A CodeProject outlives any single Daytona sandbox — the VM is ephemeral
(reaped on idle) and its files are backed up to blob storage; the project persists
the identity, the S3 snapshot pointer, and a nullable pointer to the CURRENT
sandbox. Opening a project reuses its live sandbox or provisions a fresh one and
restores the snapshot into it, so a user who returns after a long gap gets their
work back instead of an empty machine.

One row per (workspace_id, user_id, provider, repo, registry_key). Only
``ee.cloud.codeproject.service`` imports this doc class directly (import-linter
"CodeProject" contract, same discipline as WebSandbox / AuditEvent).

Modified 2026-07-22 (fix/starter-project-collision): added ``registry_key`` and
widened the unique index to include it. The old key was
(workspace_id, user_id, provider, repo), which is right only while ``repo``
names an IDENTITY. A SCAFFOLD project (``provider="starter"``) puts a TEMPLATE
id there, and the catalog has four, so two unrelated prompts that both plan to
``react`` collided and the second create silently returned the first project.
``registry_key`` is the immutable tail that separates them: empty for identity
providers (so one row per repo still holds, enforced by the index and not just
by the service's read-then-insert), a per-row token for scaffold providers (so
they never dedupe). NOTE: the index has a NEW NAME, and Beanie only ever
*creates* indexes — the superseded ``ws_user_provider_repo_unique`` survives in
every existing deployment and would still reject the second starter row, so
``shared/db.py`` drops it at boot (same reconcile the invite-token rollout
needed).

Relation to WebSandbox: WebSandbox stays the EPHEMERAL runtime row (Daytona VM +
lifecycle); CodeProject is the durable owner and points at the current one via
``current_sandbox_id`` (the WebSandbox row id), null when none is live.

Modified 2026-07-24 (feat/code-durable-project-store): added ``overlay`` — the
incremental per-file durability tier, keyed on the PROJECT rather than the
ephemeral WebSandbox row. Mirrors ``WebSandbox.overlay`` (``relpath ->
FileRecord id``): each mirrored editor save records an entry here, ``restore``
replays them onto a fresh clone, and ``set_project_snapshot`` CLEARS it (a full
snapshot supersedes the overlay, and clearing on snapshot is what avoids
replaying a stale write over a since-deleted file). This makes a project-keyed
durable store — snapshot pointer + overlay — that round-trips through S3
independent of any runtime. Additive: the sandbox-keyed WebSandbox durability
path is unchanged. No new index — read only via the owner-scoped project row.

Modified 2026-07-25 (feat/code-s3-authoritative): the per-file ``overlay`` is now
the AUTHORITATIVE store and the tarball tier is retired as a source of truth. This
REVERSES the 2026-07-24 note above ("``set_project_snapshot`` CLEARS it"), for a
structural reason: a tarball is an unmodifiable blob, so a DELETE could not be
represented in it — replaying the baseline resurrected every file the user had
removed, on both runtimes, and clearing the overlay on snapshot threw away the only
tier that COULD represent the absence. Under the new model the overlay is a complete
image of the workspace (seeded by ``durability.sync_project_files``, which enumerates
the VM and drops entries for paths that no longer exist), so a delete is simply the
removal of an entry and there is nothing left to resurrect it. Two consequences here:
``snapshot_file_id`` is now a LEGACY pointer, read once by the migration in
``durability.restore_project`` (expand the tar into per-file entries) and then
cleared forever; and ``overlay_complete`` records whether the overlay has been
verified against a real workspace, which is what makes the destructive operations
(pruning stale files out of a restored VM) safe to run at all.

Modified 2026-07-24 (feat/code-initial-prompt): added ``initial_prompt`` (the
natural-language description of WHAT to build, captured when a ``/code`` project
is created from a prompt) and ``initial_prompt_consumed`` (whether the auto-run
build turn has already been kicked off for it). The frontend reads the prompt on
first open to auto-run one build turn and flips ``consumed`` on turn START; a
retry-build re-arms it. Both are nullable/defaulted so every existing row stays
valid. No new index — read only via the owner-scoped project row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class CodeProject(Document):
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    user_id: Indexed(str)  # type: ignore[valid-type]
    # Display name (defaults to the repo's short name at creation).
    name: str
    # Which code host this project's repo lives on. Multi-provider ready — GitHub
    # today, Google + others planned (see the RepoAuthProvider seam). Kept as a
    # plain str (not an enum) so a new provider needs no migration.
    provider: str = "github"
    # The provider-native repo identifier (a public URL today; "owner/repo" once
    # the GitHub App connect flow lands). For a SCAFFOLD provider ("starter")
    # this is a starter id from the codescaffold catalog, not a repo.
    repo: str
    # The immutable tail of the registry key — what makes two rows that share
    # (workspace, user, provider, repo) legitimately distinct.
    #
    # Empty for identity providers (github): every row for a given repo carries
    # "", so the unique index below enforces one-project-per-repo. A per-row
    # token for scaffold providers, where `repo` names a template and two
    # projects sharing it is normal.
    #
    # Deliberately NOT the display name, even though name is what distinguishes
    # two starter projects to the user: `rename_project` mutates the name, and a
    # registry key a rename can move is not a key. Keying on it would let a
    # rename collide two rows into a DuplicateKeyError, and would make a
    # re-create under a since-renamed name mint a surprise duplicate.
    registry_key: str = ""
    # LEGACY blob-storage tarball pointer (an EEUploadService FileRecord id).
    # Written by the retired ``snapshot_project`` path; no longer produced. It is
    # read exactly ONCE more, by the migration in ``durability.restore_project``,
    # which expands the tar into per-file ``overlay`` entries and clears this back
    # to None so the baseline can never resurrect a deleted file again.
    snapshot_file_id: str | None = None
    # The AUTHORITATIVE per-file store, keyed on the PROJECT: ``relpath ->
    # FileRecord id``, one blob-storage object per file. Written by the editor
    # write-through hooks, by the browser file-sync route, and — for everything
    # that never passes a write hook (a clone, a scaffold, generated output) — by
    # ``durability.sync_project_files``, which enumerates the VM workspace and
    # DROPS entries whose paths no longer exist. Restore reconstructs the workspace
    # from this map alone. No new index — read only via the owner-scoped row.
    overlay: dict[str, str] = Field(default_factory=dict)
    # Whether ``overlay`` has been verified to be a COMPLETE image of the project's
    # workspace (every non-regenerable file), rather than a partial delta of the
    # files that happened to pass through a write hook. Set by
    # ``sync_project_files`` and by the legacy-tarball migration; False for a
    # project whose only writes came from the in-tab runtime, whose baseline (the
    # starter scaffold) is re-materialized client-side and never stored.
    #
    # It exists to gate DESTRUCTIVE reconciliation: restore prunes workspace files
    # missing from the overlay only when this is True, because "absent from the
    # overlay" means "deleted by the user" only once the overlay is known complete.
    overlay_complete: bool = False
    # The natural-language build prompt captured when a ``/code`` project is
    # created from a description — WHAT to build. Null for projects opened from an
    # existing repo with nothing to auto-build. The frontend reads it on first open
    # to auto-run one build turn.
    initial_prompt: str | None = None
    # Whether the auto-run build turn has been kicked off for ``initial_prompt``.
    # Set True on build-turn START (so a reopen doesn't re-run it); a retry-build
    # re-arms it to False. Independent of whether the build succeeded.
    initial_prompt_consumed: bool = False
    # The CURRENT ephemeral sandbox (a WebSandbox row id), or null when none is
    # live (never opened, or the VM was reaped). ``open_project`` resolves this.
    current_sandbox_id: str | None = None
    # When the user last opened this project (for "recent projects" ordering).
    last_opened_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "code_projects"
        indexes = [
            # The registry key. `registry_key` is "" for identity providers, so
            # this still reads as one-project-per-(workspace, user, provider,
            # repo) for git; scaffold rows each carry their own token and so
            # never collide with a sibling built from the same starter.
            IndexModel(
                [
                    ("workspace_id", 1),
                    ("user_id", 1),
                    ("provider", 1),
                    ("repo", 1),
                    ("registry_key", 1),
                ],
                unique=True,
                name="ws_user_provider_repo_key_unique",
            ),
            # The per-user list read path (list_projects).
            IndexModel([("workspace_id", 1), ("user_id", 1)]),
        ]
