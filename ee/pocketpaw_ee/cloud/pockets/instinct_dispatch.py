# ee/pocketpaw_ee/cloud/pockets/instinct_dispatch.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — single entry
# point from the runtime into the RFC 03 v2 template-level Instinct.
# Wraps the OSS-side ``resolve_instinct`` pure function with the EE-
# side persistence (``instinct_approvals.service``) and returns a
# typed ``InstinctGateResult`` the action_executor branches on.
#
# Why a separate module: ``action_executor`` is import-linter-pure
# (no Beanie / no models). This wrapper is the impure layer that may
# call ``instinct_approvals.service.create_approval`` (a Beanie writer
# under the import-linter contract for that entity).
#
# Single entry point: future bulk fan-out (Wave 3b) + temporal sweeper
# (Wave 3d) call ``gate_action`` per-row too, so all dispatch flows
# share the same persistence + audit shape.
#
# Hard constraint: this module DOES NOT touch the M2b.1 binding-level
# ``requires_instinct`` flag / ``_park`` sentinel. That flow stays
# intact for backward compatibility — Wave 3a layers a NEW
# template-level gate that runs BEFORE the M2b.1 gate.

"""Dispatch wrapper around OSS ``resolve_instinct`` + EE persistence."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pocketpaw.bundled_templates import (
    InstinctDecision,
    InstinctResolutionError,
    InstinctRule,
    PocketTemplate,
    resolve_instinct,
)
from pocketpaw.bundled_templates.identifier_resolver import IdentifierResolver
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service

logger = logging.getLogger(__name__)

NextStepT = Literal["proceed", "blocked", "pending_approval"]


class InstinctGateResult(BaseModel):
    """Outcome of a single ``gate_action`` call.

    Frozen so the executor cannot mutate it. The four-fielded shape
    matches what the runtime needs to dispatch:

    * ``decision`` — the pure OSS composer's verdict + audit data.
    * ``next_step`` — collapsed branch for the executor (proceed /
      blocked / pending_approval).
    * ``approval_id`` — set only when ``next_step == "pending_approval"``;
      the action_executor returns it on the ``instinct_pending``
      sentinel response.
    * ``notify_rules`` — top-level ``notify`` rules whose ``when``
      matched the row. Carried through for the future notify side-
      effect dispatcher (Wave 3c). Empty on BLOCK.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    decision: InstinctDecision
    next_step: NextStepT
    approval_id: str | None = None
    notify_rules: list[InstinctRule] = []


def _matched_rules_payload(decision: InstinctDecision) -> list[dict[str, Any]]:
    """Serialize matched rules to plain dicts so they can ride a
    Beanie ``list[dict]`` field. ``InstinctRule.model_dump`` is the
    canonical serializer."""
    return [r.model_dump() for r in decision.matched_rules]


async def gate_action(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    template: PocketTemplate,
    action_name: str,
    row_context: dict[str, Any],
    workspace_context: dict[str, Any] | None = None,
    row_id: str = "",
    park: dict[str, Any] | None = None,
    resolver: IdentifierResolver | None = None,
    now: datetime | None = None,
) -> InstinctGateResult:
    """Resolve the template-level Instinct verdict for one action+row.

    Calls the pure OSS composer (``resolve_instinct``) first, then
    branches:

    * ``BLOCK`` → persist nothing. Return ``next_step="blocked"``.
    * ``ESCALATE_APPROVAL`` → persist one ``InstinctApproval`` row via
      ``instinct_approvals.service.create_approval``. Return
      ``next_step="pending_approval"`` with the new approval id.
    * ``EXECUTE`` → no persistence. Return ``next_step="proceed"``.
    * ``NOTIFY_AND_EXECUTE`` → no persistence. Return
      ``next_step="proceed"`` with ``notify_rules`` populated for the
      side-effect dispatcher.

    Errors:
        ``InstinctResolutionError`` from the composer (unknown action
        on the template, or a CEL eval failure on a rule) is mapped to
        ``NotFound("instinct_action", action_name)``. The brief locks
        ``NotFound`` for unknown action; an eval failure is structurally
        the same shape (an undeclared identifier or a malformed rule is
        a programming/authoring error, not a permissions issue).
    """
    try:
        decision = resolve_instinct(
            template,
            action_name,
            row_context,
            workspace_context,
            resolver=resolver,
            now=now,
        )
    except InstinctResolutionError as exc:
        # The runtime cannot dispatch an action the template does not
        # declare, and it cannot continue past a rule that does not
        # evaluate cleanly. Either is a 404 on "the action the caller
        # asked for is not (currently) runnable" — never echo the
        # internal exception text to the wire.
        logger.warning(
            "instinct gate failed for action=%s pocket=%s: %s",
            action_name,
            pocket_id,
            exc,
        )
        raise NotFound("instinct_action", action_name) from exc

    if decision.verdict == "BLOCK":
        return InstinctGateResult(
            decision=decision,
            next_step="blocked",
            approval_id=None,
            notify_rules=[],
        )

    if decision.verdict == "ESCALATE_APPROVAL":
        # Persist + tag the approval id back onto the result. The
        # executor returns this on the ``instinct_pending`` sentinel
        # so the caller (and the eventual approver UI) can address
        # the row.
        body = {
            "pocket_id": pocket_id,
            "action_name": action_name,
            "row_id": row_id,
            "row_data": row_context,
            "verdict": decision.verdict,
            "reason": decision.reason,
            "matched_rules": _matched_rules_payload(decision),
            "park": park,
        }
        wire = await approvals_service.create_approval(workspace_id, user_id, body)
        return InstinctGateResult(
            decision=decision,
            next_step="pending_approval",
            approval_id=wire["id"],
            notify_rules=list(decision.notify_rules),
        )

    # EXECUTE / NOTIFY_AND_EXECUTE — proceed. Notify rules carry
    # through (top-level notify can fire alongside an auto execute).
    return InstinctGateResult(
        decision=decision,
        next_step="proceed",
        approval_id=None,
        notify_rules=list(decision.notify_rules),
    )


__all__ = ["InstinctGateResult", "NextStepT", "gate_action"]
