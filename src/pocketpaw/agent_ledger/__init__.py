# Agent ledger — the agent-keyed record of what an agent actually achieved.
# Updated: 2026-08-01 (AL-2, paw-bar emitters) — re-exports the three new
#   `paw.*` attribute names (widget id, visitor verb, handoff source,
#   product id) so the
#   paw-bar emitters keep importing every ledger name from ONE module.
# Created: 2026-07-31 (AL-1, ledger spine) — the public surface of the module.
#   Re-exports the row model, the closed `paw.*` kind vocabulary, the window
#   helpers, and the store, so every consumer (the Instinct emitter, the paw-bar
#   emitters in AL-2, the metering sweeper in AL-3, the read endpoints) imports
#   from ONE place and the internal file split stays free to move.
#
# The store handle itself is NOT constructed here — use
# ``pocketpaw.stores.get_agent_ledger_store(workspace_id=...)`` so the
# per-workspace file routing, the fail-closed cloud guard, and the bounded
# handle cache all apply. Constructing AgentLedgerStore directly is for tests.

from __future__ import annotations

from pocketpaw.agent_ledger.models import (
    ATTR_ACTION_CATEGORY,
    ATTR_ACTION_ID,
    ATTR_AGENT_ID,
    ATTR_AGENT_NAME,
    ATTR_APPROVAL_AUTO,
    ATTR_CART_CURRENCY,
    ATTR_CART_VALUE_CENTS,
    ATTR_CONVERSATION_ID,
    ATTR_DECISION_ACTOR,
    ATTR_HANDOFF_SOURCE,
    ATTR_INSTINCT_EVENT,
    ATTR_OPERATION_NAME,
    ATTR_POCKET_ID,
    ATTR_PRODUCT_ID,
    ATTR_SCOPE_TYPE,
    ATTR_VISITOR_VERB,
    ATTR_WIDGET_ID,
    CORE_KINDS,
    KIND_ACTION_APPROVED,
    KIND_ACTION_DELIVERED,
    KIND_ACTION_OUTCOME,
    KIND_ACTION_PROPOSED,
    KIND_ACTION_REJECTED,
    KIND_CONVERSATION_STARTED,
    KIND_CONVERSATION_TAKEOVER,
    KIND_HANDOFF_RAISED,
    KIND_HANDOFF_RESOLVED,
    KIND_RUN_COMPLETED,
    KIND_VISITOR_ACTION,
    LEDGER_VOCAB_VERSION,
    SURFACE_BELT,
    SURFACE_CHAT,
    SURFACE_DEEP_WORK,
    SURFACE_GROWTH,
    SURFACE_INSTINCT,
    SURFACE_PAW_BAR,
    CoreKind,
    LedgerActor,
    LedgerKindValidationError,
    LedgerRow,
    WindowParseError,
    is_core_kind,
    parse_window,
    surface_from_trigger,
    validate_ledger_kind,
    window_start,
)
from pocketpaw.agent_ledger.store import MAX_QUERY_LIMIT, AgentLedgerStore

__all__ = [
    "ATTR_ACTION_CATEGORY",
    "ATTR_ACTION_ID",
    "ATTR_AGENT_ID",
    "ATTR_AGENT_NAME",
    "ATTR_APPROVAL_AUTO",
    "ATTR_CONVERSATION_ID",
    "ATTR_DECISION_ACTOR",
    "ATTR_HANDOFF_SOURCE",
    "ATTR_INSTINCT_EVENT",
    "ATTR_OPERATION_NAME",
    "ATTR_POCKET_ID",
    "ATTR_CART_CURRENCY",
    "ATTR_CART_VALUE_CENTS",
    "ATTR_PRODUCT_ID",
    "ATTR_SCOPE_TYPE",
    "ATTR_VISITOR_VERB",
    "ATTR_WIDGET_ID",
    "CORE_KINDS",
    "KIND_ACTION_APPROVED",
    "KIND_ACTION_DELIVERED",
    "KIND_ACTION_OUTCOME",
    "KIND_ACTION_PROPOSED",
    "KIND_ACTION_REJECTED",
    "KIND_CONVERSATION_STARTED",
    "KIND_CONVERSATION_TAKEOVER",
    "KIND_HANDOFF_RAISED",
    "KIND_HANDOFF_RESOLVED",
    "KIND_RUN_COMPLETED",
    "KIND_VISITOR_ACTION",
    "LEDGER_VOCAB_VERSION",
    "MAX_QUERY_LIMIT",
    "SURFACE_BELT",
    "SURFACE_CHAT",
    "SURFACE_DEEP_WORK",
    "SURFACE_GROWTH",
    "SURFACE_INSTINCT",
    "SURFACE_PAW_BAR",
    "AgentLedgerStore",
    "CoreKind",
    "LedgerActor",
    "LedgerKindValidationError",
    "LedgerRow",
    "WindowParseError",
    "is_core_kind",
    "parse_window",
    "surface_from_trigger",
    "validate_ledger_kind",
    "window_start",
]
