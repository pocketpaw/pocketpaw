# Rules — request / response schemas.
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery). Per cloud rule §4 the
# request schema (``CreateRuleRequest``) is distinct from the response schema
# (``RuleResponse``) — never one model for both, so fields can't leak silently.
#
# ``CreateRuleRequest`` is what the R3 ``_instinct_rule`` executor hands to
# ``rules.service.create_rule``: the editable ``RuleDraft`` plus ``owner_user_id``
# (the approver). Tenancy is NOT in this DTO — it arrives as the service's explicit
# ``workspace_id`` parameter, and the service asserts it matches ``draft.scope``.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from pocketpaw_ee.discovery.rule_models import RuleDraft

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateRuleRequest(BaseModel):
    """Body for ``rules.service.create_rule``.

    Carries the editable ``RuleDraft`` (the ``rule_spec`` content) plus the
    ``owner_user_id`` of the approver. Tenancy is the service's ``workspace_id``
    parameter, not a field here — keeping it out of the request means an edited
    draft can never move the rule to another workspace.
    """

    model_config = ConfigDict(extra="forbid")

    draft: RuleDraft
    owner_user_id: str


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class RuleScopeResponse(BaseModel):
    workspace_id: str
    pocket_id: str | None = None
    object_type: str | None = None


class RuleResponse(BaseModel):
    """Wire shape for one governed rule.

    Flattens the persisted doc into the JSON the review surface + the
    ``get_active_rules`` consumers read. ``scope`` nests the rule's RuleScope.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    owner_user_id: str
    name: str
    description: str | None = None
    when: str
    action: str
    status: str
    scope: RuleScopeResponse
    confidence: float
    provenance: list[str]
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["CreateRuleRequest", "RuleResponse", "RuleScopeResponse"]
