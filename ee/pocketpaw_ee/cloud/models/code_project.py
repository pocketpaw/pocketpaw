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
    # Durable blob-storage snapshot pointer (an EEUploadService FileRecord id).
    # The project's files live HERE between sandboxes — this is why the project
    # survives VM reaping. Null until the first backup.
    snapshot_file_id: str | None = None
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
