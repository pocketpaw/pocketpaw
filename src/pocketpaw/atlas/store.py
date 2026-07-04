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
# Updated: 2026-07-02 (feat/atlas-compiler, AT-4) — the data file is now the
# COMPILED artifact (authored primitives/surfaces + extracted connector and
# sense entries; see ``atlas/compile.py``). New lightweight startup drift
# check: the first ``get_atlas_store()`` call compares the artifact's
# ``connector:*`` name set against the live connector YAML scan (the same
# dirs ConnectorRegistry reads) and logs a WARNING on mismatch — name-set
# compare only, no recompile, never raises.
# Updated: 2026-07-02 (feat/atlas-overlay, AT-5) — additive ``search_scored``
# (``search`` now delegates to it): returns ``(score, entry)`` pairs so the
# overlay (``atlas/overlay.py``) can re-rank available connectors above
# unavailable ones at equal relevance without touching the base scoring.
# Store API otherwise unchanged; the primer and drift check keep the
# unfiltered OS-level view.
# Updated: 2026-07-02 (feat/atlas-widgets, AT-6) — cheap deterministic suffix
# normalizer (``_stem``: strip ing/ed/es/s then a trailing e, 3+ char stems,
# no external deps) applied to BOTH index and query tokens, so plural /
# inflected query words match singular keywords ("competitors" now hits a
# "competitor" keyword). Field weights and the search_scored / search /
# describe signatures are unchanged.
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-3) — a tiny function-word
# stoplist (``_STOPWORDS``) is dropped by ``_tokenize`` from both index and
# query tokens. Without it, an entry whose NAME is a natural phrase (the WA-3
# admin-capability cards, e.g. "Change a member's role") scored a full
# name-weight hit on a query's bare "a"/"the"/"make", letting admin cards hijack
# unrelated intents. Stopwords carry no intent signal, so removing them can only
# drop spurious matches — no entry is ever the right answer *because of* a
# stopword. Weights and signatures unchanged.

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pocketpaw.atlas.model import AtlasEntry, AtlasModel

logger = logging.getLogger(__name__)

# The compiled artifact ships inside the package (hatchling includes
# non-Python files under src/pocketpaw in the wheel). Built by
# ``pocketpaw atlas build`` from atlas/authored/ + the connector YAMLs.
_DATA_PATH = Path(__file__).parent / "data" / "atlas.json"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function-word stoplist (WA-3). These carry no intent signal — an entry whose
# NAME is a natural phrase ("Change a member's role") would otherwise score a
# full name-weight hit on a query's bare "a", letting admin-capability cards
# hijack unrelated intents ("make a landing page"). Dropped identically from
# index and query tokens (via ``_stem_set`` → ``_tokenize``), so this can only
# REMOVE spurious matches, never break a real one: no atlas entry is ever the
# right answer *because of* a stopword. Kept deliberately tiny (closed-class
# articles / prepositions / pronouns / auxiliaries + a couple of ubiquitous
# instruction verbs) so it never swallows a content word.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "us",
        "you",
        "your",
        "they",
        "them",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "into",
        "and",
        "or",
        "but",
        "as",
        "so",
        "if",
        "then",
        "is",
        "are",
        "be",
        "been",
        "am",
        "was",
        "were",
        "do",
        "does",
        "did",
        "can",
        "will",
        "would",
        "should",
        "could",
        "make",
        "let",
        "want",
        "need",
        "please",
        "just",
    }
)

# Score weights: a hit on the name or a keyword is a much stronger signal
# of intent than a word that merely appears somewhere in the narrative.
_NAME_WEIGHT = 5.0
_KEYWORD_WEIGHT = 3.0
_SUMMARY_WEIGHT = 1.5
_NARRATIVE_WEIGHT = 1.0


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _stem(token: str) -> str:
    """Cheap deterministic suffix normalizer (AT-6) — no external deps.

    Fixes the plural/inflection miss class the atlas eval documented
    ("competitors" query token never matched a "competitor" keyword).
    Exact rules, applied to an already-lowercased token and REPEATED to a
    fixpoint (so "meetings" → "meeting" → "meet" and a bare "meeting"
    reach the SAME stem — a single pass left them unequal):

    1. ``ies`` → ``y`` when the stem keeps >= 3 chars ("companies" →
       "company").
    2. Strip a trailing ``s`` (but never ``ss``/``us``/``is`` — "less",
       "status", "analysis" stay put) when the stem keeps >= 3 chars.
    3. Strip ``ing`` when the stem keeps >= 4 chars ("meeting" → "meet").
    4. Strip ``ed`` when the stem keeps >= 4 chars ("connected" →
       "connect").

    Deliberately NO trailing-``e`` strip: it collided real vocabulary
    ("state" → "stat" hit widget:stat at name weight, "notes" → "not",
    "sites" → "sit"). Applied identically to index and query tokens, so
    equal tokens always keep matching; this only ADDS matches between
    inflected forms. Intentionally not a full stemmer (approve/approved
    still differ) — cheap, deterministic, collision-averse.
    """
    while True:
        before = token
        if token.endswith("ies") and len(token) - 3 >= 3:
            token = token[:-3] + "y"
        elif token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) - 1 >= 3:
            token = token[:-1]
        elif token.endswith("ing") and len(token) - 3 >= 4:
            token = token[:-3]
        elif token.endswith("ed") and len(token) - 2 >= 4:
            token = token[:-2]
        if token == before:
            return token


