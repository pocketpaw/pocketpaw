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
# Updated: 2026-07-05 (fix/atlas-data-accuracy-and-relevance) — two relevance
# fixes for the "agent misdirects users about the OS" class:
#   * five more instruction-filler stopwords ("up", "set", "get", "open",
#     "show") join ``_STOPWORDS``. They hit entry NAMES with no intent signal —
#     "set up memory" scored "set" against the approval-level card's name and
#     "up" against widget:follow-up, burying primitive:soul. Same closed-class
#     rationale as WA-3: dropping them can only remove spurious name hits.
#   * an IDF-style NAME-weight damper (``_name_idf_weight``). Generic tokens
#     that appear in MANY entry names ("workspace" in 8 names, "connector" in 5)
#     handed every card a full 5.0 name hit, so a benign "manage workspace users"
#     produced an 8-way tie that seed order broke — sometimes landing a
#     destructive card (Delete the workspace) above a benign one. The damper
#     scales ONLY the name-field hit by the token's smoothed inverse name
#     document frequency, normalized so a token in a single name keeps the full
#     ``_NAME_WEIGHT`` and a ubiquitous token is damped toward the keyword tier.
#     Keyword / summary / narrative weights are untouched (those fields carry
#     deliberate, per-entry intent vocabulary), so a discriminating keyword now
#     out-scores a generic name collision. Signatures unchanged.
# Updated: 2026-07-05 (fix/atlas-relevance-round2) — a kind-priority bias so a
# governing primitive can't be outranked by a management SURFACE (or an admin
# capability) at equal keyword overlap. Governance paraphrases ("gate the
# agent", "sign-off", "approve what the agent does") were landing on the
# /agents list route (surface:agents), dropping primitive:instinct below it.
# Two small levers in ``search_scored``: (1) ``_KIND_SCALE`` multiplies every
# non-primitive score by 0.96 (primitive = 1.0) — the largest damping that
# leaves the eval strict-hit baseline untouched; (2) ``_KIND_TIEBREAK`` is a
# deterministic secondary sort key (primitive first) so an exact tie resolves
# toward the primitive, not seed order. Both are near-tie only — a match that
# out-scores the primitive by a real margin still wins. Field weights and the
# search_scored / search / describe signatures are unchanged.

from __future__ import annotations

import json
import logging
import math
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
        "up",
        "set",
        "get",
        "open",
        "show",
    }
)

# Score weights: a hit on the name or a keyword is a much stronger signal
# of intent than a word that merely appears somewhere in the narrative.
_NAME_WEIGHT = 5.0
_KEYWORD_WEIGHT = 3.0
_SUMMARY_WEIGHT = 1.5
_NARRATIVE_WEIGHT = 1.0

# Floor for the IDF name-weight damper: even a token that appears in EVERY
# entry name still contributes at least this fraction of ``_NAME_WEIGHT`` when
# it hits a name, so the damper only re-ranks — it never zeroes a real match.
# 0.6 keeps a fully-generic name hit (0.6 × 5.0 = 3.0) at the keyword tier, so
# a discriminating keyword can tie/beat a generic name collision but a name hit
# is never worth less than a keyword hit.
_NAME_IDF_FLOOR = 0.6

