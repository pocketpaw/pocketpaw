# atlas/store.py — loader + lexical search + describe for the atlas
# OS self-model (AT-1). Created: 2026-07-02 (feat/atlas-core).
# ``AtlasStore`` loads and validates the packaged seed
# (``atlas/data/atlas.json``) against the paw.atlas/v1 pydantic schema,
# ranks entries for an intent query with simple token-overlap scoring
# (name/keyword hits weighted above summary/narrative — no external
# deps), and returns a full entry by id. A module-level lazy singleton
# (``get_atlas_store``) mirrors the repo's other registry getters so the
# MCP tools and the context_builder primer block share one parsed model.
# Updated: 2026-07-02 (feat/atlas-surface, AT-3) — no code change; the seed
# now also carries kind="surface" entries (frontend routes), which search
# and describe serve exactly like primitives.

from __future__ import annotations

import json
import re
from pathlib import Path

from pocketpaw.atlas.model import AtlasEntry, AtlasModel

# The hand-authored seed ships inside the package (hatchling includes
# non-Python files under src/pocketpaw in the wheel).
_DATA_PATH = Path(__file__).parent / "data" / "atlas.json"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Score weights: a hit on the name or a keyword is a much stronger signal
# of intent than a word that merely appears somewhere in the narrative.
_NAME_WEIGHT = 5.0
_KEYWORD_WEIGHT = 3.0
_SUMMARY_WEIGHT = 1.5
_NARRATIVE_WEIGHT = 1.0


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class AtlasStore:
    """In-memory view over the atlas self-model with lexical search."""

    def __init__(self, model: AtlasModel) -> None:
        self._model = model
        self._by_id: dict[str, AtlasEntry] = {e.id: e for e in model.entries}
        # Pre-tokenized fields per entry, so search doesn't re-tokenize the
        # narrative on every call.
        self._index: list[tuple[AtlasEntry, set[str], set[str], set[str], set[str]]] = [
            (
                entry,
                set(_tokenize(entry.name)),
                set(_tokenize(" ".join(entry.keywords))),
                set(_tokenize(entry.summary)),
                set(_tokenize(entry.narrative)),
            )
            for entry in model.entries
        ]

    @classmethod
    def load(cls, path: Path | None = None) -> AtlasStore:
        """Load and validate the seed JSON (packaged data file by default)."""
        data_path = path or _DATA_PATH
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        return cls(AtlasModel.model_validate(raw))

    @property
    def model(self) -> AtlasModel:
        return self._model

    @property
    def entries(self) -> list[AtlasEntry]:
        return list(self._model.entries)

    def search(self, query: str, limit: int = 5) -> list[AtlasEntry]:
        """Rank entries for an intent query by weighted token overlap.

        Each query token scores per entry: name hit > keyword hit >
        summary hit > narrative hit (a token counts once, at its best
        field). Entries with zero overlap are dropped; ties keep seed
        order (stable sort).
        """
        tokens = _tokenize(query)
        if not tokens or limit <= 0:
            return []

        scored: list[tuple[float, AtlasEntry]] = []
        for entry, name_t, keyword_t, summary_t, narrative_t in self._index:
            score = 0.0
            for token in set(tokens):
                if token in name_t:
                    score += _NAME_WEIGHT
                elif token in keyword_t:
                    score += _KEYWORD_WEIGHT
                elif token in summary_t:
                    score += _SUMMARY_WEIGHT
                elif token in narrative_t:
                    score += _NARRATIVE_WEIGHT
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def describe(self, entry_id: str) -> AtlasEntry | None:
        """Return the full entry for a stable id, or None if unknown."""
        return self._by_id.get(entry_id)


_store: AtlasStore | None = None


def get_atlas_store() -> AtlasStore:
    """Module-level lazy singleton — one parsed model per process."""
    global _store
    if _store is None:
        _store = AtlasStore.load()
    return _store


__all__ = ["AtlasStore", "get_atlas_store"]
