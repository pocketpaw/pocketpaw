# Cloud rules entity — re-exports for the gate executor, read seam, and HTTP router.
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery). The discovered-rules
# entity: the persistence + read API for governed Instinct rules mined from
# workspace exhaust and approved through the gate.
#
# Updated: 2026-07-09 (feat/instinct-guardrail-rules). A UI-authored governed rule
# is now manageable over HTTP: the entity gains a thin ``router`` (create / list /
# archive + the per-workspace enforcement toggle) over the shipped service, plus the
# enforcement DTOs. The original "no HTTP router this slice" note is retired.
# Re-exports the service entry points + DTOs so consumers import from the package root.

from __future__ import annotations

from pocketpaw_ee.cloud.rules.domain import Rule
from pocketpaw_ee.cloud.rules.dto import (
    CreateRuleRequest,
    EnforcementResponse,
    RuleResponse,
    SetEnforcementRequest,
)
from pocketpaw_ee.cloud.rules.service import (
    archive_rule,
    create_rule,
    get_active_rules,
    get_enforcement,
    get_enforcement_override,
    set_enforcement,
)

__all__ = [
    "CreateRuleRequest",
    "EnforcementResponse",
    "Rule",
    "RuleResponse",
    "SetEnforcementRequest",
    "archive_rule",
    "create_rule",
    "get_active_rules",
    "get_enforcement",
    "get_enforcement_override",
    "set_enforcement",
]
