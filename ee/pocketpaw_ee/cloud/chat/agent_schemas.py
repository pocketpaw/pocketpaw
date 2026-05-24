# agent_schemas.py — Request and SSE-event payload schemas for the
#   enterprise agent chat endpoint.
# Changes: 2026-05-22 (Increment 3) — RFC-06 structure/data split:
#   added ``PocketMutationFrame``, the discriminated ``pocket_mutation``
#   SSE frame the granular ``agent_*`` edit ops emit. Its ``family``
#   discriminant ("updateComponents" for node/structure ops,
#   "updateDataModel" for state/data ops) lets the canvas re-render only
#   the structure layer or only the data layer instead of rebuilding the
#   whole pocket from the coarse ``PocketUpdated`` realtime event. Also
#   added the ``POCKET_EXECUTION`` SSE event name for the execution
#   router's per-request observability frame.
# Changes: 2026-05-24 — added ``surface`` + ``surface_meta`` fields so
#   clients can stamp a {surface_kind, meta} hint on every send. The
#   chat router passes them to ``surface_context.resolve_surface_context``
#   which renders a per-turn preamble injected ahead of the dynamic
#   scope tags. Unknown surface strings fall back to the GENERIC handler
#   so older clients keep working unchanged.
# Changes: 2026-05-24 (RFC 06 position #2 follow-up) — added the
#   plain-English ``kind`` discriminant alongside the existing A2UI-style
#   ``family``. ``kind`` is ``"structure" | "data" | "replace"``: every
#   ``pocket_mutation`` frame now carries it, including the full-document
#   ``replace`` push (which previously had no discriminant at all).
#   ``kind`` is the simpler client-facing label; ``family`` is kept for
#   any consumer that already branches on the A2UI names. Mapping:
#   structure-ops -> ``"structure"``, state-ops -> ``"data"``,
#   full-document push -> ``"replace"``. The field is additive —
#   un-discriminated consumers keep working byte-for-byte and the
#   coarse ``PocketUpdated`` realtime event remains the refetch fallback
#   for clients that ignore ``kind`` entirely.
"""Request and SSE-event payload schemas for the enterprise agent chat endpoint.

The endpoint lives at ``POST /cloud/chat/{scope}/{scope_id}/agent`` and streams
back a typed SSE event sequence. See
``docs/superpowers/specs/2026-04-23-enterprise-agent-chat-endpoint-design.md``
for the full protocol.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CloudAgentChatRequest(BaseModel):
    """Body of ``POST /cloud/chat/{scope}/{scope_id}/agent``."""

    content: str = Field(min_length=1, max_length=10_000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: str | None = None
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    # Required for group scope when the group has more than one agent member;
    # optional for dm (the agent peer is unambiguous) and pocket (primary agent
    # used unless overridden).
    agent_id: str | None = None
    # Idempotency key echoed back in ``message.persisted`` so the client can
    # reconcile its optimistic bubble before any agent output arrives.
    client_message_id: str | None = None
    # Optional dispatch hint from the client. One of:
    #   - ``pocket_create`` — swaps the system prompt to pocket-creation
    #     guidance so the agent uses ``create_pocket`` instead of
    #     rendering an inline ``ui-spec`` block. This is the only value
    #     that changes backend behavior today (see ``build_context_block``).
    #   - ``skill:<name>`` — the user invoked a slash command. The Claude
    #     Agent SDK reads the bare ``/<name> args`` message text and
    #     invokes its built-in Skill tool. NOTE: ``skill:*`` is accepted
    #     but NOT yet consumed — it (and ``skill_args``) are reserved for
    #     future deterministic dispatch in ``_run_agent_stream``.
    #   - ``None`` — no hint (default; what older clients send).
    # Kept as ``str`` (not ``Literal``) so a new ``skill:<name>`` needs no
    # schema bump. The validator below still rejects values that are
    # neither ``pocket_create`` nor ``skill:``-prefixed, so a client-side
    # typo fails loudly with a 422 instead of being silently ignored.
    intent: str | None = None
    # Argument string for ``intent="skill:<name>"`` (empty when the skill
    # was invoked bare). Reserved — not consumed by the backend yet.
    skill_args: str | None = None
    # Surface-aware context hint (RFC: universal surface context).
    # ``surface`` is the SurfaceKind enum value the client computed from
    # ``$page.route.id`` ("home", "pockets", "pocket", "mission_control",
    # …). Unknown values fall back to ``GENERIC`` in
    # ``surface_context.service.resolve_surface_context`` so a client can
    # ship a new surface name before the backend handler does. ``None``
    # means the client didn't stamp a hint (older clients) — the agent
    # then sees only the legacy three-line dynamic context.
    surface: str | None = None
    # Per-surface meta hint — ``pocket_id`` / ``widget_id`` / ``agent_id``
    # / etc. Validated downstream by ``SurfaceMetaRequest``; unknown
    # fields are dropped. ``None`` is treated as an empty dict.
    surface_meta: dict[str, Any] | None = None

    @field_validator("intent")
    @classmethod
    def _check_intent(cls, v: str | None) -> str | None:
        """Reject unknown intents so client typos surface as a 422.

        ``skill:<name>`` stays open-ended (any skill name) for forward
        compatibility; only genuinely unrecognized values are rejected.
        """
        if v is None or v == "pocket_create" or v.startswith("skill:"):
            return v
        raise ValueError("intent must be 'pocket_create', 'skill:<name>', or null")


class SseEventName(StrEnum):
    """Names of SSE events emitted by the cloud agent endpoint.

    Kept as an Enum so tests and consumers have a single source of truth; the
    router itself builds raw SSE frames (``event:``/``data:``) for performance.
    """

    MESSAGE_PERSISTED = "message.persisted"
    STREAM_START = "stream_start"
    THINKING = "thinking"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    CHUNK = "chunk"
    RIPPLE = "ripple"
    POCKET_CREATED = "pocket_created"
    POCKET_MUTATION = "pocket_mutation"
    POCKET_EXECUTION = "pocket_execution"
    ASK_USER_QUESTION = "ask_user_question"
    STREAM_END = "stream_end"
    ERROR = "error"


# Map of the granular ``agent_*`` op identifier -> mutation ``family``.
# Structure ops touch ``rippleSpec.ui`` (the component tree); data ops
# touch ``rippleSpec.state`` (the data model). The split is the cheap
# RFC-06 win — a data-only change need not re-run layout.
_STRUCTURE_OPS: frozenset[str] = frozenset(
    {
        "node_added",
        "node_replaced",
        "node_prop_set",
        "node_moved",
        "node_removed",
        "node_prop_array_item_set",
        "node_prop_array_item_appended",
        "node_prop_array_item_removed",
    }
)
_DATA_OPS: frozenset[str] = frozenset(
    {
        "state_set",
        "state_appended",
        "state_removed",
        "state_patched",
    }
)


def family_for_op(op: str) -> Literal["updateComponents", "updateDataModel"] | None:
    """Resolve a granular op identifier to its mutation ``family``.

    ``updateComponents`` for a structure (node) op, ``updateDataModel``
    for a data (state) op, ``None`` for anything else (the caller should
    not emit a discriminated frame — the coarse ``PocketUpdated`` event
    still covers it).
    """
    if op in _STRUCTURE_OPS:
        return "updateComponents"
    if op in _DATA_OPS:
        return "updateDataModel"
    return None


# RFC 06 position #2 — the plain-English ``kind`` discriminant. Mirrors
# ``family`` but uses simpler labels every consumer can grep for without
# learning the A2UI vocabulary. ``"replace"`` is a third value covering
# the full-document push (no granular op identifier — the whole pocket
# document goes over the wire). Granular sources / actions ops route
# through the replace push today, so their frame also carries
# ``kind == "replace"``. A future increment can split those into
# in-place structure ops; the field gives us room without a wire break.
MutationKind = Literal["structure", "data", "replace"]


def kind_for_op(op: str) -> MutationKind | None:
    """Resolve a granular op identifier to its plain-English ``kind``.

    ``"structure"`` for node/component-tree ops, ``"data"`` for state
    ops, ``None`` for anything else. The caller decides what to do with
    ``None`` — ``_push_mutation_frame`` falls back to the legacy
    un-discriminated frame so the canvas still gets a signal.

    The full-document ``replace`` push does NOT go through this resolver
    — it stamps ``kind="replace"`` directly because there is no granular
    op identifier on that path.
    """
    if op in _STRUCTURE_OPS:
        return "structure"
    if op in _DATA_OPS:
        return "data"
    return None


class PocketMutationFrame(BaseModel):
    """A narrow, discriminated ``pocket_mutation`` SSE frame.

    Emitted by the granular ``agent_*`` edit ops in
    ``pockets/agent_context.py`` alongside the coarse ``PocketUpdated``
    realtime event. The coarse event tells a client "this pocket
    changed, refetch"; this frame tells it *what kind* of change so the
    canvas can patch in place rather than rebuild.

    Two discriminants ride on every frame — they carry the SAME
    structure/data split, just under different names:

    * ``kind`` (plain English, the canonical RFC 06 position #2 label):
      ``"structure"`` for a component-tree op, ``"data"`` for a state op,
      ``"replace"`` for the full-document push that ``_push_replace``
      emits (sources/actions changes, top-level pocket-field updates,
      embedded-widget mutations). Clients should branch on ``kind`` —
      it's the simpler grep target and the one the channel doc names.
    * ``family`` (A2UI vocabulary, kept for back-compat):
      ``"updateComponents"`` for structure ops, ``"updateDataModel"`` for
      data ops. ``None`` on a full-document replace, because the
      replace push isn't an A2UI granular op.

    ``op`` is the granular op identifier (``state_set``, ``node_added``,
    …) or ``"replace"`` for the full-document push; ``payload`` is that
    op's narrow change descriptor (subtree, path, value, full pocket) —
    the same fields the historical un-discriminated frame carried at
    the top level.

    Wire shape: the frame is emitted FLAT — ``kind``, ``family``, ``op``,
    ``pocket_id`` and every ``payload`` key sit at the top level of the
    SSE ``data`` object, next to the legacy ``action`` key. This keeps
    the historical un-discriminated consumers working byte-for-byte
    (they read ``action`` + flat payload) while new consumers branch on
    ``kind`` (preferred) or ``family``. ``to_wire()`` produces that flat
    dict. Older clients that ignore both discriminants still get the
    coarse ``PocketUpdated`` event, so the frame is purely additive —
    no client is forced to upgrade.
    """

    kind: MutationKind = Field(
        description=(
            "Plain-English discriminant. 'structure' for component-tree ops, "
            "'data' for state ops, 'replace' for the full-document push. The "
            "canonical RFC 06 position #2 field name."
        ),
    )
    family: Literal["updateComponents", "updateDataModel"] | None = Field(
        default=None,
        description=(
            "A2UI-vocabulary alias for ``kind`` on granular ops. "
            "``None`` on a full-document ``replace`` (no granular A2UI op)."
        ),
    )
    op: str = Field(min_length=1, description="Granular op identifier, e.g. 'state_set'.")
    pocket_id: str = Field(description="Id of the mutated pocket.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="The op's narrow change descriptor (subtree, path, value, …).",
    )

    def to_wire(self) -> dict[str, Any]:
        """Flatten to the wire dict pushed via ``push_pocket_mutation``.

        ``payload`` keys are spread to the top level (next to ``kind`` /
        ``family`` / ``op`` / ``pocket_id``) and the legacy ``action``
        alias is added so un-discriminated consumers keep working. A
        ``payload`` key never collides with ``kind`` / ``family`` /
        ``op`` / ``action`` / ``pocket_id`` — the granular ops only put
        change descriptors there. ``family`` is omitted from the wire
        when it's ``None`` (full-document replace), so consumers that
        check ``"family" in frame`` keep working as before.
        """
        wire: dict[str, Any] = {
            "action": self.op,
            "op": self.op,
            "kind": self.kind,
            "pocket_id": self.pocket_id,
        }
        if self.family is not None:
            wire["family"] = self.family
        wire.update(self.payload)
        return wire
