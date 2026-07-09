# tagging.py — free-form auto-tag derivation from extraction output.
# Created: 2026-07-03 — FL-6 "Auto-tagging on ingest". Derives a small set
#   of free-form tags from an ExtractionResult (title + captions + text) with
#   a dependency-free keyword/keyphrase heuristic. The PRD chose free-form
#   over a controlled vocabulary for v1, and the rule is "reuse what
#   extraction already produced" — this never calls a new external LLM.
#   If the extraction adapter already surfaced candidate labels in
#   ``metadata`` (``labels`` / ``keywords`` / ``tags``), those win.
#   ``merge_tags`` unions derived tags with any user-applied tags so a
#   re-index never clobbers a hand-typed tag.
"""Free-form auto-tag derivation.

The listener runs extraction on every upload; this module turns the resulting
:class:`ExtractionResult` into a small, normalized, deduped tag list. Design
constraints (FL-6):

  * No new external LLM call — reuse the text/captions extraction already
    produced. If a captioning adapter surfaced candidate labels in
    ``result.metadata`` (under ``labels``, ``keywords`` or ``tags``), prefer
    those verbatim.
  * Free-form, not a controlled vocabulary (PRD decision for v1).
  * Normalized: lowercase, trimmed, punctuation-stripped, deduped.
  * Capped at :data:`MAX_TAGS` so the library UI stays tidy.
  * Union, never replace: :func:`merge_tags` keeps pre-existing user tags.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

MAX_TAGS = 8
"""Cap on auto-derived tags per file — keep the library chip row tidy."""

_MIN_TOKEN_LEN = 3
"""Tokens shorter than this are dropped (too generic to be a useful tag)."""

_MAX_TAG_LEN = 40
"""Guard against a pathological single "word" becoming a giant tag."""

# Metadata keys a captioning/vision adapter might use to surface labels.
_LABEL_KEYS = ("labels", "keywords", "tags")

# A compact English stop-word set. Kept inline (no NLTK dep) — FL-6 must not
# add a new runtime dependency just for tagging.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "any",
        "can",
        "her",
        "was",
        "one",
        "our",
        "out",
        "day",
        "get",
        "has",
        "him",
        "his",
        "how",
        "man",
        "new",
        "now",
        "old",
        "see",
        "two",
        "way",
        "who",
        "boy",
        "did",
        "its",
        "let",
        "put",
        "say",
        "she",
        "too",
        "use",
        "this",
        "that",
        "with",
        "from",
        "they",
        "have",
        "will",
        "your",
        "what",
        "when",
        "were",
        "there",
        "their",
        "which",
        "would",
        "about",
        "these",
        "those",
        "been",
        "into",
        "over",
        "then",
        "them",
        "than",
        "some",
        "such",
        "only",
        "also",
        "more",
        "most",
        "other",
        "here",
        "each",
        "very",
        "just",
        "like",
        "make",
        "page",
        "image",
        "file",
        "document",
        "text",
        "content",
        "upload",
    }
)


def _normalize(raw: str) -> str:
    """Lowercase, trim, collapse whitespace, strip surrounding punctuation."""
    s = raw.strip().lower()
    s = re.sub(r"\s+", " ", s)
    # Strip leading/trailing punctuation but keep intra-word hyphens.
    s = s.strip(" \t\r\n.,;:!?\"'()[]{}<>|/\\")
    return s


def _clean_tags(candidates: Iterable[str]) -> list[str]:
    """Normalize, filter, and dedup a candidate tag stream (order-preserving)."""
    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        if not isinstance(cand, str):
            continue
        tag = _normalize(cand)
        if not tag or len(tag) < _MIN_TOKEN_LEN or len(tag) > _MAX_TAG_LEN:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _labels_from_metadata(metadata: dict) -> list[str]:
    """Pull explicit candidate labels an adapter may have surfaced.

    Accepts a list under any of ``labels`` / ``keywords`` / ``tags``. A
    comma/newline-delimited string is also tolerated (some adapters return a
    flat string). Returns cleaned tags — empty when nothing usable is present.
    """
    if not isinstance(metadata, dict):
        return []
    collected: list[str] = []
    for key in _LABEL_KEYS:
        val = metadata.get(key)
        if isinstance(val, str):
            collected.extend(re.split(r"[,\n;]+", val))
        elif isinstance(val, (list, tuple, set)):
            collected.extend(str(v) for v in val)
    return _clean_tags(collected)


def _keywords_from_text(*parts: str, limit: int) -> list[str]:
    """Frequency-rank single-word keywords across the given text parts.

    Dependency-free: tokenize on word boundaries, drop stop-words and short
    tokens, count, and return the most common up to ``limit``. Ties keep the
    first-seen order (Counter.most_common is stable in CPython 3.7+).
    """
    counts: Counter[str] = Counter()
    for part in parts:
        if not part:
            continue
        for match in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", part):
            token = match.lower().strip("-")
            if len(token) < _MIN_TOKEN_LEN or len(token) > _MAX_TAG_LEN:
                continue
            if token in _STOPWORDS:
                continue
            counts[token] += 1
    return [word for word, _n in counts.most_common(limit)]


def derive_tags(
    *,
    title: str | None,
    captions: Iterable[str] | None,
    text: str | None,
    metadata: dict | None = None,
    limit: int = MAX_TAGS,
) -> list[str]:
    """Derive up to ``limit`` free-form tags from extraction output.

    Precedence:
      1. Explicit adapter-supplied labels in ``metadata`` (if any) come first.
      2. The detected ``title`` contributes its words next (a title is the
         strongest single signal a document carries).
      3. Frequency-ranked keywords from captions + text fill the rest.

    Everything is normalized (lowercase/trimmed), deduped, and capped at
    ``limit``. Returns ``[]`` when there's nothing usable — the caller then
    skips the metadata write entirely.
    """
    limit = max(0, limit)
    if limit == 0:
        return []

    caption_list = list(captions or [])
    caption_blob = " ".join(c for c in caption_list if isinstance(c, str))

    ordered: list[str] = []

    # 1. Adapter labels win — they're the highest-signal candidates.
    ordered.extend(_labels_from_metadata(metadata or {}))

    # 2. Title words.
    if title:
        ordered.extend(_keywords_from_text(title, limit=limit))

    # 3. Keyword fill from captions + text.
    ordered.extend(_keywords_from_text(caption_blob, text or "", limit=limit * 2))

    return _clean_tags(ordered)[:limit]


def merge_tags(existing: Iterable[str] | None, derived: Iterable[str]) -> list[str]:
    """Union existing (user) tags with newly derived ones — existing first.

    Re-indexing a file must never drop a tag a user typed. Existing tags keep
    their original order and win on collision; derived tags are appended in
    order, skipping any already present after normalization. The result is
    capped at :data:`MAX_TAGS`.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tag in _clean_tags(existing or []):
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    for tag in _clean_tags(derived):
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out[:MAX_TAGS]


__all__ = ["derive_tags", "merge_tags", "MAX_TAGS"]
