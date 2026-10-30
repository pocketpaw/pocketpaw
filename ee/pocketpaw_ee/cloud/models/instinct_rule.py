# InstinctRuleDoc Beanie document — a persisted, approved, workspace-scoped, OWNED rule.
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery) — the persistence home for
# a rule discovered from workspace exhaust and approved through the Instinct gate.
#
# It carries the editable ``RuleDraft`` fields (name / description / when / action /
# scope / confidence / provenance) PLUS the non-editable governance fields kept
# SEPARATE from the draft: ``workspace`` (indexed tenancy), ``owner_user_id`` (who
# approved it), and ``status`` (active | archived). Tenancy/owner live here as
# top-level columns — NOT nested in any editable ``rule_spec`` — so a tenant editing
# the rule can never move it to another workspace.
#
# Only ``ee.cloud.rules.service`` imports this doc (import-linter "Rules" contract).
# The ``scope`` is stored as a plain JSON sub-dict; the service re-validates it into
# a ``RuleScope`` on the way in and projects it back out on read.

from __future__ import annotations

from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class InstinctRuleDoc(TimestampedDocument):
    """One approved, workspace-scoped governed rule.

    Tenancy: ``workspace`` is required and indexed; every read in
    ``rules/service.py`` filters on it. ``status`` is ``"active"`` on create
    and flips to ``"archived"`` when superseded or retired — the active read
    excludes archived rows. The CEL ``when`` + ``action`` literal make the
    persisted rule ``InstinctRule``-compatible for the (slice-3) enforcement
    seam; this slice only makes it discoverable, governed, and readable via
    ``get_active_rules``.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    owner_user_id: str
    status: Literal["active", "archived"] = "active"

    # --- editable RuleDraft fields (the rule_spec content) ---
    name: str
    description: str | None = None
    when: str  # CEL expression text (validated on the draft before persist)
    action: str  # "require_approval" | "notify" | "block"
    # Stored flat as JSON; the service maps it to/from a RuleScope value object.
    scope: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    provenance: list[str] = Field(default_factory=list)

    class Settings(TimestampedDocument.Settings):
        name = "instinct_rules"
