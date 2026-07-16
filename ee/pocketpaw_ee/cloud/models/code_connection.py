# code_connection.py — CodeConnection document: a user's Code Mode GitHub App
# install binding.
# Created 2026-07-16 (CM-3, feat/code-mode): persists
# ``(workspace_id, user_id, provider, installation_id)`` so a user who installed
# the Code Mode GitHub App can list + open their PRIVATE repos. One row per
# installation — a user may install the App on several accounts/orgs, each a
# distinct installation id. The ``installation_id`` is the least-privilege pointer
# the broker mints repo-scoped tokens from (githubapp.py); it is NEVER a token.
#
# Only ``ee.cloud.codeconnect.service`` imports this doc class directly
# (import-linter "CodeConnection" contract, same discipline as WebSandbox).

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class CodeConnection(Document):
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    user_id: Indexed(str)  # type: ignore[valid-type]
    # The auth provider this connection is for (github first; google + others
    # planned — the picker + broker are provider-agnostic).
    provider: str = "github"
    # The GitHub App installation id — the least-privilege pointer the broker mints
    # repo-scoped tokens from. A pointer, never a token.
    installation_id: str
    # The GitHub account (user/org) the App was installed on, for display in the
    # picker. Optional — enriched from GitHub lazily; None until then.
    account_login: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "code_connections"
        indexes = [
            # One row per (workspace, user, provider, installation) — the key.
            IndexModel(
                [
                    ("workspace_id", 1),
                    ("user_id", 1),
                    ("provider", 1),
                    ("installation_id", 1),
                ],
                unique=True,
                name="ws_user_provider_installation_unique",
            ),
            # The per-user list read path (list_connections).
            IndexModel([("workspace_id", 1), ("user_id", 1)]),
        ]
