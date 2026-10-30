# Rules — service (the sole owner of InstinctRuleDoc + InstinctWorkspaceConfig writes).
# Created: 2026-06-20 (S2-R1 / feat/szd-slice2-discovery). The R3 ``_instinct_rule``
# gate executor calls ``create_rule`` at approve time; ``get_active_rules`` is the
# slice-2 read seam (slice-3 wires it into live gate dispatch). Follows the ee/cloud
# 4-file rules: module-level ``async def``, validate-at-entry, tenant filter on every
# read, emit on every write, errors via CloudError.
#
# Updated: 2026-07-09 (feat/instinct-guardrail-rules). The rules entity now HAS a thin
# HTTP router (``rules/router.py``) over ``create_rule`` / ``get_active_rules`` /
# ``archive_rule`` so a UI-authored governed rule is manageable over HTTP (the
# original "no-router" note is retired). This module also gains the per-workspace
# ENFORCEMENT toggle: ``get_enforcement_override`` (the fail-open resolver seam the
# live gate consults), ``get_enforcement`` (read) and ``set_enforcement`` (upsert)
# over the ``InstinctWorkspaceConfig`` doc — a tri-state override on the global
# ``instinct_enforce_discovered_rules`` flag so one tenant can flip enforcement
# without a code change. The gate's fail-OPEN-on-read-error rail is unchanged.

from __future__ import annotations

from typing import Any

from pocketpaw.config import get_settings
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import RuleArchived, RuleCreated
from pocketpaw_ee.cloud.models.instinct_rule import InstinctRuleDoc
from pocketpaw_ee.cloud.models.instinct_workspace_config import InstinctWorkspaceConfig
from pocketpaw_ee.cloud.rules.domain import Rule
from pocketpaw_ee.cloud.rules.dto import (
    CreateRuleRequest,
    EnforcementResponse,
    RuleResponse,
    RuleScopeResponse,
)

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


# ---------------------------------------------------------------------------
# Per-workspace enforcement toggle — the tri-state override on the global
# ``instinct_enforce_discovered_rules`` flag, over ``InstinctWorkspaceConfig``.
# ---------------------------------------------------------------------------


async def get_enforcement_override(workspace_id: str) -> bool | None:
    """Return the workspace's raw enforcement override, or ``None`` if unset.

    ``True``/``False`` is an explicit per-workspace override; ``None`` means the
    workspace has no override and inherits the global flag. This is the pure read
    seam the live gate consults — it RAISES on a store read error so the CALLER
    owns the fail-OPEN decision (the gate must never turn a read hiccup into a
    block; see ``instinct_dispatch._enforcement_enabled``).
    """
    doc = await InstinctWorkspaceConfig.find_one(
        InstinctWorkspaceConfig.workspace == workspace_id,
    )
    return None if doc is None else doc.enforce_discovered_rules


async def get_enforcement(workspace_id: str) -> dict:
    """Return the workspace's effective enforcement state as a wire dict.

    ``enforce_discovered_rules`` is the EFFECTIVE value (``override`` when set,
    else the global flag); ``override`` is the raw tri-state; ``global_default``
    is the current global flag. Read-only — never writes a doc.
    """
    override = await get_enforcement_override(workspace_id)
    global_default = bool(get_settings().instinct_enforce_discovered_rules)
    return EnforcementResponse(
        workspace_id=workspace_id,
        enforce_discovered_rules=override if override is not None else global_default,
        override=override,
        global_default=global_default,
    ).model_dump(mode="json")


async def set_enforcement(workspace_id: str, user_id: str, enabled: bool | None) -> dict:
    """Upsert the workspace's enforcement override, returning the effective state.

    ``enabled=True`` forces enforcement ON, ``False`` forces it OFF, and ``None``
    clears the override (the workspace re-inherits the global flag). The doc
    itself survives a reset — a "previously overridden, now cleared" workspace
    keeps an audit-relevant ``updatedAt``. ``workspace`` is unique-indexed, so
    the find-then-insert/save upsert is O(1); a concurrent-insert race would
    surface as a DuplicateKeyError (admin-only write path — treated as a 5xx, no
    retry loop, mirroring ``foresight.service.set_threshold``).
    """
    doc = await InstinctWorkspaceConfig.find_one(
        InstinctWorkspaceConfig.workspace == workspace_id,
    )
    if doc is None:
        doc = InstinctWorkspaceConfig(
            workspace=workspace_id,
            enforce_discovered_rules=enabled,
        )
        await doc.insert()
    else:
        doc.enforce_discovered_rules = enabled
        await doc.save()

    return await get_enforcement(workspace_id)


__all__ = [
    "archive_rule",
    "create_rule",
    "get_active_rules",
    "get_enforcement",
    "get_enforcement_override",
    "set_enforcement",
]