def _stem_set(text: str) -> set[str]:
    """Tokenize *text* and normalize every token to its stem."""
    return {_stem(token) for token in _tokenize(text)}


class AtlasStore:
    """In-memory view over the atlas self-model with lexical search."""

    def __init__(self, model: AtlasModel) -> None:
        self._model = model
        self._by_id: dict[str, AtlasEntry] = {e.id: e for e in model.entries}
        # Pre-tokenized fields per entry, so search doesn't re-tokenize the
        # narrative on every call. Tokens are stem-normalized (``_stem``,
        # AT-6) so inflected query words match singular index words.
        self._index: list[tuple[AtlasEntry, set[str], set[str], set[str], set[str]]] = [
            (
                entry,
                _stem_set(entry.name),
                _stem_set(" ".join(entry.keywords)),
                _stem_set(entry.summary),
                _stem_set(entry.narrative),
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
        if limit <= 0:
            return []
        return [entry for _, entry in self.search_scored(query, limit=limit)]

    def search_scored(self, query: str, limit: int | None = None) -> list[tuple[float, AtlasEntry]]:
        """Like :meth:`search` but returns ``(score, entry)`` pairs.

        Added for the overlay (AT-5), which needs the base relevance score
        to re-rank available connectors above unavailable ones WITHOUT
        changing this scoring. ``limit=None`` returns every match so the
        overlay can filter before truncating.
        """
        # Query tokens go through the same stem normalizer as the index
        # (AT-6): "competitors" scores against a "competitor" keyword. Two
        # query words that share a stem collapse into one scoring token.
        tokens = _stem_set(query)
        if not tokens:
            return []

        scored: list[tuple[float, AtlasEntry]] = []
        for entry, name_t, keyword_t, summary_t, narrative_t in self._index:
            score = 0.0
            for token in tokens:
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
        return scored if limit is None else scored[:limit]

    def describe(self, entry_id: str) -> AtlasEntry | None:
        """Return the full entry for a stable id, or None if unknown."""
        return self._by_id.get(entry_id)


def _scan_live_connector_names() -> set[str]:
    """Names of connector definitions visible to the live registry.

    Mirrors ``ConnectorRegistry._scan``'s directories (home dir +
    CWD ``connectors/``) but reads only the YAML ``name:`` field — no
    adapters, no state store, no registry construction. Cheap enough to
    run once at first store load.
    """
    import yaml

    from pocketpaw.connectors.registry import _default_home_connectors_dir

    names: set[str] = set()
    for directory in (_default_home_connectors_dir(), Path("connectors")):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                name = raw.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
            except Exception:  # noqa: BLE001 — malformed YAML never breaks the check
                continue
    return names


def check_connector_drift(store: AtlasStore, live_names: set[str] | None = None) -> bool:
    """Warn (never raise) when the compiled artifact's connector set is stale.

    Compares the ``connector:*`` ids in the artifact against the live
    connector name set (scanned from the YAML dirs when not passed in).
    Returns True when drift was detected and a WARNING was logged. Kept
    deliberately cheap: a name-set compare, no recompile.
    """
    try:
        atlas_names = {e.id.split(":", 1)[1] for e in store.entries if e.kind == "connector"}
        if live_names is None:
            live_names = _scan_live_connector_names()
        if atlas_names == live_names:
            return False
        if not live_names:
            # No connector YAMLs visible from this process (installed wheel,
            # CWD outside the repo). That's an environment fact, not a stale
            # artifact — rebuilding wouldn't change anything. Stay quiet.
            logger.debug(
                "atlas connector drift check skipped: no live connector definitions visible"
            )
            return False
        missing = sorted(live_names - atlas_names)
        extra = sorted(atlas_names - live_names)
        logger.warning(
            "atlas is stale — run `pocketpaw atlas build` "
            "(connectors missing from atlas: %s; in atlas but not live: %s)",
            ", ".join(missing) or "none",
            ", ".join(extra) or "none",
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the drift check must never raise
        logger.debug("atlas connector drift check skipped: %s", exc)
        return False


_store: AtlasStore | None = None


def get_atlas_store() -> AtlasStore:
    """Module-level lazy singleton — one parsed model per process.

    The first load also runs the connector drift check (WARNING-only,
    never raises) so a stale compiled artifact is visible in the logs of
    whichever process serves atlas first (MCP server, primer build, CLI).
    """
    global _store
    if _store is None:
        _store = AtlasStore.load()
        check_connector_drift(_store)
    return _store


__all__ = ["AtlasStore", "check_connector_drift", "get_atlas_store"]
