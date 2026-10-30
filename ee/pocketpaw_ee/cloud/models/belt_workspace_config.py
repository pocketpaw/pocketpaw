# ee/pocketpaw_ee/cloud/models/belt_workspace_config.py
# Created: 2026-06-10 (feat/belt-console-backend, SC-1) — Per-workspace Belt &
# Pulley console configuration. v1 ships ONE field — ``allowlist_roots``, the
# durable extension to ``settings.belt_repo_allowlist``. The /belt console's
# POST /belt/repos route appends a realpath-resolved git-repo root here (admin/
# owner-gated) so a workspace admin can authorize a new repo root WITHOUT a
# redeploy / env change. The repo-discovery + repo-resolution paths read the
# settings allowlist UNIONED with this list.
#
# Why a per-workspace Mongo doc (mirrors ForesightWorkspaceConfig): the additions
# must survive restarts (a settings-file edit would, but the console writes them
# at runtime), and the deployment topology is per-tenant-dedicated-server, so a
# workspace-keyed doc is the tenant-sane home. One row per workspace; the
# ``workspace`` key is indexed unique so the read/upsert path stays O(1). The
# shape is extension-additive — future console knobs (default base branch per
# repo, PR templates) layer on as new optional fields with safe defaults.
#
# Only ``ee.cloud.belt.service`` reads/writes this module (same single-importer
# discipline as ForesightWorkspaceConfig). The stored roots are RAW strings as
# the admin submitted them post-realpath; the resolution + containment check
# (the security boundary) runs at read time in the belt MCP resolver, never
# trusting a stored string blindly.

from __future__ import annotations

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class BeltWorkspaceConfig(TimestampedDocument):
    """Per-workspace Belt & Pulley console configuration overrides.

    Fields:
      - ``workspace`` — tenancy key. Indexed unique so ``find_one`` / upsert
        stays O(1).
      - ``allowlist_roots`` — durable extension to ``settings.belt_repo_allowlist``.
        Each entry is a realpath-resolved directory the console's add-repo route
        appended (admin/owner only). The repo-discovery + repo-resolution paths
        read ``settings.belt_repo_allowlist`` UNIONED with this list, so a root
        added here authorizes repos under it without a redeploy. Stored as the
        admin-submitted realpath string; the resolution + containment boundary is
        re-applied at read time, never trusting a stored string blindly.
      - ``createdAt`` / ``updatedAt`` — inherited from
        :class:`TimestampedDocument`.

    The shape is extension-additive; new optional fields with safe defaults won't
    break callers reading the v1 wire dict.
    """

    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    allowlist_roots: list[str] = Field(default_factory=list)

    class Settings:
        name = "belt_workspace_configs"
        # ``workspace`` already has a unique single-field index from the
        # ``Indexed(..., unique=True)`` annotation; the upsert path uses it
        # directly. No composite indexes needed in v1.
