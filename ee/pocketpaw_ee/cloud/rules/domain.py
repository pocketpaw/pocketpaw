# Rules — domain value objects.
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery). Frozen value objects
# constructed in service.py from an InstinctRuleDoc (read path) or a validated
# RuleDraft (create path). Per the ee/cloud rule §3, tenancy (``workspace_id``)
# is required at construction — there is no default, so a Rule can never be built
# without a workspace. Consumers outside the service only ever see ``Rule``.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Rule:
    """One approved, workspace-scoped governed rule.

    Required tenancy first (``workspace_id`` has no default). The ``when`` /
    ``action`` pair is enforcement-ready (CEL + the template action literal).
    ``scope_*`` flatten the rule's RuleScope; ``scope_workspace_id`` always
    equals ``workspace_id`` (the create path asserts the match before
    construction). Built from a validated ``RuleDraft`` plus the governance
    fields (owner / status) that live OUTSIDE the editable draft.
    """

    workspace_id: str
    id: str
    owner_user_id: str
    name: str
    when: str
    action: str
    status: str  # "active" | "archived"
    scope_workspace_id: str
    scope_pocket_id: str | None
    scope_object_type: str | None
    confidence: float
    provenance: tuple[str, ...] = field(default_factory=tuple)
    description: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["Rule"]
