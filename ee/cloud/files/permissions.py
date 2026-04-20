"""RBAC + ABAC permission layer for the files module.

RBAC is provided per-entry by the owning provider via Permission(read, write, manage).
ABAC is a post-filter that can only further restrict visibility.

derive_capabilities() returns the final UI-facing capability list:
  read/download     <- rbac.read AND abac_allowed
  rename/move/replace/upload <- rbac.write AND mount_writable AND abac_allowed
  delete            <- rbac.manage AND mount_writable AND abac_allowed
Only capabilities the provider already declared on the entry survive
(so a provider can opt-out of a capability regardless of permission).
"""
from __future__ import annotations

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.schemas import (
    Capability,
    FileEntry,
    Permission,
    RequestContext,
)

_READ_CAPS: set[Capability] = {"read", "download"}
_WRITE_CAPS: set[Capability] = {"rename", "move", "replace", "upload"}
_MANAGE_CAPS: set[Capability] = {"delete"}


def apply_abac(
    entries: list[FileEntry],
    *,
    ctx: RequestContext,
    rules: AbacRuleSet,
) -> list[FileEntry]:
    return [
        e for e in entries if rules.allows(tags=e.tags, attributes=ctx.attributes)
    ]


def derive_capabilities(
    *,
    entry: FileEntry,
    rbac: Permission,
    mount_writable: bool,
    abac_allowed: bool,
) -> list[Capability]:
    if not abac_allowed:
        return []
    allowed: set[Capability] = set()
    if rbac.read:
        allowed |= _READ_CAPS
    if rbac.write and mount_writable:
        allowed |= _WRITE_CAPS
    if rbac.manage and mount_writable:
        allowed |= _MANAGE_CAPS
    return [c for c in entry.capabilities if c in allowed]


class PermissionsEvaluator:
    def __init__(self, rules: AbacRuleSet) -> None:
        self._rules = rules

    def filter(
        self, *, entries: list[FileEntry], ctx: RequestContext
    ) -> list[FileEntry]:
        return apply_abac(entries, ctx=ctx, rules=self._rules)
