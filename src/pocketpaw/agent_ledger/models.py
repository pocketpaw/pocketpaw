# Agent ledger models — the row shape + the closed `paw.*` kind vocabulary.
# Created: 2026-07-31 (AL-1, ledger spine) — "what did my agents do for me?" was
#   un-askable because every surface recorded its own fragment in its own store
#   with its own key, and half of them carried no agent_id at all. This module
#   defines the ONE agent-keyed row every surface writes, and the vocabulary of
#   things worth recording.
#
# Two decisions worth the reader's time:
#
#   * GOVERNANCE MIRRORS SENSES (src/pocketpaw/senses/vocabulary.py). The
#     ``paw.*`` core is CLOSED — an unknown ``paw.*`` kind raises rather than
#     silently landing — because an analytics vocabulary that anyone can extend
#     in the core namespace stops being comparable across surfaces within a
#     release. The ``vendor.*`` extension space is OPEN and validated only for
#     shape, so a third-party surface can record its own beats without a PR
#     against core.
#
#   * ``attrs`` USES OTEL GenAI SEMANTIC-CONVENTION NAMES where one exists
#     (``gen_ai.agent.id``, not ``agent_id``). The ops half of agent
#     observability standardized on OTel during 2025-26; the VALUE half — what
#     an agent achieved for its owner — did not, which is why this store exists
#     at all. Borrowing their attribute names now means a later OTel exporter is
#     an adapter, not a rewrite. Paw concepts with no OTel equivalent take a
#     ``paw.*`` attribute name, deliberately distinguishable from the borrowed
#     ones.
#
# What must NEVER appear on a row: tokens, cost, latency, model mix, traces.
# Those stay federated in usage_tracker / ChatRunDoc / the inference gateway and
# are read where they already live. Copying them here re-creates the
# chart-vs-wallet two-meters bug (a usage chart read one meter while the wallet
# held the spend, and they disagreed) — the row model has no field for them on
# purpose, so the mistake cannot be made quietly.

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Bump when the core vocabulary changes. Unlike sense ids, ledger kinds are NOT
# individually versioned: a kind names a beat in the agent's life ("an action was
# approved"), and versioning that per-kind would fragment the very comparability
# the closed core exists to protect. The catalog as a whole carries the version.
LEDGER_VOCAB_VERSION = "1"

# Core kind ids follow paw.<domain>.<verb>. Extension (vendor) kinds follow
# <vendor>.<domain>.<verb> and are accepted freely — the core is closed, the
# extension space is open. Same split as senses/vocabulary.py.
_CORE_KIND_PATTERN = re.compile(r"^paw\.[a-z0-9_]+\.[a-z0-9_]+$")
_EXTENSION_KIND_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")


class LedgerKindValidationError(ValueError):
    """Raised when a ledger kind is malformed or an unknown ``paw.*`` core id."""


@dataclass(frozen=True)
class CoreKind:
    """A single curated core kind in the versioned vocabulary."""

    id: str
    display_name: str
    description: str


# ---------------------------------------------------------------------------
# The v1 kind vocabulary
# ---------------------------------------------------------------------------
#
# Every kind here has a producer in the codebase today — this is not a wish list.
# AL-1 emits the three action-lifecycle kinds; the rest are defined now so the
# vocabulary is settled before five emitters race to invent names for the same
# beat, which is exactly how a "closed core" stops being one.

KIND_ACTION_PROPOSED = "paw.action.proposed"
KIND_ACTION_APPROVED = "paw.action.approved"
KIND_ACTION_REJECTED = "paw.action.rejected"
KIND_ACTION_DELIVERED = "paw.action.delivered"
KIND_ACTION_OUTCOME = "paw.action.outcome"
KIND_HANDOFF_RAISED = "paw.handoff.raised"
KIND_HANDOFF_RESOLVED = "paw.handoff.resolved"
KIND_VISITOR_ACTION = "paw.visitor.action"
KIND_CONVERSATION_STARTED = "paw.conversation.started"
KIND_CONVERSATION_TAKEOVER = "paw.conversation.takeover"
KIND_RUN_COMPLETED = "paw.run.completed"

