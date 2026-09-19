# ee/pocketpaw_ee/terrarium/moderation.py
#
# One check, both directions: a viewer line on its way IN (rejected, nothing
# written) and a citizen's say / write / moment headline on its way OUT
# (withheld: the Journal row lands with ``[withheld]`` as its body so ``seq``
# stays continuous and the cost still counts).
#
# It is a deny-list plus a length cap, and that is all it is. PocketPaw's
# security stack was checked first and nothing there takes a short line of
# prose and returns a verdict: ``GuardianAgent`` classifies SHELL COMMANDS
# through an LLM, ``InjectionScanner`` looks for prompt-injection shapes, and
# ``PIIScanner`` finds identifiers. Replace ``allowed`` with a real text
# moderation call (a provider moderation endpoint, or a Guardian-style LLM
# classifier with a prose prompt) before the public flag flips on a world
# strangers can speak into; keep the length cap either way.
#
# Extra terms come from ``TERRARIUM_DENY_TERMS`` (comma separated, matched on
# word boundaries, case-insensitive) so an operator can react without a deploy.

"""Inbound and outbound text check for the terrarium."""

from __future__ import annotations

import os
import re
from functools import lru_cache

MAX_LEN = 500
WITHHELD = "[withheld]"
MODERATED_KINDS: frozenset[str] = frozenset({"say", "write", "moment"})

# Harassment and self-harm incitement. Short on purpose — see the header.
_DEFAULT_TERMS: tuple[str, ...] = (
    "kill yourself",
    "kys",
    "go die",
    "nazi scum",
    "subhuman",
)


@lru_cache(maxsize=4)
def _pattern(extra: str) -> re.Pattern[str]:
    terms = [*_DEFAULT_TERMS, *(t.strip() for t in extra.split(",") if t.strip())]
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)


def clean(text: str) -> bool:
    """True when nothing on the deny-list is in ``text``. No length cap — this
    is the check for a charter or an artifact body, which may run long."""
    return _pattern(os.environ.get("TERRARIUM_DENY_TERMS", "")).search(text or "") is None


def allowed(text: str) -> bool:
    """True when a LINE (a say, a headline, a viewer message) may enter the
    Journal as written: clean and within ``MAX_LEN``."""
    return len(text) <= MAX_LEN and clean(text)


__all__ = ["MAX_LEN", "MODERATED_KINDS", "WITHHELD", "allowed", "clean"]
