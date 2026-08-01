"""The prompt assembler — renders layers, joins them, digests the stable ones.

Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam).

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
from collections.abc import Sequence
from dataclasses import dataclass, field

from pocketpaw.prompt.layer import PromptContext, PromptLayer

# Field/record separators for the digest input, so two layers cannot collide by
# concatenation (``("ab", "c")`` and ``("a", "bc")`` must not hash alike).
_FIELD_SEP = b"\x1f"
_RECORD_SEP = b"\x1e"

# The block separator between layers — the blank line the legacy string
# assembly used between every appended block.
_JOIN = "\n\n"


@dataclass(frozen=True)
class DroppedLayer:
    """A layer the assembler left out of ``text``, and why.

    Nothing produces one yet — the budget pass does. It is declared now so the
    consumers that report a truncated prompt can be written against the final
    shape rather than against a placeholder.
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
    """
    texts: list[str] = []
    keyed: list[tuple[str, str]] = []

    for layer in layers:
        output = await layer.render(ctx)
        if output.text:
            texts.append(output.text)
        if output.cache_key is not None:
            keyed.append((layer.name, output.cache_key))

    return AssembledPrompt(text=_JOIN.join(texts), stable_digest=_digest(keyed))