CORE_KINDS: tuple[CoreKind, ...] = (
    CoreKind(
        id=KIND_ACTION_PROPOSED,
        display_name="Action proposed",
        description="The agent asked a human to decide something.",
    ),
    CoreKind(
        id=KIND_ACTION_APPROVED,
        display_name="Action approved",
        description="A human (or the triager) let the agent's proposal through.",
    ),
    CoreKind(
        id=KIND_ACTION_REJECTED,
        display_name="Action rejected",
        description="A human declined the agent's proposal.",
    ),
    CoreKind(
        id=KIND_ACTION_DELIVERED,
        display_name="Action delivered",
        description="The approved answer actually reached the person waiting for it.",
    ),
    CoreKind(
        id=KIND_ACTION_OUTCOME,
        display_name="Action outcome",
        description=(
            "The verdict on an executed action — whether it solved the problem, "
            "not merely whether it ran."
        ),
    ),
    CoreKind(
        id=KIND_HANDOFF_RAISED,
        display_name="Handoff raised",
        description="The agent decided a human needed to take this one.",
    ),
    CoreKind(
        id=KIND_HANDOFF_RESOLVED,
        display_name="Handoff resolved",
        description="The human finished what the agent handed over.",
    ),
    CoreKind(
        id=KIND_VISITOR_ACTION,
        display_name="Visitor action",
        description=(
            "A visitor-owned action the agent executed directly (add to cart, "
            "checkout) — the one kind that routinely carries attributed value."
        ),
    ),
    CoreKind(
        id=KIND_CONVERSATION_STARTED,
        display_name="Conversation started",
        description="A new person started talking to this agent.",
    ),
    CoreKind(
        id=KIND_CONVERSATION_TAKEOVER,
        display_name="Conversation takeover",
        description="The owner took the conversation off the agent.",
    ),
    CoreKind(
        id=KIND_RUN_COMPLETED,
        display_name="Run completed",
        description=(
            "One terminal agent run, counted. COUNT ONLY — tokens and cost stay "
            "in the run doc where billing reads them."
        ),
    ),
)

_CORE_KIND_IDS: frozenset[str] = frozenset(k.id for k in CORE_KINDS)


def is_core_kind(kind: str) -> bool:
    """True if ``kind`` is one of the curated core kinds."""
    return kind in _CORE_KIND_IDS


def validate_ledger_kind(kind: str) -> str:
    """Validate a ledger kind, returning it unchanged if valid.

    Rules (identical in shape to :func:`pocketpaw.senses.vocabulary.
    validate_sense_id`):

      - A ``paw.*`` kind is accepted ONLY if it is in the curated core set. An
        unknown ``paw.*`` kind raises — the core namespace is closed so a new
        emitter can't silently fragment it (two surfaces inventing
        ``paw.action.done`` and ``paw.action.complete`` for the same beat is how
        an analytics vocabulary dies).
      - A non-``paw.*`` kind (vendor extension, ``vendor.domain.verb``) is
        accepted freely — the extension space is open.
      - Anything that is neither a well-formed core kind nor a well-formed
        extension kind raises.
    """
    if not isinstance(kind, str) or not kind:
        raise LedgerKindValidationError(f"ledger kind must be a non-empty string, got {kind!r}")

    if kind.startswith("paw."):
        if not _CORE_KIND_PATTERN.match(kind):
            raise LedgerKindValidationError(
                f"malformed core ledger kind {kind!r} (expected paw.<domain>.<verb>)"
            )
        if kind not in _CORE_KIND_IDS:
            raise LedgerKindValidationError(
                f"unknown core ledger kind {kind!r}; the paw.* namespace is "
                f"closed. Known core kinds: {sorted(_CORE_KIND_IDS)}. "
                f"Use a vendor.domain.verb kind for custom beats."
            )
        return kind

    # Extension (vendor) kind — open namespace, just enforce the shape.
    if not _EXTENSION_KIND_PATTERN.match(kind):
        raise LedgerKindValidationError(
            f"malformed extension ledger kind {kind!r} (expected vendor.domain.verb)"
        )
    return kind


