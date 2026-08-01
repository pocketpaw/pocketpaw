"""The prompt assembler — renders layers, joins them, digests the stable ones.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).
Updated: 2026-08-02 (PA-1 review) — ``assemble`` guards ``layer.render()``. A
  raising layer is logged, dropped from the text, reported in ``dropped`` and
  keyed as a failure, so one layer cannot fail a turn (``AgentPool.run`` calls
  this outside any try). Cancellation still propagates. That gives
  ``dropped`` its first real producer, and pins the rule the budget pass will
  lean on: dropped from ``text`` never means dropped from the digest.

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

from pocketpaw.prompt.layer import PromptContext, PromptLayer

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


@dataclass(frozen=True)
class DroppedLayer:
    """A layer the assembler left out of ``text``, and why.

    Produced today by the render guard (a layer whose ``render`` raised); the
    budget pass is the other producer. Whatever the reason, the layer keeps its
    place in the digest — see ``_digest``.
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


async def assemble(layers: Sequence[PromptLayer], ctx: PromptContext) -> AssembledPrompt:
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
    """
    texts: list[str] = []
    keyed: list[tuple[str, str]] = []
    dropped: list[DroppedLayer] = []

    for layer in layers:
        name = getattr(layer, "name", type(layer).__name__)
        try:
            output = await layer.render(ctx)
        except Exception as exc:
            logger.warning("Prompt layer %r failed to render: %s", name, exc, exc_info=True)
            dropped.append(DroppedLayer(name=name, reason=f"render raised {type(exc).__name__}"))
            keyed.append((name, _FAILED_LAYER_KEY))
            continue
        if output.text:
            texts.append(output.text)
        if output.cache_key is not None:
            keyed.append((name, output.cache_key))

    return AssembledPrompt(
        text=_JOIN.join(texts), stable_digest=_digest(keyed), dropped=dropped
    )
