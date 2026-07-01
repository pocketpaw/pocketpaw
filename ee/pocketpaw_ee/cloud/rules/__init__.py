# Cloud rules entity — re-exports for the gate executor + read seam.
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery). The discovered-rules
# entity: the persistence + read API for governed Instinct rules mined from
# workspace exhaust and approved through the gate. No HTTP router this slice
# (rules are born only via the S2-R3 ``_instinct_rule`` executor; see the
# ``no-router`` note in service.py). Re-exports the service entry points so the
# executor + the slice-3 dispatcher import from the package root.

from __future__ import annotations

from pocketpaw_ee.cloud.rules.domain import Rule
from pocketpaw_ee.cloud.rules.dto import CreateRuleRequest, RuleResponse
from pocketpaw_ee.cloud.rules.service import archive_rule, create_rule, get_active_rules

__all__ = [
    "CreateRuleRequest",
    "Rule",
    "RuleResponse",
    "archive_rule",
    "create_rule",
    "get_active_rules",
]