# ---------------------------------------------------------------------------
# Actor + surface
# ---------------------------------------------------------------------------


class LedgerActor(StrEnum):
    """WHO caused this row — not who it is about (that is always ``agent_id``).

    The split matters for the value board: "12 approvals" reads very differently
    depending on whether a human clicked twelve times or the triager did.
    """

    VISITOR = "visitor"  # an anonymous person on a site
    OWNER = "owner"  # the workspace's human operator
    AGENT = "agent"  # the agent acted on its own authority
    SYSTEM = "system"  # a sweeper, an executor, the auto-triager


# The surfaces that produce rows. Deliberately plain strings on an open set
# rather than a closed enum: a new surface should be able to record its work
# before it has earned a constant here, and an unknown surface is a legible
# label in a dashboard, not a crash. The KIND vocabulary is what stays closed.
SURFACE_PAW_BAR = "paw_bar"
SURFACE_CHAT = "chat"
SURFACE_BELT = "belt"
SURFACE_DEEP_WORK = "deep_work"
SURFACE_GROWTH = "growth"
SURFACE_INSTINCT = "instinct"  # the fallback: gated, but by an unidentified surface

# Instinct ``ActionTrigger.source`` prefixes → surface. Sources are colon-
# qualified by convention ("paw_bar:<widget_id>", "belt:develop"), so a prefix
# match is the honest read. Anything unmatched falls back to SURFACE_INSTINCT
# rather than guessing — an "instinct" bucket the owner can see is better than a
# confident mislabel they cannot.
_SOURCE_PREFIX_SURFACES: tuple[tuple[str, str], ...] = (
    ("paw_bar:", SURFACE_PAW_BAR),
    ("belt:", SURFACE_BELT),
    ("deep_work:", SURFACE_DEEP_WORK),
    ("growth:", SURFACE_GROWTH),
)


def surface_from_trigger(trigger_type: str, trigger_source: str) -> str:
    """Best-effort map from an Instinct trigger to a ledger surface.

    Used by the Instinct emitter, which sees every agent kind but is told only
    what the proposer put in the trigger. Prefix-matches the known
    colon-qualified sources first, then treats a bare ``agent`` trigger as chat
    (that is what an agent run proposes as), and otherwise returns
    ``SURFACE_INSTINCT``. Never raises — a bad trigger yields the fallback.
    """
    source = str(trigger_source or "").strip().lower()
    for prefix, surface in _SOURCE_PREFIX_SURFACES:
        if source.startswith(prefix):
            return surface
    if str(trigger_type or "").strip().lower() == "agent":
        return SURFACE_CHAT
    return SURFACE_INSTINCT


# ---------------------------------------------------------------------------
# OTel GenAI semantic-convention attribute names
# ---------------------------------------------------------------------------
#
# Borrowed verbatim from the OTel GenAI semantic conventions so a later exporter
# maps 1:1. Only the names are borrowed — no OTel infra, no spans, no collector.

ATTR_AGENT_ID = "gen_ai.agent.id"
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_CONVERSATION_ID = "gen_ai.conversation.id"
ATTR_OPERATION_NAME = "gen_ai.operation.name"

# Paw concepts with no OTel equivalent. Namespaced under ``paw.`` precisely so a
# reader can tell at a glance which attributes are portable and which are ours.
ATTR_ACTION_ID = "paw.action.id"
ATTR_ACTION_CATEGORY = "paw.action.category"
ATTR_INSTINCT_EVENT = "paw.instinct.event"
ATTR_APPROVAL_AUTO = "paw.approval.auto"
ATTR_DECISION_ACTOR = "paw.decision.actor"
ATTR_POCKET_ID = "paw.pocket.id"
ATTR_SCOPE_TYPE = "paw.scope.type"


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


