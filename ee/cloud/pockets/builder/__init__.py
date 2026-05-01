# Pockets builder — public re-exports.
#
# Created 2026-05-01.  Single import surface for callers (the SSE handler
# and tests).  Importing from sub-modules directly is forbidden by the
# ``pockets-builder-no-internal-leak`` importlinter contract.

from __future__ import annotations

from ee.cloud.pockets.builder.domain import (
    BuilderEvent,
    BuilderResult,
    IntentKind,
    PocketSpec,
    PocketUpdatePatch,
    WidgetSpec,
)
from ee.cloud.pockets.builder.dto import BuildRequest, BuildResponse
from ee.cloud.pockets.builder.service import (
    build_pocket_spec,
    build_update_patch,
    detect_intent,
    run_intent_from_message,
)

__all__ = [
    "BuildRequest",
    "BuildResponse",
    "BuilderEvent",
    "BuilderResult",
    "IntentKind",
    "PocketSpec",
    "PocketUpdatePatch",
    "WidgetSpec",
    "build_pocket_spec",
    "build_update_patch",
    "detect_intent",
    "run_intent_from_message",
]
