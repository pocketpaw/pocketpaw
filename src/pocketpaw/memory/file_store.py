# File-based memory store implementation.
# Created: 2026-02-02 - Memory System
# Updated: 2026-02-09 - Fixed UUID collision, daily file loading, search, persistent delete
# Updated: 2026-02-10 - Session index for fast listing, delete/rename support
#
# Stores memories as markdown files for human readability:
# - ~/.pocketpaw/memory/MEMORY.md     (long-term)
# - ~/.pocketpaw/memory/2026-02-02.md (daily)
# - ~/.pocketpaw/memory/sessions/     (session JSON files)
# - ~/.pocketpaw/memory/sessions/_index.json (session metadata index)

import asyncio
import hashlib
import html
import importlib
import json
import logging
import math
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from pocketpaw._compat import require_extra
from pocketpaw.memory.protocol import MemoryEntry, MemoryType

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# Stop words excluded from word-overlap search scoring
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "about",
        "like",
        "through",
        "after",
        "over",
        "between",
        "out",
        "against",
        "during",
        "without",
        "before",
        "under",
        "around",
        "among",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "because",
        "if",
        "when",
        "where",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "it",
        "its",
        "they",
        "them",
        "their",
    }
)

# Knowledge graph extraction is regex + heuristics only (no LLM dependency here).
# Pipeline:
# 1) extract candidate entities/relations via conservative patterns
# 2) canonicalize entity names and normalize relation types
# 3) compute confidence and keep only edges above threshold

# Technology terms for entity extraction (lowercase for matching)
_GRAPH_TECH_TERMS = frozenset(
    {
        "python",
        "java",
        "javascript",
        "typescript",
        "react",
        "node",
        "fastapi",
        "flask",
        "django",
        "postgres",
        "postgresql",
        "mysql",
        "sqlite",
        "redis",
        "mongodb",
        "docker",
        "kubernetes",
        "ollama",
        "anthropic",
        "openai",
        "qdrant",
        "chromadb",
        "mem0",
        "pocketpaw",
    }
)

# Blacklist for filtering out junk entities from regex extraction
# These are common words that produce false-positive relationships
_GRAPH_ENTITY_BLACKLIST = frozenset(
    {
        # Generic nouns that appear in conversational text
        "something",
        "anything",
        "everything",
        "nothing",
        "question",
        "questions",
        "answer",
        "answers",
        "thing",
        "things",
        "stuff",
        "idea",
        "ideas",
        "thought",
        "thoughts",
        "point",
        "points",
        "part",
        "parts",
        "bit",
        "piece",
        "way",
        "ways",
        "kind",
        "kinds",
        "sort",
        "sorts",
        "type",
        "types",
        "example",
        "examples",
        "case",
        "cases",
        "time",
        "times",
        "day",
        "days",
        "moment",
        "moment",
        "second",
        "minute",
        "hour",
        "problem",
        "problems",
        "issue",
        "issues",
        "error",
        "errors",
        "result",
        "results",
        "output",
        "input",
        # Pronouns and vague references
        "it",
        "this",
        "that",
        "these",
        "those",
        "they",
        "them",
        "one",
        "ones",
        "someone",
        "somebody",
        "everyone",
        "everybody",
        "anyone",
        "anybody",
        "no one",
        "nobody",
        # Temporal states (produce false "has" relationships)
        "meeting",
        "call",
        "appointment",
        "schedule",
        "plan",
        "deadline",
        # Action nouns that aren't entities
        "look",
        "check",
        "try",
        "attempt",
        "go",
        "start",
        "begin",
        "end",
        "finish",
        "stop",
    }
)

# Maximum word count for extracted entities (longer = likely sentence fragment)
_GRAPH_MAX_ENTITY_WORDS = 6
_GRAPH_RELATION_CONFIDENCE_THRESHOLD = 0.75

# Controlled relation schema for graph edges.
_GRAPH_RELATION_SCHEMA = frozenset(
    {
        "uses",
        "depends_on",
        "built_on",
        "is_a",
        "part_of",
        "implements",
        "extends",
        "calls",
    }
)

_RELATION_NORMALIZATION_MAP = {
    "depends": "depends_on",
    "depends_on": "depends_on",
    "built on": "built_on",
    "built_on": "built_on",
    "kind_of": "is_a",
    "type_of": "is_a",
    "is_a": "is_a",
    "part_of": "part_of",
    "implements": "implements",
    "inherits_from": "extends",
    "extends": "extends",
    "invokes": "calls",
    "calls": "calls",
    "uses": "uses",
}

_TECH_CANONICAL_NAMES = {
    "python": "Python",
    "java": "Java",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "react": "React",
    "node": "Node",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "postgres": "Postgres",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "sqlite": "SQLite",
    "redis": "Redis",
    "mongodb": "MongoDB",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "ollama": "Ollama",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "qdrant": "Qdrant",
    "chromadb": "ChromaDB",
    "mem0": "Mem0",
    "pocketpaw": "PocketPaw",
}

# Conservative regex patterns for relationship extraction.
# These require specific structural cues to reduce false positives.
# Patterns like "X has Y" are intentionally EXCLUDED due to high false-positive rate.
_RELATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "X uses Y" - requires capitalized or tech-term entities
    # Example: "Project Phoenix uses PostgreSQL" ✓
    # Example: "The user has a question" ✗ (no match - "has" not in patterns)
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+uses\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "uses",
    ),
    # "X depends on Y" - explicit dependency language
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+depends\s+on\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "depends_on",
    ),
    # "X is built on Y" - technical stack relationship
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+is\s+built\s+on\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "built_on",
    ),
    # "X is a type of Y" / "X is an example of Y" - taxonomic
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+is\s+(?:a|an)\s+"
            r"(?:type\s+of|kind\s+of|example\s+of)\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "is_a",
    ),
    # "X is part of Y" - compositional
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+is\s+part\s+of\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "part_of",
    ),
    # "X implements Y" - interface/protocol relationship
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+implements\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "implements",
    ),
    # "X extends Y" / "X inherits from Y" - inheritance
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+"
            r"(?:extends|inherits\s+from)\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "extends",
    ),
    # "X calls Y" / "X invokes Y" - API/method relationship
    (
        re.compile(
            r"(?P<src>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})\s+(?:calls|invokes)\s+"
            r"(?P<tgt>[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2})",
            re.IGNORECASE,
        ),
        "calls",
    ),
]

_VECTOR_SCHEMA_VERSION = 1