class LedgerRow(BaseModel):
    """One thing an agent did, keyed by the agent that did it.

    ``agent_id`` is THE key of the whole design: the named worker with identity
    and memory owns its outcomes, not the pocket and not the surface. An empty
    ``agent_id`` is legal and means "we could not attribute this one" — the row
    still lands surface-keyed and the board shows an honest unattributed bucket,
    which beats dropping the row and quietly under-counting.
    """

    # Assigned by SQLite on insert; None on a row that has not been appended.
    id: int | None = None
    agent_id: str = ""
    workspace_id: str = ""
    surface: str = SURFACE_INSTINCT
    kind: str
    # The OutcomeStatus value ("solved" / "partial" / "not_solved" / "unknown")
    # when this row carries a verdict. None on every kind that isn't a verdict.
    outcome: str | None = None
    # Attributed value in minor units, when it is genuinely known (a cart total,
    # a checkout). Deliberately RAW — whether the owner's "value" is order value,
    # fee, or margin is an outcome-metering question, and baking one answer in
    # here would make the other two unrecoverable.
    value_cents: int | None = None
    currency: str | None = None
    # The producing record's id (action_id / run_id / "widget:customer"). Half of
    # the UNIQUE(kind, ref) dedupe guard, so it must be STABLE across replays.
    ref: str
    actor: str = LedgerActor.SYSTEM.value
    attrs: dict[str, Any] = Field(default_factory=dict)
    # ISO-8601 UTC (aware). Aware, not naive, because these rows are compared
    # against ChatRunDoc.createdAt and rendered in windows — a naive local stamp
    # would skew every window by the host's UTC offset.
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        """Enforce the vocabulary at the model boundary, not at each call site."""
        return validate_ledger_kind(value)


# ---------------------------------------------------------------------------
# Window parsing (shared by every read surface)
# ---------------------------------------------------------------------------

_WINDOW_PATTERN = re.compile(r"^(\d+)([dhw])$")

_WINDOW_UNITS: dict[str, str] = {"h": "hours", "d": "days", "w": "weeks"}

# Refuse an absurd window rather than letting it become an unbounded scan under
# a friendly-looking query string. A year of an append-only ledger is already
# past the point where the rollup (deferred, additive) is the right answer.
_MAX_WINDOW_DAYS = 366


class WindowParseError(ValueError):
    """Raised when a caller-supplied analytics window can't be understood."""


def parse_window(window: str) -> timedelta | None:
    """Parse ``30d`` / ``7d`` / ``24h`` / ``2w`` / ``all`` into a lookback.

    Returns ``None`` for ``all`` — and ONLY for ``all``. An empty or blank
    window raises rather than falling back to unbounded: "" is what an
    accidentally-empty query parameter looks like, and answering it with the
    whole history is the largest possible reinterpretation of a question nobody
    asked. Unbounded has an explicit spelling; use it.

    Raises :class:`WindowParseError` on anything malformed or beyond
    ``_MAX_WINDOW_DAYS`` so a router can turn it into a 422 instead of serving a
    silently-wrong number — an analytics surface that quietly reinterprets your
    question is worse than one that refuses it.
    """
    raw = str(window or "").strip().lower()
    if raw == "all":
        return None
    if not raw:
        raise WindowParseError("window must not be empty (use 'all' for no lower bound)")
    match = _WINDOW_PATTERN.match(raw)
    if not match:
        raise WindowParseError(
            f"unparseable window {window!r} (expected e.g. 24h, 7d, 30d, 2w, or all)"
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise WindowParseError(f"window must be positive, got {window!r}")
    delta = timedelta(**{_WINDOW_UNITS[match.group(2)]: amount})
    if delta > timedelta(days=_MAX_WINDOW_DAYS):
        raise WindowParseError(f"window {window!r} exceeds the {_MAX_WINDOW_DAYS}-day maximum")
    return delta


def window_start(window: str, *, now: datetime | None = None) -> str | None:
    """The ISO-UTC lower bound for ``window``, or ``None`` for an unbounded read.

    Rows store aware-UTC ISO strings, which sort lexicographically in the same
    order they sort chronologically, so the store compares against this string
    directly instead of parsing every row.
    """
    delta = parse_window(window)
    if delta is None:
        return None
    return ((now or datetime.now(UTC)) - delta).isoformat()
