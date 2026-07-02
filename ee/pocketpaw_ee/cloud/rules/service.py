# Rules — service (the sole owner of InstinctRuleDoc writes).
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery). The R3 ``_instinct_rule``
# gate executor calls ``create_rule`` at approve time; ``get_active_rules`` is the
# slice-2 read seam (slice-3 wires it into live gate dispatch). Follows the ee/cloud
# 4-file rules: module-level ``async def``, validate-at-entry, tenant filter on every
# read, emit on every write, errors via CloudError.
#
# no-router: rules have NO direct HTTP surface this slice. A rule is born ONLY by
#   approving an ``_instinct_rule`` gate proposal (S2-R3 executor → this service);
#   it is never POSTed by a client. The read seam ``get_active_rules`` is consumed
#   in-process by the (slice-3) gate dispatcher, not over HTTP. A read router can be
#   added later if a workspace rule-list endpoint is needed; omitted by design now.

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import RuleArchived, RuleCreated
from pocketpaw_ee.cloud.models.instinct_rule import InstinctRuleDoc
from pocketpaw_ee.cloud.rules.domain import Rule
from pocketpaw_ee.cloud.rules.dto import CreateRuleRequest, RuleResponse, RuleScopeResponse

# ---------------------------------------------------------------------------
# Private mapping helpers — Beanie doc ↔ domain ↔ wire dict
# ---------------------------------------------------------------------------


def _doc_to_domain(doc: InstinctRuleDoc) -> Rule:
    scope = doc.scope or {}
    return Rule(
        workspace_id=doc.workspace,
        id=str(doc.id),
        owner_user_id=doc.owner_user_id,
        name=doc.name,
        when=doc.when,
        action=doc.action,
        status=doc.status,
        scope_workspace_id=str(scope.get("workspace_id") or doc.workspace),
        scope_pocket_id=scope.get("pocket_id"),
        scope_object_type=scope.get("object_type"),
        confidence=doc.confidence,
        provenance=tuple(doc.provenance),
        description=doc.description,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


def _domain_to_response(rule: Rule) -> RuleResponse:
    return RuleResponse(
        id=rule.id,
        workspace_id=rule.workspace_id,
        owner_user_id=rule.owner_user_id,
        name=rule.name,
        description=rule.description,
        when=rule.when,
        action=rule.action,
        status=rule.status,
        scope=RuleScopeResponse(
            workspace_id=rule.scope_workspace_id,
            pocket_id=rule.scope_pocket_id,
            object_type=rule.scope_object_type,
        ),
        confidence=rule.confidence,
        provenance=list(rule.provenance),
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _wire_dict(doc: InstinctRuleDoc) -> dict:
    return _domain_to_response(_doc_to_domain(doc)).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_rule(workspace_id: str, user_id: str, body: CreateRuleRequest | dict) -> dict:
    """Persist an approved governed rule, returning its wire dict.

    Validates at entry (re-parses for internal callers), asserts the draft's
    ``scope.workspace_id`` matches the caller's ``workspace_id`` (tenancy can't
    be moved by an edited draft), writes the ``InstinctRuleDoc``, and emits
    ``RuleCreated``. Raises ``ValidationError`` (a CloudError) on a tenancy
    mismatch — never a silent cross-tenant write.
    """
    body = CreateRuleRequest.model_validate(body)
    draft = body.draft

    if draft.scope.workspace_id != workspace_id:
        raise ValidationError(
            "rule.workspace_mismatch",
            "rule scope workspace does not match the caller's workspace — "
            "a discovered rule cannot be persisted into another tenant",
        )

    doc = InstinctRuleDoc(
        workspace=workspace_id,
        owner_user_id=body.owner_user_id,
        status="active",
        name=draft.name,
        description=draft.description,
        when=draft.when,
        action=draft.action,
        scope=draft.scope.model_dump(),
        confidence=draft.confidence,
        provenance=list(draft.provenance),
    )
    await doc.insert()
    wire = _wire_dict(doc)
    await emit(RuleCreated(data=wire))
    return wire


async def get_active_rules(workspace_id: str) -> list[dict]:
    """Return the active governed rules for one workspace, as wire dicts.

    Tenant-filtered (``workspace == workspace_id``) and ``status == "active"``
    — archived rows are excluded. This is the seam the slice-3 gate dispatcher
    consults; slice-2 only exposes the read.
    """
    cursor = InstinctRuleDoc.find(
        InstinctRuleDoc.workspace == workspace_id,
        InstinctRuleDoc.status == "active",
    )
    return [_wire_dict(doc) async for doc in cursor]


async def archive_rule(workspace_id: str, user_id: str, rule_id: str) -> dict:
    """Flip a rule to ``archived`` (retire / supersede), returning its wire dict.

    Tenant-scoped lookup (``workspace == workspace_id``) so a caller can never
    archive another tenant's rule. Raises ``ValidationError`` if the id is not
    found in this workspace. Emits ``RuleArchived``.
    """
    doc = await InstinctRuleDoc.find_one(
        InstinctRuleDoc.id == _as_object_id(rule_id),
        InstinctRuleDoc.workspace == workspace_id,
    )
    if doc is None:
        raise ValidationError(
            "rule.not_found",
            f"rule {rule_id} not found in workspace {workspace_id}",
        )
    doc.status = "archived"
    await doc.save()
    wire = _wire_dict(doc)
    await emit(RuleArchived(data=wire))
    return wire


def _as_object_id(rule_id: str) -> Any:
    """Coerce a string id to a Beanie PydanticObjectId, surfacing a malformed
    id as a handled ValidationError rather than an unguarded ObjectId error."""
    from beanie import PydanticObjectId

    try:
        return PydanticObjectId(rule_id)
    except Exception as exc:  # noqa: BLE001 — a bad id is a validation failure
        raise ValidationError("rule.invalid_id", f"invalid rule id: {rule_id}") from exc


__all__ = ["archive_rule", "create_rule", "get_active_rules"]