def _make_deterministic_id(path: Path, header: str, body: str) -> str:
    """Generate a deterministic UUID5 from path, header, AND body content."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{path}:{header}:{body}"))


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alpha, strip stop words."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return words - _STOP_WORDS


class FileMemoryStore:
    """
    File-based memory store.

    Human-readable markdown for long-term and daily memories.
    JSON for session memories (machine-readable).
    """

    def __init__(
        self,
        base_path: Path | None = None,
        *,
        vector_enabled: bool = False,
        vector_store: str = "sqlite-vec",
        embedding_model: str = "nomic-embed-text",
        embedding_provider: str = "ollama",
        embedding_base_url: str = "http://localhost:11434",
    ):
        self.base_path = base_path or (Path.home() / ".pocketpaw" / "memory")
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Sub-directories
        self.sessions_path = self.base_path / "sessions"
        self.sessions_path.mkdir(exist_ok=True)

        # File paths
        self.long_term_file = self.base_path / "MEMORY.md"

        # In-memory index for fast lookup
        self._index: dict[str, MemoryEntry] = {}
        self._session_write_locks: dict[str, asyncio.Lock] = {}
        self._session_index_lock = asyncio.Lock()  # Protects _index.json read-modify-write
        self._alias_lock = asyncio.Lock()  # Protects _aliases.json read-modify-write

        # Vector-backed semantic memory (phase 1)
        self._vector_enabled = vector_enabled
        self._vector_store = vector_store.strip().lower()
        self._embedding_model = embedding_model
        self._embedding_provider = embedding_provider.strip().lower()
        self._embedding_base_url = embedding_base_url
        self._vector_lock = asyncio.Lock()
        self._vector_db_path = self.base_path / "vector_index.sqlite3"
        self._vector_backend = "none"
        self._chroma_collection = None

        # Knowledge graph (phase 2)
        # NOTE: Graph extraction uses conservative regex patterns with heuristic
        # filtering to minimize false positives. See _extract_graph_signals() docs.
        # Tied to vector_enabled - only active when semantic features are enabled.
        self._graph_enabled = vector_enabled
        self._graph_db_path = self.base_path / "knowledge_graph.sqlite3"
        self._graph_lock = asyncio.Lock()

        # Inverted index for O(k) search narrowing (word -> set of entry IDs)
        self._inverted: dict[str, set[str]] = {}
        self._inv_dirty = True

        self._initialize_vector_backend()
        if self._graph_enabled:
            self._initialize_graph_store()

        self._load_index()

        # Build session index on first run (migration)
        if not self._index_path.exists():
            self.rebuild_session_index()

    def _initialize_vector_backend(self) -> None:
        """Initialize vector backend for semantic retrieval.

        Keeps markdown storage as source of truth and augments it with vector search.
        """
        if not self._vector_enabled:
            self._vector_backend = "disabled"
            return

        if self._vector_store == "chromadb":
            try:
                chromadb = importlib.import_module("chromadb")

                chroma_path = self.base_path / "chroma_db"
                chroma_path.mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(chroma_path))
                self._chroma_collection = client.get_or_create_collection(
                    name="pocketpaw_file_memory"
                )
                self._vector_backend = "chromadb"
                return
            except ImportError:
                logger.warning(
                    "vector_store=chromadb requested but chromadb is not installed; "
                    "falling back to sqlite-vec style local index"
                )

        if self._vector_store == "qdrant":
            logger.warning(
                "vector_store=qdrant requested for file backend; "
                "using sqlite-vec style local index for now"
            )

        self._initialize_sqlite_vector_store()
        self._vector_backend = "sqlite-vec"

    def _initialize_sqlite_vector_store(self) -> None:
        """Initialize and migrate local sqlite vector schema."""
        with sqlite3.connect(self._vector_db_path) as conn:
            current_version = self._get_sqlite_user_version(conn)
            self._migrate_vector_schema(conn, current_version, _VECTOR_SCHEMA_VERSION)

    @staticmethod
    def _get_sqlite_user_version(conn: sqlite3.Connection) -> int:
        """Read sqlite schema version from PRAGMA user_version."""
        row = conn.execute("PRAGMA user_version").fetchone()
        if not row:
            return 0
        return int(row[0])

    def _migrate_vector_schema(
        self,
        conn: sqlite3.Connection,
        current_version: int,
        target_version: int,
    ) -> None:
        """Apply forward-only vector schema migrations up to target_version."""
        if current_version > target_version:
            logger.warning(
                "vector_index schema version %s is newer than supported %s",
                current_version,
                target_version,
            )
            return

        while current_version < target_version:
            next_version = current_version + 1
            migration_name = f"_migrate_vector_schema_v{current_version}_to_v{next_version}"
            migration = getattr(self, migration_name, None)
            if migration is None:
                raise RuntimeError(f"Missing vector schema migration: {migration_name}")

            migration(conn)
            conn.execute(f"PRAGMA user_version = {next_version}")
            current_version = next_version

    @staticmethod
    def _migrate_vector_schema_v0_to_v1(conn: sqlite3.Connection) -> None:
        """Initial vector schema for memory_vectors table and lookup indexes."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_vectors (
                doc_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                user_scope TEXT NOT NULL,
                session_key TEXT,
                role TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                embedding_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_vectors_user_scope ON memory_vectors(user_scope)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_vectors_type ON memory_vectors(memory_type)"
        )

    def _initialize_graph_store(self) -> None:
        """Initialize sqlite tables for lightweight knowledge graph."""
        with sqlite3.connect(self._graph_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    user_scope TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    UNIQUE(user_scope, entity_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationships (
                    relationship_id TEXT PRIMARY KEY,
                    user_scope TEXT NOT NULL,
                    source_entity_id TEXT NOT NULL,
                    target_entity_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    weight INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    UNIQUE(user_scope, source_entity_id, target_entity_id, relation_type)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entity_links (
                    memory_id TEXT NOT NULL,
                    user_scope TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    PRIMARY KEY(memory_id, entity_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationship_evidence (
                    relationship_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    PRIMARY KEY(relationship_id, memory_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_user_scope ON entities(user_scope)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_links_memory ON memory_entity_links(memory_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_links_scope ON memory_entity_links(user_scope)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_scope ON relationships(user_scope)")

    @staticmethod
    def _normalize_entity_key(name: str) -> str:
        """Normalize entity key for deduplicated graph storage."""
        compact = re.sub(r"\s+", " ", name).strip(" \t\n.,;:()[]{}\"'")
        return compact.lower()

    @staticmethod
    def _is_valid_entity_candidate(name: str) -> bool:
        """Heuristic filter: reject obvious junk entities.

        Filters out:
        - Blacklisted generic words (question, thing, something, etc.)
        - Entities that are too long (likely sentence fragments)
        - Pure lowercase entities (except tech terms handled separately)
        """
        if not name:
            return False

        normalized = name.lower().strip()

        # Check blacklist
        if normalized in _GRAPH_ENTITY_BLACKLIST:
            return False

        # Check word count (avoid sentence fragments)
        word_count = len(name.split())
        if word_count > _GRAPH_MAX_ENTITY_WORDS:
            return False

        # Require at least one alphabetic character
        if not any(c.isalpha() for c in name):
            return False

        return True

    @staticmethod
    def _is_valid_relationship_candidate(src: str, rel_type: str, tgt: str) -> bool:
        """Heuristic filter: reject low-quality relationship candidates.

        Filters out:
        - Self-referential relationships (A -> A)
        - Relationships with blacklisted entities
        - Very short entities (likely noise)
        """
        # No self-loops
        if src.lower().strip() == tgt.lower().strip():
            return False

        # Both entities must pass basic validation
        if not FileMemoryStore._is_valid_entity_candidate(src):
            return False
        if not FileMemoryStore._is_valid_entity_candidate(tgt):
            return False

        # Minimum length check (avoid single-character noise)
        if len(src.strip()) < 2 or len(tgt.strip()) < 2:
            return False

        return True

    @staticmethod
    def _canonicalize_entity_name(name: str) -> str:
        """Canonicalize an entity name for cleaner graph nodes."""
        compact = re.sub(r"\s+", " ", name).strip(" \t\n.,;:()[]{}\"'")
        if not compact:
            return ""

        lowered = compact.lower()
        if lowered.startswith("the "):
            compact = compact[4:].strip()
            lowered = compact.lower()
        elif lowered.startswith("a "):
            compact = compact[2:].strip()
            lowered = compact.lower()
        elif lowered.startswith("an "):
            compact = compact[3:].strip()
            lowered = compact.lower()

        if lowered in _TECH_CANONICAL_NAMES:
            return _TECH_CANONICAL_NAMES[lowered]

        return compact

    @staticmethod
    def _normalize_relation_type(relation: str) -> str:
        """Map a raw/extracted relation into the controlled relation schema."""
        normalized = relation.strip().lower().replace(" ", "_")
        normalized = _RELATION_NORMALIZATION_MAP.get(normalized, normalized)
        return normalized if normalized in _GRAPH_RELATION_SCHEMA else ""

    @staticmethod
    def _score_relationship_candidate(src: str, rel_type: str, tgt: str) -> float:
        """Score confidence for a relationship candidate in range [0.0, 1.0]."""
        relation_weight = {
            "depends_on": 0.90,
            "built_on": 0.88,
            "implements": 0.88,
            "extends": 0.87,
            "part_of": 0.86,
            "calls": 0.85,
            "uses": 0.82,
            "is_a": 0.80,
        }

        score = relation_weight.get(rel_type, 0.70)

        src_lower = src.lower()
        tgt_lower = tgt.lower()
        if src_lower in _GRAPH_TECH_TERMS:
            score += 0.08
        if tgt_lower in _GRAPH_TECH_TERMS:
            score += 0.08

        src_title = any(ch.isupper() for ch in src)
        tgt_title = any(ch.isupper() for ch in tgt)
        if src_title:
            score += 0.03
        if tgt_title:
            score += 0.03

        return max(0.0, min(1.0, score))

    def _extract_graph_signals(self, content: str) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Extract entities + simple relationships from conversational text.

        Uses a conservative hybrid approach:
        1. Technology term matching (high precision)
        2. Title-case entity detection (moderate precision)
        3. Restrictive regex patterns for explicit relationships
        4. Heuristic filtering to remove false positives

        NOTE: The "has" pattern is intentionally excluded due to massive
        false-positive rate (e.g., "The user has a question" -> user-has-question).
        Only patterns with clear semantic boundaries are used.

        Relationship edges are normalized to a controlled schema and gated by
        confidence threshold before they are persisted.
        """
        entities: set[str] = set()
        relationships: list[tuple[str, str, str]] = []

        # Technology entities (lowercase terms) - high precision
        for token in _tokenize(content):
            if token in _GRAPH_TECH_TERMS:
                entities.add(self._canonicalize_entity_name(token))

        # Title-case entities (e.g., Project Phoenix) - moderate precision
        # Require at least one capital letter to avoid matching common words
        for match in re.finditer(
            r"\b[A-Z][a-zA-Z0-9_\-]*(?:\s+[A-Z][a-zA-Z0-9_\-]*){0,2}\b",
            content,
        ):
            name = match.group(0).strip()
            if len(name) >= 3 and self._is_valid_entity_candidate(name):
                canonical_name = self._canonicalize_entity_name(name)
                if canonical_name:
                    entities.add(canonical_name)

        # Explicit relationships from conservative patterns
        for pattern, rel_type in _RELATION_PATTERNS:
            for match in pattern.finditer(content):
                src = match.group("src").strip()
                tgt = match.group("tgt").strip()
                if not src or not tgt:
                    continue

                src = self._canonicalize_entity_name(src)
                tgt = self._canonicalize_entity_name(tgt)
                normalized_rel = self._normalize_relation_type(rel_type)
                if not src or not tgt or not normalized_rel:
                    continue

                # Apply heuristic filtering
                if not self._is_valid_relationship_candidate(src, normalized_rel, tgt):
                    continue

                confidence = self._score_relationship_candidate(src, normalized_rel, tgt)
                if confidence < _GRAPH_RELATION_CONFIDENCE_THRESHOLD:
                    continue

                entities.add(src)
                entities.add(tgt)
                relationships.append((src, normalized_rel, tgt))

        # REMOVED: Weak fallback that connects arbitrary entities.
        # This produced too many false-positive "related_to" edges.
        # Only explicit patterns above are used for relationships.

        # Keep extraction bounded
        trimmed_entities = sorted(entities)[:12]
        trimmed_relationships = relationships[:24]
        return trimmed_entities, trimmed_relationships

    async def _delete_graph_for_memory(self, memory_id: str) -> None:
        """Remove graph evidence links for a memory and maintain counters."""
        if not self._graph_enabled:
            return
        async with self._graph_lock:
            await asyncio.to_thread(self._delete_graph_for_memory_sync, memory_id)

    def _delete_graph_for_memory_sync(self, memory_id: str) -> None:
        """Sync graph cleanup for a deleted/updated memory."""
        with sqlite3.connect(self._graph_db_path) as conn:
            links = conn.execute(
                "SELECT entity_id, user_scope FROM memory_entity_links WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
            conn.execute("DELETE FROM memory_entity_links WHERE memory_id = ?", (memory_id,))

            for entity_id, user_scope in links:
                conn.execute(
                    """
                    UPDATE entities
                    SET mention_count = CASE
                        WHEN mention_count > 0 THEN mention_count - 1
                        ELSE 0
                    END
                    WHERE entity_id = ? AND user_scope = ?
                    """,
                    (entity_id, user_scope),
                )

            rel_rows = conn.execute(
                "SELECT relationship_id FROM relationship_evidence WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
            conn.execute("DELETE FROM relationship_evidence WHERE memory_id = ?", (memory_id,))

            for (relationship_id,) in rel_rows:
                conn.execute(
                    """
                    UPDATE relationships
                    SET weight = CASE WHEN weight > 0 THEN weight - 1 ELSE 0 END
                    WHERE relationship_id = ?
                    """,
                    (relationship_id,),
                )

            conn.execute("DELETE FROM relationships WHERE weight <= 0")
            conn.execute(
                """
                DELETE FROM entities
                WHERE mention_count <= 0
                  AND entity_id NOT IN (SELECT entity_id FROM memory_entity_links)
                """
            )

    async def _index_graph_record(self, entry: MemoryEntry) -> None:
        """Index entry entities and relationships into lightweight graph store."""
        if not self._graph_enabled:
            return
        entities, relationships = self._extract_graph_signals(entry.content)
        if not entities and not relationships:
            return

        user_scope = self._entry_user_scope(entry)
        now_iso = _ensure_utc(entry.created_at).isoformat()

        async with self._graph_lock:
            await asyncio.to_thread(
                self._index_graph_record_sync,
                entry.id,
                user_scope,
                now_iso,
                entities,
                relationships,
            )

    def _index_graph_record_sync(
        self,
        memory_id: str,
        user_scope: str,
        now_iso: str,
        entities: list[str],
        relationships: list[tuple[str, str, str]],
    ) -> None:
        """Sync graph write path for entities/relationships."""
        with sqlite3.connect(self._graph_db_path) as conn:
            entity_id_map: dict[str, str] = {}

            for display_name in entities:
                entity_key = self._normalize_entity_key(display_name)
                if not entity_key:
                    continue
                entity_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_scope}:{entity_key}"))
                entity_id_map[entity_key] = entity_id

                conn.execute(
                    """
                    INSERT INTO entities (
                        entity_id, user_scope, entity_key, display_name,
                        mention_count, first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(user_scope, entity_key) DO UPDATE SET
                        mention_count = entities.mention_count + 1,
                        last_seen = excluded.last_seen
                    """,
                    (entity_id, user_scope, entity_key, display_name, now_iso, now_iso),
                )

                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_entity_links (memory_id, user_scope, entity_id)
                    VALUES (?, ?, ?)
                    """,
                    (memory_id, user_scope, entity_id),
                )

            for src_name, rel_type, tgt_name in relationships:
                src_key = self._normalize_entity_key(src_name)
                tgt_key = self._normalize_entity_key(tgt_name)
                src_id = entity_id_map.get(src_key)
                tgt_id = entity_id_map.get(tgt_key)
                if not src_id or not tgt_id or src_id == tgt_id:
                    continue

                relationship_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"{user_scope}:{src_id}:{rel_type}:{tgt_id}",
                    )
                )

                conn.execute(
                    """
                    INSERT OR IGNORE INTO relationships (
                        relationship_id, user_scope, source_entity_id,
                        target_entity_id, relation_type, weight, first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (relationship_id, user_scope, src_id, tgt_id, rel_type, now_iso, now_iso),
                )

                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO relationship_evidence (relationship_id, memory_id)
                    VALUES (?, ?)
                    """,
                    (relationship_id, memory_id),
                ).rowcount

                if inserted:
                    conn.execute(
                        """
                        UPDATE relationships
                        SET weight = weight + 1,
                            last_seen = ?
                        WHERE relationship_id = ?
                        """,
                        (now_iso, relationship_id),
                    )

    @staticmethod
    def _hash_embedding(text: str, dim: int = 384) -> list[float]:
        """Deterministic local embedding fallback (no network/deps)."""
        tokens = _tokenize(text)
        if not tokens:
            return [0.0] * dim

        vector = [0.0] * dim
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + (digest[5] / 255.0)
            vector[index] += sign * weight

        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 1e-12:
            return vector
        return [value / norm for value in vector]

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        """Cosine similarity for equal-length vectors."""
        if len(left) != len(right) or not left:
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=False))

    async def _embed_text(self, text: str) -> list[float]:
        """Embed text using configured provider, falling back to local hash embedding."""
        if self._embedding_provider == "ollama":
            try:
                import ollama

                client = ollama.AsyncClient(host=self._embedding_base_url)
                result = await client.embeddings(model=self._embedding_model, prompt=text)
                embedding = result.get("embedding") if isinstance(result, dict) else None
                if isinstance(embedding, list) and embedding:
                    return [float(value) for value in embedding]
            except Exception:
                logger.debug("Ollama embedding failed, using local hash embedding", exc_info=True)

        return self._hash_embedding(text)

    def _entry_user_scope(self, entry: MemoryEntry) -> str:
        """Resolve memory ownership scope for semantic retrieval."""
        metadata = entry.metadata or {}
        if entry.type == MemoryType.LONG_TERM:
            return str(metadata.get("user_id", "default"))
        if entry.type == MemoryType.SESSION:
            return str(metadata.get("sender_id", metadata.get("user_id", "default")))
        return "default"

    async def _upsert_vector_record(self, entry: MemoryEntry) -> None:
        """Insert or update vector record for a memory entry."""
        if not self._vector_enabled:
            return

        user_scope = self._entry_user_scope(entry)
        metadata_json = json.dumps(entry.metadata or {}, ensure_ascii=False)
        created_at_iso = _ensure_utc(entry.created_at).isoformat()

        if self._vector_backend == "chromadb" and self._chroma_collection is not None:
            chroma_meta = {
                "memory_type": entry.type.value,
                "user_scope": user_scope,
                "session_key": entry.session_key or "",
                "role": entry.role or "",
                "created_at": created_at_iso,
                "metadata_json": metadata_json,
            }

            await asyncio.to_thread(
                self._chroma_collection.upsert,
                ids=[entry.id],
                documents=[entry.content],
                metadatas=[chroma_meta],
            )
            return

        embedding = await self._embed_text(entry.content)

        async with self._vector_lock:
            await asyncio.to_thread(
                self._upsert_sqlite_vector_record,
                entry,
                user_scope,
                created_at_iso,
                metadata_json,
                embedding,
            )

    def _upsert_sqlite_vector_record(
        self,
        entry: MemoryEntry,
        user_scope: str,
        created_at_iso: str,
        metadata_json: str,
        embedding: list[float],
    ) -> None:
        """Upsert vector record into local sqlite vector table."""
        with sqlite3.connect(self._vector_db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_vectors (
                    doc_id, content, memory_type, user_scope, session_key,
                    role, created_at, metadata_json, embedding_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    content = excluded.content,
                    memory_type = excluded.memory_type,
                    user_scope = excluded.user_scope,
                    session_key = excluded.session_key,
                    role = excluded.role,
                    created_at = excluded.created_at,
                    metadata_json = excluded.metadata_json,
                    embedding_json = excluded.embedding_json
                """,
                (
                    entry.id,
                    entry.content,
                    entry.type.value,
                    user_scope,
                    entry.session_key,
                    entry.role,
                    created_at_iso,
                    metadata_json,
                    json.dumps(embedding),
                ),
            )

    async def _delete_vector_record(self, entry_id: str) -> None:
        """Delete a vector record by entry ID."""
        if not self._vector_enabled:
            return

        if self._vector_backend == "chromadb" and self._chroma_collection is not None:
            await asyncio.to_thread(self._chroma_collection.delete, ids=[entry_id])
            return

        async with self._vector_lock:
            await asyncio.to_thread(self._delete_sqlite_vector_record, entry_id)

    def _delete_sqlite_vector_record(self, entry_id: str) -> None:
        """Delete a vector record from sqlite store."""
        with sqlite3.connect(self._vector_db_path) as conn:
            conn.execute("DELETE FROM memory_vectors WHERE doc_id = ?", (entry_id,))

    async def semantic_search(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 5,
    ) -> list[dict]:
        """Semantic search for relevant memories.

        Returns list of dicts compatible with MemoryManager.get_semantic_context().
        """
        if not query.strip():
            return []

        if self._vector_enabled:
            try:
                await self._backfill_missing_vector_records()
                if self._vector_backend == "chromadb" and self._chroma_collection is not None:
                    return await self._semantic_search_chromadb(query, user_id=user_id, limit=limit)
                return await self._semantic_search_sqlite(query, user_id=user_id, limit=limit)
            except Exception:
                logger.debug("Vector semantic search failed; using lexical fallback", exc_info=True)

        lexical = await self.search(query=query, limit=limit)
        return [
            {
                "id": entry.id,
                "memory": entry.content,
                "score": 0.0,
                "memory_type": entry.type.value,
            }
            for entry in lexical
        ]

    async def _backfill_missing_vector_records(self, max_items: int = 250) -> None:
        """Lazily vectorize historical in-memory entries not present in vector store."""
        if not self._vector_enabled:
            return

        # Chroma backend can upsert duplicates safely and handles id conflicts internally.
        if self._vector_backend == "chromadb" and self._chroma_collection is not None:
            pending = list(self._index.values())[:max_items]
            for entry in pending:
                await self._upsert_vector_record(entry)
            return

        def _get_existing_ids_for_batch(ids: list[str]) -> set[str]:
            if not ids:
                return set()
            placeholders = ",".join("?" for _ in ids)
            query = f"SELECT doc_id FROM memory_vectors WHERE doc_id IN ({placeholders})"
            with sqlite3.connect(self._vector_db_path) as conn:
                rows = conn.execute(query, ids).fetchall()
            return {str(row[0]) for row in rows}

        pending: list[MemoryEntry] = []
        batch_size = 200
        items = iter(self._index.items())

        while len(pending) < max_items:
            batch: list[tuple[str, MemoryEntry]] = []
            for _ in range(batch_size):
                try:
                    batch.append(next(items))
                except StopIteration:
                    break

            if not batch:
                break

            batch_ids = [entry_id for entry_id, _entry in batch]
            existing_batch_ids = await asyncio.to_thread(_get_existing_ids_for_batch, batch_ids)

            for entry_id, entry in batch:
                if entry_id not in existing_batch_ids:
                    pending.append(entry)
                    if len(pending) >= max_items:
                        break

        for entry in pending:
            await self._upsert_vector_record(entry)

    async def _semantic_search_chromadb(self, query: str, user_id: str, limit: int) -> list[dict]:
        """Semantic search using chromadb backend."""
        where = {"user_scope": user_id}
        result = await asyncio.to_thread(
            self._chroma_collection.query,
            query_texts=[query],
            n_results=limit,
            where=where,
        )

        ids = result.get("ids", [[]])[0] if result else []
        docs = result.get("documents", [[]])[0] if result else []
        metas = result.get("metadatas", [[]])[0] if result else []
        distances = result.get("distances", [[]])[0] if result else []

        rows: list[dict] = []
        for index, doc_id in enumerate(ids):
            distance = float(distances[index]) if index < len(distances) else 1.0
            score = max(0.0, 1.0 - distance)
            meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
            memory_text = docs[index] if index < len(docs) else ""
            rows.append(
                {
                    "id": doc_id,
                    "memory": memory_text,
                    "score": score,
                    "memory_type": meta.get("memory_type", "unknown"),
                }
            )

        return rows

    async def _semantic_search_sqlite(self, query: str, user_id: str, limit: int) -> list[dict]:
        """Semantic search using local sqlite vector table."""
        if limit <= 0:
            return []

        query_embedding = await self._embed_text(query)
        # Keep candidate fetch bounded to avoid loading excessive embedding JSON blobs.
        candidate_limit = min(max(limit * 10, 200), 2000)

        def _search_sync() -> list[dict]:
            with sqlite3.connect(self._vector_db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT doc_id, content, memory_type, embedding_json
                    FROM memory_vectors
                    WHERE user_scope = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (user_id, candidate_limit),
                )
                rows = cursor.fetchall()

            scored: list[tuple[float, dict]] = []
            for doc_id, content, memory_type, embedding_json in rows:
                try:
                    embedding = json.loads(embedding_json)
                    if not isinstance(embedding, list):
                        continue
                    score = self._cosine_similarity(query_embedding, [float(v) for v in embedding])
                    scored.append(
                        (
                            score,
                            {
                                "id": doc_id,
                                "memory": content,
                                "score": score,
                                "memory_type": memory_type,
                            },
                        )
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue

            scored.sort(key=lambda item: item[0], reverse=True)
            return [payload for _, payload in scored[:limit]]

        return await asyncio.to_thread(_search_sync)

    # =========================================================================
    # Session Index
    # =========================================================================

    @property
    def _index_path(self) -> Path:
        """Path to the session index file."""
        return self.sessions_path / "_index.json"

    def _load_session_index(self) -> dict:
        """Read session index from disk. Returns empty dict if missing/corrupt."""
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_session_index(self, index: dict) -> None:
        """Atomic write of session index (write to .tmp then rename)."""
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
        tmp.replace(self._index_path)

    # =========================================================================
    # Session Aliases
    # =========================================================================

    @property
    def _aliases_path(self) -> Path:
        """Path to the session aliases file."""
        return self.sessions_path / "_aliases.json"

    def _load_aliases(self) -> dict[str, str]:
        """Read session aliases from disk. Returns empty dict if missing/corrupt."""
        if not self._aliases_path.exists():
            return {}
        try:
            return json.loads(self._aliases_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_aliases(self, aliases: dict[str, str]) -> None:
        """Atomic write of aliases file (write to .tmp then rename)."""
        tmp = self._aliases_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(aliases, indent=2), encoding="utf-8")
        tmp.replace(self._aliases_path)

    async def resolve_session_alias(self, session_key: str) -> str:
        """Resolve a session key through the alias table.

        Returns the aliased target key if one exists, otherwise the original key.
        """
        async with self._alias_lock:
            aliases = self._load_aliases()
        return aliases.get(session_key, session_key)

    async def set_session_alias(self, source_key: str, target_key: str) -> None:
        """Set or overwrite a session alias (source_key -> target_key)."""
        async with self._alias_lock:
            aliases = self._load_aliases()
            aliases[source_key] = target_key
            self._save_aliases(aliases)

    async def remove_session_alias(self, source_key: str) -> bool:
        """Remove a session alias. Returns True if it existed."""
        async with self._alias_lock:
            aliases = self._load_aliases()
            if source_key not in aliases:
                return False
            del aliases[source_key]
            self._save_aliases(aliases)
            return True

    async def get_session_keys_for_chat(self, source_key: str) -> list[str]:
        """Return all session keys associated with this source key.

        Includes the current alias target (if any) plus all historical
        target keys where source matches.
        """
        async with self._alias_lock:
            aliases = self._load_aliases()

        keys: list[str] = []
        for src, tgt in aliases.items():
            if src == source_key:
                keys.append(tgt)

        # Also include the source_key itself (the default/unaliased session)
        safe_default = source_key.replace(":", "_").replace("/", "_")
        default_file = self.sessions_path / f"{safe_default}.json"
        if default_file.exists() and source_key not in keys:
            keys.append(source_key)

        return keys

    async def _update_session_index(
        self, session_key: str, entry: MemoryEntry, session_data: list[dict]
    ) -> None:
        """Update a single entry in the session index after a message save."""
        async with self._session_index_lock:
            index = self._load_session_index()
            safe_key = session_key.replace(":", "_").replace("/", "_")

            # Extract channel from session_key (format: "channel:uuid")
            parts = session_key.split(":", 1)
            channel = parts[0] if len(parts) > 1 else "unknown"

            # Find first user message for title
            title = ""
            for msg in session_data:
                if msg.get("role") == "user" and msg.get("content", "").strip():
                    title = msg["content"].strip()[:80]
                    break
            if not title:
                title = "New Chat"

            # Last message preview
            last_msg = session_data[-1] if session_data else {}
            preview = last_msg.get("content", "")[:120]

            # Timestamps
            first_msg = session_data[0] if session_data else {}
            created = first_msg.get("timestamp", datetime.now(tz=UTC).isoformat())
            last_activity = last_msg.get("timestamp", datetime.now(tz=UTC).isoformat())

            # Preserve existing title if user renamed it
            existing = index.get(safe_key, {})
            if existing.get("user_title"):
                title = existing["user_title"]

            index[safe_key] = {
                "title": title,
                "channel": channel,
                "created": existing.get("created", created),
                "last_activity": last_activity,
                "message_count": len(session_data),
                "preview": preview,
            }
            # Preserve user_title flag if set
            if existing.get("user_title"):
                index[safe_key]["user_title"] = existing["user_title"]

            self._save_session_index(index)

    def rebuild_session_index(self) -> dict:
        """Full directory scan to build index from all session files."""
        index: dict = {}
        for session_file in self.sessions_path.glob("*.json"):
            if session_file.name.startswith("_") or session_file.name.endswith("_compaction.json"):
                continue

            safe_key = session_file.stem
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                if not data or not isinstance(data, list):
                    continue

                # Derive channel from safe_key (format: "channel_uuid")
                parts = safe_key.split("_", 1)
                channel = parts[0] if len(parts) > 1 else "unknown"

                # First user message as title
                title = "New Chat"
                for msg in data:
                    if msg.get("role") == "user" and msg.get("content", "").strip():
                        title = msg["content"].strip()[:80]
                        break

                first_msg = data[0]
                last_msg = data[-1]

                index[safe_key] = {
                    "title": title,
                    "channel": channel,
                    "created": first_msg.get("timestamp", ""),
                    "last_activity": last_msg.get("timestamp", ""),
                    "message_count": len(data),
                    "preview": last_msg.get("content", "")[:120],
                }
            except (json.JSONDecodeError, KeyError, OSError):
                continue

        self._save_session_index(index)
        return index

    async def delete_session(self, session_key: str) -> bool:
        """Delete a session file, compaction cache, and index entry."""
        safe_key = session_key.replace(":", "_").replace("/", "_")
        session_file = self.sessions_path / f"{safe_key}.json"
        compaction_file = self.sessions_path / f"{safe_key}_compaction.json"

        if not session_file.exists():
            return False

        entry_ids: list[str] = []
        try:
            raw = await asyncio.to_thread(lambda: session_file.read_text(encoding="utf-8"))
            data = json.loads(raw)
            if isinstance(data, list):
                entry_ids = [str(item.get("id", "")) for item in data if item.get("id")]
        except (OSError, json.JSONDecodeError):
            entry_ids = []

        session_file.unlink()
        if compaction_file.exists():
            compaction_file.unlink()

        for entry_id in entry_ids:
            await self._delete_vector_record(entry_id)
            await self._delete_graph_for_memory(entry_id)
            self._index.pop(entry_id, None)

        # Remove from index (protected by lock to prevent lost updates)
        async with self._session_index_lock:
            index = self._load_session_index()
            index.pop(safe_key, None)
            self._save_session_index(index)

        # Clean up write lock
        self._session_write_locks.pop(session_key, None)

        return True

    async def update_session_title(self, session_key: str, title: str) -> bool:
        """Update the title of a session in the index."""
        safe_key = session_key.replace(":", "_").replace("/", "_")
        async with self._session_index_lock:
            index = self._load_session_index()
            if safe_key not in index:
                return False
            index[safe_key]["title"] = title
            index[safe_key]["user_title"] = title  # Mark as user-renamed
            self._save_session_index(index)
        return True

    async def search_sessions(self, query: str, limit: int = 20) -> list[dict]:
        """Search session files for messages matching *query*.

        All blocking I/O (glob, read_text, json.loads) runs inside
        ``asyncio.to_thread`` so the event loop is never blocked.
        """
        if not query or not query.strip():
            return []

        query_lower = query.lower()
        sessions_path = self.sessions_path
        index_path = self._index_path

        def _search_sync() -> list[dict]:
            # Load index inside the thread so its file I/O doesn't block
            # the event loop either.
            try:
                index_snapshot = json.loads(index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                index_snapshot = {}
            results: list[dict] = []
            for session_file in sessions_path.glob("*.json"):
                if session_file.name.startswith("_") or session_file.name.endswith(
                    "_compaction.json"
                ):
                    continue
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                    for msg in data:
                        if query_lower in msg.get("content", "").lower():
                            safe_key = session_file.stem
                            meta = index_snapshot.get(safe_key, {})
                            results.append(
                                {
                                    "id": safe_key,
                                    "title": meta.get("title", "Untitled"),
                                    "channel": meta.get("channel", "unknown"),
                                    "match": msg["content"][:200],
                                    "match_role": msg.get("role", ""),
                                    "last_activity": meta.get("last_activity", ""),
                                }
                            )
                            break
                except (json.JSONDecodeError, OSError):
                    continue
                if len(results) >= limit:
                    break
            return results

        return await asyncio.to_thread(_search_sync)

    def _load_index(self) -> None:
        """Load existing memories into index."""
        # Load long-term memories (root = owner/default)
        if self.long_term_file.exists():
            self._parse_markdown_file(self.long_term_file, MemoryType.LONG_TERM)

        # Load per-user long-term memories
        users_dir = self.base_path / "users"
        if users_dir.exists():
            for user_mem in users_dir.glob("*/MEMORY.md"):
                self._parse_markdown_file(user_mem, MemoryType.LONG_TERM)

        # Load ALL daily files (not just today's)
        for daily_file in sorted(
            self.base_path.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md")
        ):
            self._parse_markdown_file(daily_file, MemoryType.DAILY)

        self._inv_dirty = True

    def _rebuild_inverted(self) -> None:
        """Build/rebuild the inverted index from _index. Resets _inv_dirty."""
        inv: dict[str, set[str]] = {}
        for eid, entry in self._index.items():
            words = _tokenize(entry.content)
            header = entry.metadata.get("header", "")
            if header:
                words |= _tokenize(header)
            for w in words:
                inv.setdefault(w, set()).add(eid)
        self._inverted = inv
        self._inv_dirty = False

    def _parse_markdown_file(self, path: Path, memory_type: MemoryType) -> None:
        """Parse a markdown file into memory entries."""
        content = path.read_text(encoding="utf-8")

        # Derive user_id from path for per-user memory files
        user_id = "default"
        users_dir = self.base_path / "users"
        try:
            if path.is_relative_to(users_dir):
                # e.g. .../users/abc123/MEMORY.md → user_id = "abc123"
                user_id = path.parent.name
        except (TypeError, ValueError):
            pass

        # Split by headers (## or ###)
        sections = re.split(r"\n(?=##+ )", content)

        for section in sections:
            if not section.strip():
                continue

            # Extract header and content
            lines = section.strip().split("\n")
            header = lines[0].lstrip("#").strip()
            body = "\n".join(lines[1:]).strip()

            if body:
                entry_id = _make_deterministic_id(path, header, body)
                metadata = {"header": header, "source": str(path)}
                if user_id != "default":
                    metadata["user_id"] = user_id
                self._index[entry_id] = MemoryEntry(
                    id=entry_id,
                    type=memory_type,
                    content=body,
                    tags=self._extract_tags(body),
                    metadata=metadata,
                )

    def _extract_tags(self, content: str) -> list[str]:
        """Extract #tags from content."""
        return re.findall(r"#(\w+)", content)

    def _get_user_memory_file(self, user_id: str = "default") -> Path:
        """Get the MEMORY.md path for a given user.

        - "default" → root MEMORY.md (owner / single-user)
        - Others → users/{user_id}/MEMORY.md (auto-create dir)
        """
        if user_id == "default":
            return self.long_term_file
        user_dir = self.base_path / "users" / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / "MEMORY.md"

    def _get_daily_file(self, d: date) -> Path:
        """Get the path for a daily notes file."""
        return self.base_path / f"{d.isoformat()}.md"

    def _get_session_file(self, session_key: str) -> Path:
        """Get the path for a session file."""
        safe_key = session_key.replace(":", "_").replace("/", "_")
        return self.sessions_path / f"{safe_key}.json"

    # =========================================================================
    # MemoryStoreProtocol Implementation
    # =========================================================================

    async def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry."""
        if entry.type == MemoryType.SESSION:
            # Session entries use random UUIDs (no collision issue)
            if not entry.id:
                entry.id = str(uuid.uuid4())
            entry.updated_at = datetime.now(tz=UTC)
            self._index[entry.id] = entry
            await self._save_session_entry(entry)
            await self._upsert_vector_record(entry)
            await self._index_graph_record(entry)
            return entry.id

        # For LONG_TERM and DAILY: compute deterministic ID from content
        header = entry.metadata.get("header", "Memory")
        if entry.type == MemoryType.LONG_TERM:
            user_id = entry.metadata.get("user_id", "default")
            target_path = self._get_user_memory_file(user_id)
        else:
            target_path = self._get_daily_file(date.today())

        det_id = _make_deterministic_id(target_path, header, entry.content)

        # Dedup: if this exact content already exists, skip
        if det_id in self._index:
            return det_id

        entry.id = det_id
        entry.metadata["source"] = str(target_path)
        entry.updated_at = datetime.now(tz=UTC)
        self._index[entry.id] = entry
        self._inv_dirty = True

        # Persist to markdown
        await self._append_to_markdown(target_path, entry)
        await self._upsert_vector_record(entry)
        await self._index_graph_record(entry)

        return entry.id

    async def _append_to_markdown(self, path: Path, entry: MemoryEntry) -> None:
        """Append a memory entry to a markdown file."""
        header = entry.metadata.get("header", datetime.now(tz=UTC).strftime("%H:%M"))
        tags_str = " ".join(f"#{t}" for t in entry.tags) if entry.tags else ""

        section = f"\n\n## {header}\n\n{entry.content}"
        if tags_str:
            section += f"\n\n{tags_str}"

        with open(path, "a", encoding="utf-8") as f:
            f.write(section)

    async def _save_session_entry(self, entry: MemoryEntry) -> None:
        """Save a session memory entry."""
        if not entry.session_key:
            return

        # Per-session lock to prevent concurrent read-modify-write corruption
        if entry.session_key not in self._session_write_locks:
            self._session_write_locks[entry.session_key] = asyncio.Lock()

        async with self._session_write_locks[entry.session_key]:
            session_file = self._get_session_file(entry.session_key)

            # Run blocking file I/O in a thread to avoid freezing the event loop
            def _read_and_append_once() -> list[dict[str, object]]:
                session_data = []
                if session_file.exists():
                    try:
                        session_data = json.loads(session_file.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        logger.warning("Discarding corrupt session file %s: %s", session_file, exc)
                session_data.append(
                    {
                        "id": entry.id,
                        "role": entry.role,
                        "content": entry.content,
                        "timestamp": entry.created_at.isoformat(),
                        "metadata": entry.metadata,
                    }
                )
                # Atomic write: tmp file + replace to prevent corruption on crash
                tmp = session_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
                # On Windows, os.replace can fail with PermissionError if another
                # process briefly holds the file handle.
                tmp.replace(session_file)
                return session_data

            session_data: list[dict[str, object]] | None = None
            for _attempt in range(5):
                try:
                    session_data = await asyncio.to_thread(_read_and_append_once)
                    break
                except PermissionError:
                    if _attempt == 4:
                        raise
                    await asyncio.sleep(0.01 * (2**_attempt))

            if session_data is None:
                return

            # Update session index
            await self._update_session_index(entry.session_key, entry, session_data)

    async def get(self, entry_id: str) -> MemoryEntry | None:
        """Get a memory entry by ID."""
        return self._index.get(entry_id)

    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry and rewrite source file."""
        if entry_id not in self._index:
            return False

        entry = self._index.pop(entry_id)
        self._inv_dirty = True

        # Rewrite the source markdown file without this entry
        source = entry.metadata.get("source")
        if source:
            self._rewrite_markdown(Path(source))

        await self._delete_vector_record(entry_id)
        await self._delete_graph_for_memory(entry_id)

        return True

    async def update_entry(
        self,
        entry_id: str,
        *,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        """Update a memory entry in-place and re-index vector/graph records."""
        entry = self._index.get(entry_id)
        if not entry:
            return False

        if content is not None:
            stripped = content.strip()
            if not stripped:
                return False
            entry.content = stripped

        if tags is not None:
            entry.tags = [str(tag).strip() for tag in tags if str(tag).strip()]

        entry.updated_at = datetime.now(tz=UTC)

        source = entry.metadata.get("source")
        if source:
            self._rewrite_markdown(Path(source))
        elif entry.type == MemoryType.SESSION and entry.session_key:
            # Session edits are not currently supported for JSON history.
            return False

        await self._delete_vector_record(entry_id)
        await self._delete_graph_for_memory(entry_id)
        await self._upsert_vector_record(entry)
        await self._index_graph_record(entry)
        self._inv_dirty = True
        return True

    async def get_graph_snapshot(
        self,
        *,
        user_id: str = "default",
        query: str | None = None,
        limit: int = 200,
    ) -> dict:
        """Return a lightweight graph snapshot for dashboard visualization."""

        if not self._graph_enabled or not self._graph_db_path.exists():
            return {"nodes": [], "edges": []}

        def _snapshot_sync() -> dict:
            with sqlite3.connect(self._graph_db_path) as conn:
                if query and query.strip():
                    like = f"%{query.strip().lower()}%"
                    nodes = conn.execute(
                        """
                        SELECT entity_id, display_name, mention_count, last_seen
                        FROM entities
                        WHERE user_scope = ? AND (entity_key LIKE ? OR display_name LIKE ?)
                        ORDER BY mention_count DESC, last_seen DESC
                        LIMIT ?
                        """,
                        (user_id, like, like, limit),
                    ).fetchall()
                else:
                    nodes = conn.execute(
                        """
                        SELECT entity_id, display_name, mention_count, last_seen
                        FROM entities
                        WHERE user_scope = ?
                        ORDER BY mention_count DESC, last_seen DESC
                        LIMIT ?
                        """,
                        (user_id, limit),
                    ).fetchall()

                node_ids = [str(row[0]) for row in nodes]
                if node_ids:
                    conn.execute("CREATE TEMP TABLE selected_node_ids (entity_id TEXT PRIMARY KEY)")
                    self._insert_valid_ids_batched(conn, "selected_node_ids", node_ids)
                    edge_rows = conn.execute(
                        """
                        SELECT r.relationship_id,
                            r.source_entity_id,
                            r.target_entity_id,
                            se.display_name,
                            te.display_name,
                            r.relation_type,
                            r.weight,
                            r.last_seen
                        FROM relationships r
                        JOIN entities se ON se.entity_id = r.source_entity_id
                        JOIN entities te ON te.entity_id = r.target_entity_id
                        JOIN selected_node_ids src_nodes ON src_nodes.entity_id = r.source_entity_id
                        JOIN selected_node_ids tgt_nodes ON tgt_nodes.entity_id = r.target_entity_id
                        WHERE r.user_scope = ?
                        ORDER BY r.weight DESC, r.last_seen DESC
                        LIMIT ?
                        """,
                        (user_id, limit),
                    ).fetchall()
                else:
                    edge_rows = []

            return {
                "nodes": [
                    {
                        "id": row[0],
                        "name": row[1],
                        "count": int(row[2]),
                        "last_seen": row[3],
                    }
                    for row in nodes
                ],
                "edges": [
                    {
                        "id": row[0],
                        "source": row[1],
                        "target": row[2],
                        "source_name": row[3],
                        "target_name": row[4],
                        "relation": row[5],
                        "weight": int(row[6]),
                        "last_seen": row[7],
                    }
                    for row in edge_rows
                ],
            }

        return await asyncio.to_thread(_snapshot_sync)

    async def get_graph_svg(
        self,
        *,
        user_id: str = "default",
        query: str | None = None,
        limit: int = 200,
        width: int = 800,
        height: int = 400,
    ) -> str:
        """Generate SVG visualization of the memory knowledge graph using networkx."""

        if not self._graph_enabled or not self._graph_db_path.exists():
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
                '<text x="10" y="20" fill="rgba(255,255,255,0.6)">No graph data</text>'
                "</svg>"
            )

        def _generate_svg_sync() -> str:
            import math

            try:
                nx = importlib.import_module("networkx")
            except ImportError:
                try:
                    require_extra("networkx", "graph")
                except ImportError as exc:
                    install_hint = str(exc)
                else:
                    install_hint = "Install graph extras to enable SVG rendering."

                logger.info("Graph SVG rendering unavailable: %s", install_hint)
                fallback_svg = (
                    '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400">'
                    '<text x="10" y="20" fill="rgba(255,255,255,0.6)">'
                    "Graph visualization unavailable. Install pocketpaw[graph]."
                    "</text>"
                    "</svg>"
                )
                return fallback_svg

            # Fetch graph data
            with sqlite3.connect(self._graph_db_path) as conn:
                if query and query.strip():
                    like = f"%{query.strip().lower()}%"
                    nodes = conn.execute(
                        """
                        SELECT entity_id, display_name, mention_count
                        FROM entities
                        WHERE user_scope = ? AND (entity_key LIKE ? OR display_name LIKE ?)
                        ORDER BY mention_count DESC LIMIT ?
                        """,
                        (user_id, like, like, limit),
                    ).fetchall()
                else:
                    nodes = conn.execute(
                        """
                        SELECT entity_id, display_name, mention_count
                        FROM entities
                        WHERE user_scope = ?
                        ORDER BY mention_count DESC LIMIT ?
                        """,
                        (user_id, limit),
                    ).fetchall()

                node_ids = [str(row[0]) for row in nodes]
                node_map = {str(row[0]): row[1] for row in nodes}

                if node_ids:
                    conn.execute("CREATE TEMP TABLE selected_node_ids (entity_id TEXT PRIMARY KEY)")
                    self._insert_valid_ids_batched(conn, "selected_node_ids", node_ids)
                    edge_rows = conn.execute(
                        """
                        SELECT r.source_entity_id, r.target_entity_id, r.relation_type, r.weight
                        FROM relationships r
                        JOIN selected_node_ids src_nodes ON src_nodes.entity_id = r.source_entity_id
                        JOIN selected_node_ids tgt_nodes ON tgt_nodes.entity_id = r.target_entity_id
                        WHERE r.user_scope = ?
                        ORDER BY r.weight DESC LIMIT ?
                        """,
                        (user_id, limit),
                    ).fetchall()
                else:
                    edge_rows = []

            if not node_ids:
                empty_svg = (
                    '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400">'
                    '<text x="10" y="20" fill="rgba(255,255,255,0.6)">No graph data</text>'
                    "</svg>"
                )
                return empty_svg

            # Build networkx graph
            G = nx.Graph()
            for node_id in node_ids:
                count = next((n[2] for n in nodes if str(n[0]) == node_id), 1)
                G.add_node(node_id, label=node_map.get(node_id, node_id), count=count)

            for src, tgt, rel_type, weight in edge_rows:
                G.add_edge(str(src), str(tgt), label=rel_type, weight=weight)

            # Layout computation
            try:
                pos = nx.spring_layout(G, k=2, iterations=50, seed=42)
            except Exception:
                pos = nx.random_layout(G)

            # Normalize positions to fit SVG canvas
            xs = [x for x, y in pos.values()]
            ys = [y for x, y in pos.values()]
            if xs and ys:
                x_min, x_max = min(xs), max(xs)
                y_min, y_max = min(ys), max(ys)
                x_range = x_max - x_min if x_max > x_min else 1
                y_range = y_max - y_min if y_max > y_min else 1
                margin = 40
                normalized_pos = {}
                for node, (x, y) in pos.items():
                    nx_norm = ((x - x_min) / x_range) * (width - 2 * margin) + margin
                    ny_norm = ((y - y_min) / y_range) * (height - 2 * margin) + margin
                    normalized_pos[node] = (nx_norm, ny_norm)
            else:
                normalized_pos = pos

            # Generate SVG
            svg_header = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                f'style="background: rgba(0,0,0,0.4);">'
            )
            svg_parts = [svg_header]

            # Draw edges
            for src, tgt, data in G.edges(data=True):
                x1, y1 = normalized_pos.get(src, (width / 2, height / 2))
                x2, y2 = normalized_pos.get(tgt, (width / 2, height / 2))
                rel_label = data.get("label", "related")
                svg_parts.append(
                    f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                    f'stroke="rgba(255,255,255,0.25)" stroke-width="1.5"/>'
                )
                # Draw arrowhead
                angle = math.atan2(y2 - y1, x2 - x1)
                arrow_size = 8
                ax1 = x2 - arrow_size * math.cos(angle - math.pi / 6)
                ay1 = y2 - arrow_size * math.sin(angle - math.pi / 6)
                ax2 = x2 - arrow_size * math.cos(angle + math.pi / 6)
                ay2 = y2 - arrow_size * math.sin(angle + math.pi / 6)
                svg_parts.append(
                    f'<polygon points="{x2:.1f},{y2:.1f} {ax1:.1f},{ay1:.1f} {ax2:.1f},{ay2:.1f}" '
                    f'fill="rgba(255,255,255,0.25)"/>'
                )
                # Draw edge label
                mid_x = (x1 + x2) / 2
                mid_y = (y1 + y2) / 2
                text_style = (
                    f'<text x="{mid_x:.1f}" y="{mid_y:.1f}" font-size="9" '
                    f'fill="rgba(255,255,255,0.5)" text-anchor="middle" '
                    f'pointer-events="none">{html.escape(rel_label)}</text>'
                )
                svg_parts.append(text_style)

            # Draw nodes
            for node in G.nodes():
                x, y = normalized_pos.get(node, (width / 2, height / 2))
                count = G.nodes[node].get("count", 1)
                label = G.nodes[node].get("label", node)
                node_size = max(20, min(50, 20 + count * 2))

                svg_parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{node_size / 2:.1f}" '
                    f'fill="rgba(255,255,255,0.15)" stroke="rgba(255,255,255,0.4)" '
                    f'stroke-width="1.5"/>'
                )
                svg_parts.append(
                    f'<text x="{x:.1f}" y="{y:.1f}" font-size="11" font-family="system-ui" '
                    f'fill="rgba(255,255,255,0.9)" text-anchor="middle" dominant-baseline="middle" '
                    f'pointer-events="none">{html.escape(label[:12])}</text>'
                )

            svg_parts.append("</svg>")
            return "".join(svg_parts)

        return await asyncio.to_thread(_generate_svg_sync)

    async def get_memory_stats(self) -> dict:
        """Return memory stats for dashboard/API display."""
        by_type = {
            MemoryType.LONG_TERM.value: 0,
            MemoryType.DAILY.value: 0,
            MemoryType.SESSION.value: 0,
        }
        for entry in self._index.values():
            by_type[entry.type.value] += 1

        def _vector_count() -> int:
            if not self._vector_db_path.exists():
                return 0
            with sqlite3.connect(self._vector_db_path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()
            return int(row[0]) if row else 0

        def _graph_counts() -> tuple[int, int]:
            if not self._graph_enabled or not self._graph_db_path.exists():
                return (0, 0)
            with sqlite3.connect(self._graph_db_path) as conn:
                entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()
                relationships = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()
            return (
                int(entities[0]) if entities else 0,
                int(relationships[0]) if relationships else 0,
            )

        vector_entries = await asyncio.to_thread(_vector_count)
        graph_entities, graph_relationships = await asyncio.to_thread(_graph_counts)

        return {
            "backend": "file",
            "total_memories": len(self._index),
            "by_type": by_type,
            "vector_backend": self._vector_backend,
            "vector_enabled": self._vector_enabled,
            "vector_entries": vector_entries,
            "graph_enabled": self._graph_enabled,
            "graph_entities": graph_entities,
            "graph_relationships": graph_relationships,
        }

    async def prune_memories(self, older_than_days: int = 30) -> dict:
        """Prune old daily memories and clean orphan vector/graph records."""
        cutoff = datetime.now(tz=UTC).timestamp() - (older_than_days * 86400)
        to_delete: list[str] = []
        for entry_id, entry in self._index.items():
            if entry.type != MemoryType.DAILY:
                continue
            if _ensure_utc(entry.created_at).timestamp() < cutoff:
                to_delete.append(entry_id)

        deleted = 0
        for entry_id in to_delete:
            if await self.delete(entry_id):
                deleted += 1

        await self._cleanup_orphan_records()
        return {
            "ok": True,
            "older_than_days": older_than_days,
            "deleted_daily_memories": deleted,
        }

    async def _cleanup_orphan_records(self) -> None:
        """Remove vector/graph rows that no longer map to in-memory entries.

        Uses a temporary table approach to avoid SQLite's SQLITE_LIMIT_VARIABLE_NUMBER
        (default 999). With 1000+ valid IDs, a direct NOT IN clause would fail.
        Instead, we create a temp table, populate it in batches, then delete orphans
        in a single operation using a subquery.
        """
        valid_ids = list(self._index.keys())

        def _cleanup_vector_sync() -> None:
            if not self._vector_db_path.exists():
                return
            with sqlite3.connect(self._vector_db_path) as conn:
                if valid_ids:
                    # Use temp table to avoid SQLite variable limit (default 999)
                    conn.execute("CREATE TEMP TABLE valid_doc_ids (doc_id TEXT PRIMARY KEY)")
                    self._insert_valid_ids_batched(conn, "valid_doc_ids", valid_ids)
                    conn.execute(
                        "DELETE FROM memory_vectors "
                        "WHERE doc_id NOT IN (SELECT doc_id FROM valid_doc_ids)"
                    )
                else:
                    conn.execute("DELETE FROM memory_vectors")

        def _cleanup_graph_sync() -> None:
            if not self._graph_db_path.exists():
                return
            with sqlite3.connect(self._graph_db_path) as conn:
                if valid_ids:
                    # Use temp table to avoid SQLite variable limit (default 999)
                    conn.execute("CREATE TEMP TABLE valid_memory_ids (memory_id TEXT PRIMARY KEY)")
                    self._insert_valid_ids_batched(conn, "valid_memory_ids", valid_ids)
                    conn.execute(
                        "DELETE FROM memory_entity_links "
                        "WHERE memory_id NOT IN (SELECT memory_id FROM valid_memory_ids)"
                    )
                    conn.execute(
                        "DELETE FROM relationship_evidence "
                        "WHERE memory_id NOT IN (SELECT memory_id FROM valid_memory_ids)"
                    )
                else:
                    conn.execute("DELETE FROM memory_entity_links")
                    conn.execute("DELETE FROM relationship_evidence")

                conn.execute(
                    "DELETE FROM relationships WHERE relationship_id NOT IN "
                    "(SELECT DISTINCT relationship_id FROM relationship_evidence)"
                )
                conn.execute(
                    "DELETE FROM entities WHERE entity_id NOT IN "
                    "(SELECT DISTINCT entity_id FROM memory_entity_links)"
                )

        await asyncio.to_thread(_cleanup_vector_sync)
        await asyncio.to_thread(_cleanup_graph_sync)

    @staticmethod
    def _insert_valid_ids_batched(
        conn: sqlite3.Connection, table_name: str, valid_ids: list[str], batch_size: int = 500
    ) -> None:
        """Insert valid IDs into a temp table in batches to avoid variable limit.

        Args:
            conn: SQLite connection
            table_name: Name of temp table to populate
            valid_ids: List of valid memory/doc IDs
            batch_size: Maximum IDs per INSERT (stays well under SQLite's 999 limit)
        """
        for i in range(0, len(valid_ids), batch_size):
            batch = valid_ids[i : i + batch_size]
            placeholders = ",".join("(?)" for _ in batch)
            conn.execute(f"INSERT INTO {table_name} VALUES {placeholders}", tuple(batch))

    def _rewrite_markdown(self, path: Path) -> None:
        """Reconstruct a markdown file from remaining index entries for that file."""
        source_str = str(path)
        entries = [e for e in self._index.values() if e.metadata.get("source") == source_str]

        if not entries:
            # No entries left — remove file
            if path.exists():
                path.unlink()
            return

        parts = []
        for e in entries:
            header = e.metadata.get("header", "Memory")
            tags_str = " ".join(f"#{t}" for t in e.tags) if e.tags else ""
            section = f"## {header}\n\n{e.content}"
            if tags_str:
                section += f"\n\n{tags_str}"
            parts.append(section)

        path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")

    async def search(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search memories using word-overlap scoring."""
        candidates: list[tuple[float, MemoryEntry]] = []
        query_words = _tokenize(query) if query else set()

        if query_words:
            # Rebuild inverted index if dirty
            if self._inv_dirty:
                self._rebuild_inverted()

            # Narrow candidates to entries sharing at least one query word
            candidate_ids: set[str] = set()
            for w in query_words:
                candidate_ids |= self._inverted.get(w, set())

            for eid in candidate_ids:
                entry = self._index.get(eid)
                if not entry:
                    continue

                # Type filter
                if memory_type and entry.type != memory_type:
                    continue

                # Tag filter
                if tags and not any(t in entry.tags for t in tags):
                    continue

                content_words = _tokenize(entry.content)
                header = entry.metadata.get("header", "")
                if header:
                    content_words |= _tokenize(header)

                overlap = query_words & content_words
                if not overlap:
                    continue
                score = len(overlap) / len(query_words)
                candidates.append((score, entry))
        else:
            # No query — apply type/tag filters across all entries
            for entry in self._index.values():
                if memory_type and entry.type != memory_type:
                    continue
                if tags and not any(t in entry.tags for t in tags):
                    continue
                candidates.append((0.0, entry))

        # Sort by score descending
        candidates.sort(key=lambda x: x[0], reverse=True)

        return [entry for _, entry in candidates[:limit]]

    async def get_by_type(
        self, memory_type: MemoryType, limit: int = 100, **kwargs
    ) -> list[MemoryEntry]:
        """Get all memories of a specific type.

        For LONG_TERM type, accepts optional user_id kwarg to scope retrieval.
        """
        user_id = kwargs.get("user_id")
        results = []
        for e in self._index.values():
            if e.type != memory_type:
                continue
            # Scope LONG_TERM to user_id if provided
            if user_id and memory_type == MemoryType.LONG_TERM:
                entry_uid = e.metadata.get("user_id", "default")
                if entry_uid != user_id:
                    continue
            results.append(e)
            if len(results) >= limit:
                break
        return results

    async def get_session(self, session_key: str) -> list[MemoryEntry]:
        """Get session history."""
        session_file = self._get_session_file(session_key)

        if not session_file.exists():
            return []

        try:
            # raw = await asyncio.to_thread(lambda: session_file.read_text(encoding="utf-8"))
            import time

            def safe_read():
                for _ in range(3):
                    try:
                        return session_file.read_text(encoding="utf-8")
                    except PermissionError:
                        time.sleep(0.01)
                return ""

            raw = await asyncio.to_thread(safe_read)
            data = json.loads(raw)
            return [
                MemoryEntry(
                    id=item["id"],
                    type=MemoryType.SESSION,
                    content=item["content"],
                    role=item.get("role"),
                    session_key=session_key,
                    created_at=_ensure_utc(datetime.fromisoformat(item["timestamp"])),
                    metadata=item.get("metadata", {}),
                )
                for item in data
            ]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Could not parse session file %s: %s", session_file, exc)
            return []

    async def clear_session(self, session_key: str) -> int:
        """Clear session history."""
        session_file = self._get_session_file(session_key)

        def _clear() -> tuple[int, list[str]]:
            if session_file.exists():
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        count = len(data)
                        ids = [
                            str(item.get("id", ""))
                            for item in data
                            if isinstance(item, dict) and item.get("id")
                        ]
                    else:
                        count = 0
                        ids = []
                    session_file.unlink()
                    return count, ids
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Corrupt session file removed %s: %s", session_file, exc)
                    session_file.unlink()
                    return 0, []
            return 0, []

        count, entry_ids = await asyncio.to_thread(_clear)
        for entry_id in entry_ids:
            await self._delete_vector_record(entry_id)
            await self._delete_graph_for_memory(entry_id)
            self._index.pop(entry_id, None)
        return count
