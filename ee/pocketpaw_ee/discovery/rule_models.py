# pocketpaw_ee/discovery/rule_models.py — the RuleDraft shape produced by rules-discovery.
#
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery) — the pure-data contract
# for sovereign zero-setup RULES discovery. A ``RuleDraft`` is the digester output
# AND the editable ``rule_spec`` blob carried through the Instinct gate:
#
#   - ``when``    — a CEL expression (reuses ``bundled_templates.CelExpression``;
#                   parses at validation, never evaluates here).
#   - ``action``  — the gate disposition, reusing the template literal
#                   ``InstinctRuleActionT`` = require_approval | notify | block.
#   - ``scope``   — workspace_id (required tenancy) + optional pocket_id / object_type.
#   - ``confidence`` — clamped into [0, 1] via the shared ``discovery.models._clamp``
#                   (silent clamp — a noisy inference score never fails validation).
#   - ``provenance`` — the audit-row / correction / record ids the rule was inferred
#                   from, so a reviewer can trace WHY the rule was proposed.
#
# ``RuleDraft.model_validate(blob)`` round-trips from a plain dict (the stored
# ``rule_spec``) so the R3 executor can re-validate at the gate chokepoint. The
# draft is deliberately FREE of approver identity / top-level tenancy: the R3 blob
# carries workspace_id / owner as SEPARATE top-level fields so a tenant editing the
# draft cannot move the rule to another workspace. Pure data, no I/O — depends only
# on pydantic + the OSS CEL field + the discovery clamp helper.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pocketpaw.bundled_templates.expressions import CelExpression
from pocketpaw.bundled_templates.schema import InstinctRuleActionT
from pocketpaw_ee.discovery.models import _clamp


class RuleScope(BaseModel):
    """Where a discovered rule applies.

    ``workspace_id`` is required tenancy (no default) — it is the only
    mandatory field. ``pocket_id`` narrows the rule to a single pocket and
    ``object_type`` to a single Fabric object type; both ``None`` means the
    rule is workspace-wide.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    pocket_id: str | None = None
    object_type: str | None = None


class RuleDraft(BaseModel):
    """A candidate governed rule reverse-engineered from workspace exhaust.

    The digester emits these; the review surface edits them; the gate
    persists an approved one as an ``InstinctRuleDoc``. The shape is
    enforcement-ready: ``when`` (CEL) + ``action`` map straight onto the
    template ``InstinctRule`` the runtime composer already evaluates.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    # CEL trigger — parses at validation (malformed → ValidationError), never
    # evaluated here; the runtime binds a real row/event context later.
    when: CelExpression
    # Gate disposition, reusing the template literal so an approved draft is
    # ``InstinctRule``-compatible.
    action: InstinctRuleActionT
    scope: RuleScope
    # Inference confidence in [0, 1]; clamped (never raised) so a noisy score
    # from the rule digester degrades gracefully.
    confidence: float = 0.0
    # Ids of the audit rows / corrections / records the rule was inferred from.
    provenance: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


__all__ = ["RuleDraft", "RuleScope"]
