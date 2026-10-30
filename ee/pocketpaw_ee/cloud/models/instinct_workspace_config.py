# ee/pocketpaw_ee/cloud/models/instinct_workspace_config.py
# Created: 2026-07-09 (feat/instinct-guardrail-rules) — Per-workspace Instinct
# enforcement configuration. v1 ships ONE field — ``enforce_discovered_rules``,
# a TRI-STATE override on the global ``settings.instinct_enforce_discovered_rules``
# flag so a workspace admin can turn authored-rule enforcement ON (or OFF) for a
# SINGLE tenant without a code change / redeploy:
#   - ``True``  → enforcement ON for this workspace regardless of the global flag.
#   - ``False`` → enforcement OFF for this workspace regardless of the global flag.
#   - ``None``  → no override; inherit the global settings flag.
# The live gate (``ee.cloud.pockets.instinct_dispatch``) resolves the effective
# value as ``override if override is not None else global_flag``.
#
# Why a per-workspace Mongo doc (mirrors ForesightWorkspaceConfig / BeltWorkspaceConfig):
# the toggle must survive restarts and be settable at runtime by an admin, and the
# deployment topology is per-tenant-dedicated-server, so a workspace-keyed doc is the
# tenant-sane home. One row per workspace; ``workspace`` is indexed unique so the
# read/upsert path stays O(1). The shape is extension-additive — future instinct
# knobs layer on as new optional fields with safe defaults.
#
# Only ``ee.cloud.rules.service`` reads/writes this module (same single-importer
# discipline as ForesightWorkspaceConfig, enforced by the "Rules" import-linter
# contract in ee/pyproject.toml).

from __future__ import annotations

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class InstinctWorkspaceConfig(TimestampedDocument):
    """Per-workspace Instinct enforcement configuration override.

    Fields:
      - ``workspace`` — tenancy key. Indexed unique so ``find_one`` / upsert
        stays O(1).
      - ``enforce_discovered_rules`` — TRI-STATE override on the global
        ``settings.instinct_enforce_discovered_rules`` flag. ``True`` forces
        authored-rule enforcement ON for this workspace, ``False`` forces it
        OFF, and ``None`` (the default) means "no override — inherit the global
        flag". Stored as ``bool | None`` exactly like
        ``ForesightWorkspaceConfig.threshold_override`` models a per-workspace
        inherit-vs-override knob.
      - ``createdAt`` / ``updatedAt`` — inherited from
        :class:`TimestampedDocument`. ``updatedAt`` doubles as the
        "when did the admin last touch this" timestamp the GET response exposes.

    The shape is extension-additive; new optional fields with safe defaults
    won't break callers reading the v1 wire dict.
    """

    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    enforce_discovered_rules: bool | None = Field(default=None)

    class Settings:
        name = "instinct_workspace_configs"
        # ``workspace`` already has a unique single-field index from the
        # ``Indexed(..., unique=True)`` annotation; the upsert path uses it
        # directly. No composite indexes needed in v1.