# Kind-priority bias (relevance fix, round 2). Governance paraphrases ("gate the
# agent", "sign-off") kept steering to the /agents management LIST route
# (kind='surface') or an admin capability card, dropping the GOVERNING primitive
# (Instinct) below them at equal-ish overlap. Two complementary levers, both
# small on purpose so they only re-rank near-ties and never overpower a real
# margin:
#   * ``_KIND_SCALE`` multiplies a non-primitive's score by 0.96 (primitive =
#     1.0), so at the SAME keyword overlap the governing primitive edges out a
#     surface / capability / connector. 0.96 was picked as the largest damping
#     that leaves the intent→capability eval strict-hit baseline untouched
#     (measured: 0.96 keeps 30/31; 0.95 and below start to regress genuine
#     specific-kind answers like widget:kanban).
#   * ``_KIND_TIEBREAK`` is a deterministic secondary sort key (primitive > sense
#     > capability > connector > widget > skill > surface) so an EXACT numeric
#     tie always resolves toward the primitive instead of falling to seed order.
# Neither lever can flip a match that outscores the primitive by a real margin
# (e.g. the owner-only approval-LEVEL capability still legitimately co-answers
# "approval gate") — the primitive is protected on ties and near-ties only.
_KIND_SCALE: dict[str, float] = {"primitive": 1.0}
_KIND_SCALE_DEFAULT = 0.96
_KIND_TIEBREAK: dict[str, int] = {
    "primitive": 6,
    "sense": 5,
    "capability": 4,
    "connector": 3,
    "widget": 2,
    "skill": 1,
    "surface": 0,
}


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
        # Name-field IDF weights (relevance fix, 2026-07-05). A generic token
        # that shows up in many entry NAMES ("workspace", "connector") carries
        # little discriminating signal, yet each name hit scored the full
        # ``_NAME_WEIGHT`` — so a benign query tied a swarm of cards and seed
        # order (sometimes a destructive card) decided the winner. Precompute a
        # per-name-token damper from the smoothed inverse name document
        # frequency, normalized so a token in exactly one name keeps 1.0 and
        # ubiquitous tokens drop toward ``_NAME_IDF_FLOOR``. Applied ONLY to the
        # name field; keyword/summary/narrative weights are unchanged.
        self._name_idf: dict[str, float] = self._compute_name_idf()

    def _compute_name_idf(self) -> dict[str, float]:
        """Map each name-field stem to a damper in ``[_NAME_IDF_FLOOR, 1.0]``.

        Smoothed IDF over entry NAMES: ``log((N + 1) / (df + 1))``, normalized
        against the df=1 value so a token appearing in a single name keeps the
        full ``_NAME_WEIGHT`` and a token in many names is damped (floored so a
        name hit is never worth less than a keyword hit).
        """
        n_entries = len(self._index)
        name_df: dict[str, int] = {}
        for _entry, name_t, _kw, _sm, _nr in self._index:
            for token in name_t:
                name_df[token] = name_df.get(token, 0) + 1

        # df=1 is the reference (a name-unique token → full weight).
        ref_idf = math.log((n_entries + 1) / (1 + 1)) if n_entries else 1.0
        if ref_idf <= 0:  # degenerate tiny model — no damping
            return {token: 1.0 for token in name_df}

        weights: dict[str, float] = {}
        for token, df in name_df.items():
            idf = math.log((n_entries + 1) / (df + 1))
            weights[token] = max(_NAME_IDF_FLOOR, min(1.0, idf / ref_idf))
        return weights

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
                    # Damp the name hit by the token's name-IDF (relevance fix,
                    # 2026-07-05): a generic token in many names is worth less
                    # than a name-unique one, so a discriminating keyword can
                    # out-score a ubiquitous name collision.
                    score += _NAME_WEIGHT * self._name_idf.get(token, 1.0)
                elif token in keyword_t:
                    score += _KEYWORD_WEIGHT
                elif token in summary_t:
                    score += _SUMMARY_WEIGHT
                elif token in narrative_t:
                    score += _NARRATIVE_WEIGHT
            if score > 0:
                # Kind-priority scale (relevance fix, round 2): a non-primitive
                # is damped slightly so the governing primitive edges out a
                # surface / capability at equal overlap. Small enough to leave a
                # real margin untouched.
                score *= _KIND_SCALE.get(entry.kind, _KIND_SCALE_DEFAULT)
                scored.append((score, entry))

        # Primary key: score (desc). Secondary: kind priority (desc) so an EXACT
        # tie resolves toward the primitive instead of seed order — the
        # ``atlas_search steered to /agents instead of Instinct`` class of miss.
        scored.sort(
            key=lambda pair: (pair[0], _KIND_TIEBREAK.get(pair[1].kind, 0)),
            reverse=True,
        )
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
