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
