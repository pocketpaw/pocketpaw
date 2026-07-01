# Instinct — decision pipeline for Paw OS.
# Created: 2026-03-28 — Actions, approvals, audit log.
# Updated: 2026-06-23 (feat/svl-1-verify-stamp) — SVL-1: exported the
#   VerdictProvider seam — the VerdictProvider protocol and the shipped
#   DeterministicVerdictProvider — so the deep_work executor can stamp an
#   outcome verdict behind a flag without hard-wiring a concrete verifier.
# Updated: 2026-05-21 (feat/instinct-outcome-verification) — issue #1162:
#   exported the outcome-verification primitives — OutcomeStatus,
#   CriterionResult, OutcomeVerdict, and the deterministic verify_outcome /
#   check_criterion functions.
# Updated: 2026-03-30 — Exported ActionStatus, ActionCategory, ActionPriority, AuditCategory.
# Updated: 2026-04-12 (Move 1 PR-A) — Exported Correction, CorrectionPatch, compute_patches.
# The decision loop: Agent proposes -> Human approves (optionally edits) ->
# Action executes -> outcome verified against success criteria -> Correction
# captured -> Soul learns.

from pocketpaw.instinct.correction import (
    Correction,
    CorrectionPatch,
    compute_patches,
    summarize_correction,
)
from pocketpaw.instinct.models import (
    Action,
    ActionCategory,
    ActionContext,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditCategory,
    AuditEntry,
    CriterionResult,
    OutcomeStatus,
    OutcomeVerdict,
)
from pocketpaw.instinct.store import InstinctStore
from pocketpaw.instinct.trace import FabricObjectSnapshot, ReasoningTrace, ToolCallRef
from pocketpaw.instinct.trace_collector import TraceCollector
from pocketpaw.instinct.verdict_provider import (
    DeterministicVerdictProvider,
    VerdictProvider,
)
from pocketpaw.instinct.verification import check_criterion, verify_outcome

__all__ = [
    "Action",
    "ActionCategory",
    "ActionContext",
    "ActionPriority",
    "ActionStatus",
    "ActionTrigger",
    "AuditCategory",
    "AuditEntry",
    "Correction",
    "CorrectionPatch",
    "CriterionResult",
    "DeterministicVerdictProvider",
    "FabricObjectSnapshot",
    "InstinctStore",
    "OutcomeStatus",
    "OutcomeVerdict",
    "ReasoningTrace",
    "ToolCallRef",
    "TraceCollector",
    "VerdictProvider",
    "check_criterion",
    "compute_patches",
    "summarize_correction",
    "verify_outcome",
]
