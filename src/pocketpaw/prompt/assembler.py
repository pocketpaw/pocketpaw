"""The prompt assembler — renders layers, joins them, digests the stable ones.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-1 review) — ``assemble`` guards ``layer.render()``. A
  raising layer is logged, dropped from the text, reported in ``dropped`` and
  keyed as a failure, so one layer cannot fail a turn (``AgentPool.run`` calls
  this outside any try). Cancellation still propagates. That gives
  ``dropped`` its first real producer, and pins the rule the budget pass will
  lean on: dropped from ``text`` never means dropped from the digest.

Updated: 2026-08-03 (PA-5) — ``assemble`` enforces per-layer caps and an
  optional total ``budget_chars``, and reports every cut in ``dropped`` and in
  the log. Two cuts exist and they are NOT the same operation:

  * a layer's own ``max_chars`` TRUNCATES its text, always, before the budget is
    even considered. The layer keeps its ``cache_key``.
  * the budget DROPS whole layers, lowest priority first, and never truncates.
    A dropped layer that declared a key contributes ``_BUDGET_DROPPED_KEY``
    instead of it.

  Why they differ is PA-5's one real design question and it is answered in
  ``_apply_cap`` and ``_fit_to_budget``. In one line: a cap is a pure function
  of the layer's own bytes and so composes with any key contract, while a cut
  sized from what is LEFT of the budget depends on what the layer's SIBLINGS
  rendered — so one key would name two different texts, which is #1842 arriving
  through the budget instead of through the backend.

  The budget defaults to ``None`` (unbounded). ``context_builder``'s 32,000 is
  NOT adopted as a default: measured cloud prompts run past 44k (see
  ``claude_sdk._VOLATILE_PROMPT_MARKERS``' note, "char ~1.4k of a ~44k prefix"),
  so defaulting to it would start truncating live traffic in the one task whose
  acceptance is that no byte moves. PA-7 passes it on the channel path; PA-9
  measures what it should be.

Two outputs, and the second is the point:

* ``text`` — the layers' rendered text, joined in the order given.
* ``stable_digest`` — a hash over the KEYED layers' ``cache_key`` values. Not
  over the rendered text. A backend that caches an agent object with the prompt
  baked in folds this into its cache key, and gets correctness without the
  churn: hashing the text would move the digest on every turn (the soul recall
  is keyed on the user's message), which is exactly the trade-off PR #1842
  refused when it made ``pydantic_ai`` pass instructions per-run instead.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from pocketpaw.prompt.layer import Priority, PromptContext, PromptLayer

logger = logging.getLogger(__name__)

# Field/record separators for the digest input, so two layers cannot collide by
# concatenation (``("ab", "c")`` and ``("a", "bc")`` must not hash alike).
_FIELD_SEP = b"\x1f"
_RECORD_SEP = b"\x1e"

# The block separator between layers — the blank line the legacy string
# assembly used between every appended block.
_JOIN = "\n\n"

# The key a layer contributes when its ``render`` raised. Deterministic, and
# distinct from any key a successful render can produce (a real key is
# non-empty and this one is reserved), so "the surface layer failed" and "the
# surface layer rendered" are different identities. Stable across different
# exceptions on purpose: both produce the same prompt — one with nothing from
# that layer in it — so both should hash alike.
_FAILED_LAYER_KEY = "\x00render-failed"

# The key a KEYED layer contributes when the budget dropped it. Reserved in the
# same way as ``_FAILED_LAYER_KEY`` and distinct from it, because the two are
# different prompts: a failed layer might have rendered anything, a dropped one
# rendered something known and too large. Neither may reuse the layer's real
# key — "atlas dropped" and "atlas present" must not be one identity, or a
# backend caching on the digest serves the prompt from the wrong one.
#
# An UNKEYED layer that the budget drops contributes NOTHING, not this. It was
# already outside the digest when it rendered; making only its ABSENCE visible
# would be an asymmetry that moves the digest on a per-turn condition, which is
# the exact churn ``cache_key=None`` exists to prevent.
_BUDGET_DROPPED_KEY = "\x00budget-dropped"

# Appended to a capped layer's text so the model can see that it is reading a
# fragment. Byte-for-byte what ``context_builder._assemble_with_budget``
# appends, INCLUDING the fact that it pushes the block 15 chars past the cap it
# is enforcing. Matching a shipped quirk is deliberate: PA-7's acceptance is
# that channel-path prompts stay byte-identical, and it cannot be met if the
# truncation shape changes underneath it. Flagged for PA-9, which is the task
# that gets to change cap arithmetic because it is the one with measurements.
_TRUNCATION_MARKER = "\n[...truncated]"


@dataclass(frozen=True)
class DroppedLayer:
    """A cut the assembler made, and why.

    Three producers, all of them recorded here AND logged, so no layer ever
    loses text silently:

    * the render guard — a layer whose ``render`` raised;
    * a layer's ``max_chars`` — the text is TRUNCATED, not removed, and the
      layer is still in the prompt. The name reads oddly for that case and the
      class is not renamed anyway: ``DroppedLayer`` is already public API from
      PA-1 and a rename buys a better word at the cost of churn in four
      modules and five test files.
    * the budget — the layer is removed whole.

    Whatever the reason, the layer keeps its place in the digest — see
    ``_digest``.
    """

    name: str
    reason: str


@dataclass(frozen=True)
class AssembledPrompt:
    """What the assembler hands back to a caller."""

    text: str
    stable_digest: str
    dropped: list[DroppedLayer] = field(default_factory=list)


def _digest(keyed: Sequence[tuple[str, str]]) -> str:
    """Hash ``(layer name, cache_key)`` pairs in order.

    The name is in the hash so two layers cannot swap keys unnoticed, and the
    order is because it changes the prompt: a digest over an unordered set
    would call two different prompts identical. Truncated to 16 hex chars, the
    same bound ``claude_sdk._client_cache_key`` uses.

    THE RULE, worth stating before the budget pass makes it load-bearing:
    dropping a layer from ``text`` must never drop it from the digest. A layer
    left out for budget, or one whose render raised, still contributes a key —
    otherwise "atlas dropped" and "atlas never present" are one identity, and a
    backend caching on the digest serves the prompt from the wrong one.

    No keyed layers at all hashes to a fixed constant, which does mean "every
    layer is volatile" and "no layers" agree. That is honest rather than
    papered over: in both cases there is nothing stable to key on, and the
    caller who passes an empty layer list has a bigger problem than its digest.
    """
    h = hashlib.sha256()
    for name, key in keyed:
        h.update(name.encode("utf-8", "replace"))
        h.update(_FIELD_SEP)
        h.update(key.encode("utf-8", "replace"))
        h.update(_RECORD_SEP)
    return h.hexdigest()[:16]


@dataclass
class _Rendered:
    """One layer's rendered state as the two passes move it along.

    Mutable and private: ``assemble`` needs somewhere to hold a layer's text
    between the cap pass, the budget pass and the join, and the ORDER of this
    list is the layer list's order throughout — the budget decides in priority
    order but never reorders what is emitted, because the prompt's order is a
    cache and attention contract (see ``AgentPool._SYSTEM_PROMPT_LAYERS``).
    """

    name: str
    priority: Priority
    text: str
    cache_key: str | None
    kept: bool = True


async def assemble(
    layers: Sequence[PromptLayer],
    ctx: PromptContext,
    *,
    budget_chars: int | None = None,
) -> AssembledPrompt:
    """Render ``layers`` against ``ctx`` and return the assembled prompt.

    Layers render in the order given and their text is joined with a blank
    line, skipping any that rendered to nothing so an inapplicable layer leaves
    no gap. A layer that returns ``cache_key=None`` contributes text but no
    key, so it never reaches the digest.

    A layer that RAISES is dropped, not propagated: it is logged, contributes
    no text, lands in ``dropped``, and keys as a failure. One layer cannot fail
    a turn. The guard lives here rather than in each layer because there is
    about to be a lot of them — the surface layer alone fans out to two dozen
    handlers doing I/O — and a rule enforced in one place is a rule.

    ``asyncio.CancelledError`` propagates: it is a ``BaseException``, so the
    guard's ``except Exception`` cannot catch it. That is deliberate rather than
    incidental — a cancelled run is the caller tearing this down, not a layer
    failing, and degrading it would turn a cancelled turn into a silently
    truncated prompt. There is no explicit re-raise clause because it would be
    dead code; the property is pinned by a test instead, which is what would
    catch a future widening of this guard.

    THEN TWO SIZING PASSES (PA-5), in this order and not the other:

    1. Each layer's own ``max_chars`` truncates its text. Unconditional, so it
       is part of what the layer renders rather than a decision about the
       prompt.
    2. ``budget_chars``, if given, removes WHOLE layers until the rest fits,
       lowest ``priority`` first. Never truncates.

    ``budget_chars=None`` means unbounded, which is what every caller passes
    today, and with no ``max_chars`` set on any pre-PA-5 layer that makes both
    passes no-ops: the assembled bytes are exactly what they were.
    """
    rendered: list[_Rendered] = []
    dropped: list[DroppedLayer] = []

    for layer in layers:
        name = getattr(layer, "name", type(layer).__name__)
        try:
            output = await layer.render(ctx)
        except Exception as exc:
            logger.warning("Prompt layer %r failed to render: %s", name, exc, exc_info=True)
            dropped.append(DroppedLayer(name=name, reason=f"render raised {type(exc).__name__}"))
            # ``kept`` stays True with empty text rather than False: the budget
            # pass and the join both skip empty text anyway, and False is
            # reserved for "the budget removed this", which is a different key.
            rendered.append(
                _Rendered(
                    name=name,
                    priority=getattr(layer, "priority", Priority.MEDIUM),
                    text="",
                    cache_key=_FAILED_LAYER_KEY,
                )
            )
            continue
        rendered.append(
            _Rendered(
                name=name,
                priority=getattr(layer, "priority", Priority.MEDIUM),
                text=_apply_cap(name, output.text, getattr(layer, "max_chars", None), dropped),
                cache_key=output.cache_key,
            )
        )

    _fit_to_budget(rendered, budget_chars, dropped)

    keyed: list[tuple[str, str]] = [
        (record.name, record.cache_key if record.kept else _BUDGET_DROPPED_KEY)
        for record in rendered
        if record.cache_key is not None
    ]
    texts = [record.text for record in rendered if record.kept and record.text]
    return AssembledPrompt(text=_JOIN.join(texts), stable_digest=_digest(keyed), dropped=dropped)


def _apply_cap(name: str, text: str, cap: int | None, dropped: list[DroppedLayer]) -> str:
    """Truncate ``text`` to the layer's own ceiling, and say so.

    THE LAYER KEEPS ITS ``cache_key``, and this is PA-5's one genuinely
    dangerous decision, so here is the argument in full.

    The hazard is real: a key names what a layer WOULD have rendered, and after
    a cut the prompt carries less than that. If two assemblies can share one key
    and differ in text, a backend that bakes the prompt into a cached agent
    serves the wrong one — #1842, reached through the budget rather than through
    the backend.

    They cannot, for this cut. ``cap`` is a module constant and ``text[:cap]`` is
    a pure function of the layer's own bytes, so whatever the key promised about
    the full text it promises exactly as strongly about the capped text: if
    ``key ⟹ same text`` then ``key ⟹ same text[:cap]``. The cap adds no new way
    for one key to name two prompts. It is the SAME lossy-view relationship the
    ``surface`` and ``identity`` layers already document, made a little lossier
    by a constant — not a new relationship.

    The two alternatives are both worse, and worse in the unsafe direction:

    * KEY THE TRUNCATED FORM (mix the cut into the key). Buys nothing the
      paragraph above does not already have, and costs the property that makes a
      key useful: ``identity``'s key deliberately ignores drifting soul counters
      so it does NOT move every turn, and folding a length back in would move it
      whenever a counter changed a digit.
    * GO UNKEYED. This is the one that looks cautious and is actually the bug.
      An unkeyed layer is excluded from the digest, so a capped ``atlas`` block
      that changed from tenant A's text to tenant B's would not move the digest
      AT ALL, and B would be served A's cached agent. "I am not sure this key is
      exact" and "this content does not belong in the key" are opposite claims;
      answering the first with the second is how a layer ends up silently
      unkeyed.

    What genuinely does not survive this argument is a cut sized from the
    REMAINING budget — see ``_fit_to_budget``, which is why it does not make
    one.

    The corollary, enforced by choosing where caps go rather than by code: only
    cap a layer whose key determines its text. ``identity`` is capped ``None``
    for exactly that reason.
    """
    if cap is None or len(text) <= cap:
        return text
    dropped.append(
        DroppedLayer(name=name, reason=f"truncated to its {cap}-char cap (was {len(text)})")
    )
    logger.info("Prompt layer %r truncated to its %d-char cap (was %d)", name, cap, len(text))
    return text[:cap] + _TRUNCATION_MARKER


def _fit_to_budget(
    rendered: list[_Rendered], budget_chars: int | None, dropped: list[DroppedLayer]
) -> None:
    """Remove whole layers, lowest priority first, until the rest fits.

    NEVER TRUNCATES, and that is the deliberate divergence from
    ``context_builder._assemble_with_budget``, which cuts a CRITICAL block down
    to whatever is left. That cut is sized from what the layer's SIBLINGS
    rendered, so the same key would name a different text every time something
    above it grew — the failure ``_apply_cap`` argues a constant cap cannot
    cause. A CRITICAL layer here is emitted whole and the budget overruns
    loudly instead. A critical layer that must be bounded takes a ``max_chars``.

    PRIORITY IS FIRST REFUSAL ON THE REMAINING BUDGET, NOT A STRICT PREFIX. A
    MEDIUM layer too large to fit is skipped and a LOW layer that does fit is
    still admitted after it — ``_assemble_with_budget``'s ``continue``, kept on
    purpose. It never drops something to make room for something less important;
    it only lets a small block use space a large one could not, which delivers
    strictly more content for the same budget.

    Two arithmetic quirks inherited verbatim from ``_assemble_with_budget``,
    both flagged for PA-9 rather than fixed here: the ``\\n\\n`` separators are
    not charged to the budget (so the emitted text can exceed it by two chars
    per boundary), and ``_TRUNCATION_MARKER`` is added after the cap. Both are
    small and both are load-bearing for PA-7, whose acceptance is that the
    channel path's bytes do not move.
    """
    if budget_chars is None:
        return

    remaining = budget_chars
    for _index, layer in sorted(enumerate(rendered), key=lambda pair: (pair[1].priority, pair[0])):
        if not layer.text:
            continue
        if layer.priority == Priority.CRITICAL:
            if len(layer.text) > remaining:
                logger.warning(
                    "Prompt layer %r is CRITICAL and over budget (%d chars, %d remaining) — "
                    "emitted whole; a budget-sized cut would break its cache key",
                    layer.name,
                    len(layer.text),
                    remaining,
                )
            remaining -= len(layer.text)
            continue
        if len(layer.text) > remaining:
            layer.kept = False
            dropped.append(
                DroppedLayer(
                    name=layer.name,
                    reason=(
                        f"dropped for budget ({len(layer.text)} chars, "
                        f"priority {layer.priority.name}, {remaining} remaining)"
                    ),
                )
            )
            logger.warning(
                "Dropped prompt layer %r (%d chars, priority %s) — budget exhausted (%d remaining)",
                layer.name,
                len(layer.text),
                layer.priority.name,
                remaining,
            )
            continue
        remaining -= len(layer.text)
