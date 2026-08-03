"""Prompt layers — the unit the system prompt is assembled from.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-1 review) — ``LayerOutput`` rejects an empty
  ``cache_key``. ``""`` read as "stable forever" while being exactly what an
  author types when they mean "nothing" — the one answer to this field's
  question that failed silently in the unsafe direction.
Updated: 2026-08-02 (PA-2) — ``PromptContext`` carries the surface the user is
  looking at: ``surface_preamble`` (the rendered block) and
  ``surface_cache_key`` (what the EE handler that BUILT it says it read). Both
  are plain data — a ``str`` and a ``str | None`` — because the producer lives
  in ``pocketpaw_ee`` and the OSS core must never import it (the same shape
  ``deny_mcp_tool_ids`` crosses on). Defaulted so every non-cloud caller (the
  channel path, OSS local runs, ``prewarm`` before a surface is known) is
  unchanged.
Updated: 2026-08-03 (PA-5) — three additions, all in service of the budget:
  :class:`Priority` (the drop order), ``PromptLayer.max_chars`` (the per-layer
  cap), and two more plain-data channels on ``PromptContext`` — the atlas
  primer (+ its tenant scope) and the about-member block (+ its user id). The
  ``priority`` field flips MEANING here: it was a free int where BIGGER was
  more important and nothing read it, and it is now a ``Priority`` where
  SMALLER is, matching ``context_builder._Priority`` so PA-7 can delete that
  enum instead of translating between two conventions.

The system prompt used to be one long string built by appending blocks in
``AgentPool._assemble_system_prompt``. That worked until three backends
independently cached an agent object with the prompt baked in and a cache key
that could not see prompt text (see ``pydantic_ai``'s 2026-08-01 (f) note): a
brand-new chat session was served the previous session's prompt. The per-backend
fix landed in PR #1842; the shape that let three authors make the same mistake
is what this package changes.

The mechanism is :class:`LayerOutput`. A layer returns its text AND a
``cache_key``, and ``cache_key`` has NO DEFAULT — a layer author cannot forget
to answer "does this content belong in a cache key". ``None`` is the explicit
answer for volatile, per-turn content (the soul recall keyed on the user's
message), and excludes the layer from the assembled digest.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol


class Priority(enum.IntEnum):
    """The order layers are DROPPED in when the prompt exceeds its budget.

    Lower value = more important, which is ``context_builder._Priority``'s
    convention rather than the ``priority = 100`` ints these layers carried
    before PA-5. Deliberately identical to that enum, values included, because
    PA-7 deletes it in favour of this one and a task that has to translate
    between two conventions while moving 14 blocks will get one of them wrong.

    CRITICAL means something stronger here than it does in
    ``_assemble_with_budget``, and the difference is the whole of PA-5's
    constraint 3. There, a CRITICAL block over budget is TRUNCATED to whatever
    is left. Here it is emitted WHOLE and the budget is allowed to overrun,
    because a budget-dependent truncation is the one cut that breaks a cache
    key: how much of a layer survives would depend on what its SIBLINGS
    rendered, so one key could name two different texts. A layer that must be
    bounded gets a constant ``max_chars`` instead — that is a pure function of
    its own content and composes with any key. See :func:`.assembler.assemble`.
    """

    CRITICAL = 0  # Never dropped, never truncated to fit — bound it with max_chars
    HIGH = 1  # Dropped only after everything below it is gone
    MEDIUM = 2  # Dropped when the budget is tight
    LOW = 3  # First to go


@dataclass(frozen=True)
class LayerOutput:
    """One layer's contribution to the prompt.

    ``cache_key`` is REQUIRED and deliberately not defaulted:

    * a ``str`` means "this content is stable for as long as this key is" — the
      key, not the text, is what the digest hashes, so a layer whose rendering
      varies without changing what it MEANS (a soul's memory counter, a
      timestamp) keeps one key and does not churn a backend's agent cache;
    * ``None`` means "this content is per-turn" — the layer is excluded from
      the digest entirely, so a retrieval block keyed on the user's message
      cannot make every turn look like a new agent.

    An empty ``text`` still contributes its key: a layer that renders to
    nothing under one identity and to something under another is a change the
    digest must see.

    An empty ``cache_key`` is REJECTED. ``""`` currently reads as "stable
    forever" — the strongest claim a layer can make — and it is also what
    someone types when they mean "nothing". Of the answers to a question this
    field exists to force, it is the only one that fails silently in the unsafe
    direction, so it fails loudly instead. ``None`` is how you say volatile.
    """

    text: str
    cache_key: str | None

    def __post_init__(self) -> None:
        if self.cache_key == "":
            raise ValueError(
                "cache_key must be a non-empty string or None; "
                "None is how a layer declares itself volatile"
            )


@dataclass(frozen=True)
class PromptContext:
    """The per-run inputs every layer renders against.

    Layers are stateless and registered process-wide (see
    :class:`~pocketpaw.prompt.registry.PromptLayerRegistry`), so everything that
    varies per run arrives here rather than on the layer instance — a layer
    holding a tenant's data between runs is the bug class this package exists
    to close.

    ``instance`` is the pool's ``AgentInstance`` and is typed ``Any`` on
    purpose: this package must not import ``pocketpaw.agents``, which imports
    it. The same opaque pass-through discipline as ``SessionHandle.session_store``.

    ``surface_preamble`` / ``surface_cache_key`` (PA-2) are the surface the user
    is looking at, resolved in the EE cloud layer and handed across as plain
    data. The KEY comes with the text rather than being derived from it here
    because only the handler that built the preamble knows what it read: a
    pocket preamble lists the first 12 of N widgets under a 1500-char cap, so
    editing widget 13 moves the pocket without moving a single rendered byte.
    ``None`` (no surface, or a producer that would not claim stability) means
    the layer keeps its text out of the digest.

    ``atlas_primer`` / ``tenant_scope`` and ``user_info`` / ``user_id`` (PA-5)
    are the two channels the budget was built for. Same plain-data shape as the
    surface pair and for the same reason, but note the asymmetry: the surface
    pair's KEY is the producer's claim because its text is a lossy view of a
    live pocket, whereas here the id/scope is only the coarse half of the key —
    the layers hash their own bytes as well, because unlike a preamble these
    two blocks arrive complete. Both default to the no-content answer, so every
    caller that exists today assembles exactly the bytes it did before.
    """

    instance: Any
    agent_id: str
    message: str
    instructions: str
    knowledge_context: str
    system_message_override: str | None
    surface_preamble: str = ""
    surface_cache_key: str | None = None
    atlas_primer: str = ""
    tenant_scope: str | None = None
    user_info: str = ""
    user_id: str | None = None


class PromptLayer(Protocol):
    """One contributor to the assembled system prompt.

    ``priority`` says where this layer sits in the DROP order when the assembled
    prompt exceeds its budget, and it belongs to the layer rather than to
    whoever assembled the list: only the layer knows whether losing it costs the
    agent a nicety or its instructions.

    ``max_chars`` is the layer's own ceiling, applied unconditionally — NOT a
    share of the budget. That distinction is load-bearing rather than
    stylistic: a constant cap is a pure function of the layer's own text, so
    whatever ``cache_key`` promised about the full text it promises equally
    about the capped text, and the layer keeps its key. A cut sized from what is
    LEFT of the budget promises nothing of the kind, which is why the assembler
    drops whole layers instead. ``None`` = no ceiling, which is the right answer
    for a layer whose key under-reports its text (``identity`` keys on the agent
    and lets soul counters drift beneath it, so a cap there could push a stable
    byte off the end as a counter grew) and for one nobody has measured yet.
    """

    name: str
    priority: Priority
    max_chars: int | None

    async def render(self, ctx: PromptContext) -> LayerOutput: ...
