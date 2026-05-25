# ee/pocketpaw_ee/cloud/decisions/explain/cache.py
# Created: 2026-05-25 (RFC 07 Slice 3a) — the 24h cache for natural-
#   language explain responses. Per RFC 07 § Open Question 6
#   (amendment): cache keyed on
#     (normalized_question, root_decision_id, depth, scope_hash)
#   Invalidation per RFC 07 § Open Question 8 amendment: when the
#   projection folds a new event, walk a reverse index keyed by
#   `decisions_walked` and drop every cache entry whose walked set
#   contains the affected decision.
#
# Layering note — no cycle:
#   cache.py imports `decisions.store` (to share the SQLite
#   connection) and registers a callback against
#   `decisions.projection`'s post-apply hook registry. The projection
#   never imports cache; it only invokes whatever hooks were registered.
#   That keeps the layering one-way: explain → decisions, never
#   decisions → explain.
#
# Schema lives in `decisions.store` so the cache + decisions tables
# share one SQLite file (`~/.soul/decisions.db`) — no second connection
# pool, same WAL semantics, same on-disk artifact for the operator
# backup story.
"""Cache layer + invalidation for the explain pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.decisions.domain import Decision
from pocketpaw_ee.cloud.decisions.explain.narrator import Explanation
from pocketpaw_ee.cloud.decisions.store import DecisionStore

logger = logging.getLogger(__name__)

# Default cache TTL — 24 hours per RFC 07 § Open Question 6.
DEFAULT_TTL = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Key derivation — deterministic across processes
# ---------------------------------------------------------------------------


def _normalize_question(question: str) -> str:
    """Collapse whitespace + lowercase so trivial reformattings hit the
    same cache row. Punctuation is kept so "Why X?" and "Why X" are
    treated as the same query (the question-mark carries no semantic
    weight in the cache key)."""
    text = question.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip("?!.")
    return text


def _hash_scope(scope: dict[str, Any] | None) -> str:
    """Stable hash of the scope dict — sorts keys so {a:1, b:2} and
    {b:2, a:1} hash the same."""
    if not scope:
        return "empty"
    raw = json.dumps(scope, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_cache_key(
    *,
    question_normalized: str,
    root_decision_id: UUID | None,
    depth: int,
    scope_hash: str,
) -> str:
    """Compose the deterministic cache key. The root id is the
    primary cache discriminator — same question against different
    decision roots is a different entry. ``None`` root collapses to
    the literal "none" so questions that don't pin a root still hash
    stably."""
    root_part = str(root_decision_id) if root_decision_id is not None else "none"
    raw = f"{question_normalized}||{root_part}||{depth}||{scope_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# DDL — applied lazily on the shared DecisionStore connection
# ---------------------------------------------------------------------------

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS decision_explain_cache (
    cache_key TEXT PRIMARY KEY,
    question_normalized TEXT NOT NULL,
    root_decision_id TEXT,
    depth INTEGER NOT NULL,
    scope_hash TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decisions_walked TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_explain_cache_expires
    ON decision_explain_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_explain_cache_root
    ON decision_explain_cache(root_decision_id);
"""


# Per-process flag — DDL only runs the first time the cache touches
# the store. WAL mode + IF NOT EXISTS make this idempotent across
# multiple processes pointed at the same SQLite file.
_DDL_APPLIED: dict[int, bool] = {}
_DDL_LOCK = threading.Lock()


def _ensure_schema(store: DecisionStore) -> None:
    """Apply the cache DDL on the store's connection. Idempotent —
    IF NOT EXISTS makes re-runs cheap."""
    conn = store._conn  # noqa: SLF001 — sibling module shares the connection
    if conn is None:
        return
    with _DDL_LOCK:
        if _DDL_APPLIED.get(id(conn)):
            return
        conn.executescript(_CACHE_DDL)
        _DDL_APPLIED[id(conn)] = True


# ---------------------------------------------------------------------------
# Public cache API
# ---------------------------------------------------------------------------


class ExplainCache:
    """24-hour cache for explain responses, sharing the decision store's
    SQLite file. Thread-safe via the store's existing write lock."""

    def __init__(
        self,
        store: DecisionStore,
        *,
        ttl: timedelta | None = None,
    ) -> None:
        self._store = store
        self._ttl = ttl or DEFAULT_TTL
        _ensure_schema(store)

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    # --- reads --------------------------------------------------------------

    def get(self, cache_key: str, *, now: datetime | None = None) -> Explanation | None:
        """Fetch a cached explanation. Returns ``None`` on miss, expired
        entry, or shape error. Expired entries are not GC'd here — the
        sweeper does that lazily (cheap enough to skip on the hot path)."""
        now = now or datetime.now(timezone.utc)
        conn = self._store._conn  # noqa: SLF001
        if conn is None:
            return None
        row = conn.execute(
            "SELECT * FROM decision_explain_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if expires <= now:
            # Stale — delete inline so the next hit skips this row.
            self._delete(cache_key)
            return None
        try:
            payload = json.loads(row["explanation_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        try:
            return Explanation.model_validate(payload)
        except Exception:  # noqa: BLE001
            return None

    # --- writes -------------------------------------------------------------

    def put(
        self,
        cache_key: str,
        explanation: Explanation,
        *,
        question_normalized: str,
        root_decision_id: UUID | None,
        depth: int,
        scope_hash: str,
        now: datetime | None = None,
    ) -> None:
        """Insert or replace the cache entry. ``decisions_walked`` is
        serialised as a JSON array string so the reverse-index search
        is a single LIKE-glob (good enough at expected scale)."""
        now = now or datetime.now(timezone.utc)
        expires = now + self._ttl
        walked_json = json.dumps([str(uid) for uid in explanation.decisions_walked])
        explanation_json = explanation.model_dump_json()
        conn = self._store._conn  # noqa: SLF001
        if conn is None:
            return
        # Reuse the store's write lock so we never write concurrently
        # with the projection (which would block the connection).
        with self._store._lock:  # noqa: SLF001
            conn.execute(
                """
                INSERT OR REPLACE INTO decision_explain_cache (
                    cache_key,
                    question_normalized,
                    root_decision_id,
                    depth,
                    scope_hash,
                    explanation_json,
                    created_at,
                    expires_at,
                    decisions_walked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    question_normalized,
                    str(root_decision_id) if root_decision_id else None,
                    int(depth),
                    scope_hash,
                    explanation_json,
                    now.isoformat(),
                    expires.isoformat(),
                    walked_json,
                ),
            )

    # --- invalidation -------------------------------------------------------

    def invalidate_for_decisions(self, decision_ids: Iterable[UUID]) -> int:
        """Drop every cache entry whose `decisions_walked` list contains
        any of the supplied decision ids. Returns the number of rows
        deleted (for telemetry / test assertions).

        Implementation note: the reverse index is a JSON array column;
        for the expected entry count (low thousands per org per day) a
        LIKE-glob over the serialised string is fast enough and avoids
        a many-to-many join table. If the row count grows past
        ~100k we'll introduce `decision_explain_cache_walk` and a true
        index — captured in the RFC 07 § amendment.
        """
        ids = [str(d) for d in decision_ids]
        if not ids:
            return 0
        conn = self._store._conn  # noqa: SLF001
        if conn is None:
            return 0
        with self._store._lock:  # noqa: SLF001
            # Build OR-chain of LIKE-globs — one per id. Parameterised
            # so the strings can't escape into the SQL.
            clauses = " OR ".join(["decisions_walked LIKE ?"] * len(ids))
            params = [f"%{d}%" for d in ids]
            cursor = conn.execute(
                f"DELETE FROM decision_explain_cache WHERE {clauses}",
                params,
            )
            return cursor.rowcount or 0

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Drop every expired row. Cheap to run periodically; not on
        the hot path (cache.get handles expiry inline for the hit row)."""
        now = now or datetime.now(timezone.utc)
        conn = self._store._conn  # noqa: SLF001
        if conn is None:
            return 0
        with self._store._lock:  # noqa: SLF001
            cursor = conn.execute(
                "DELETE FROM decision_explain_cache WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            return cursor.rowcount or 0

    def count(self) -> int:
        """Total cached rows (test seam)."""
        conn = self._store._conn  # noqa: SLF001
        if conn is None:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM decision_explain_cache"
        ).fetchone()
        return int(row["n"]) if row else 0

    # --- internals ---------------------------------------------------------

    def _delete(self, cache_key: str) -> None:
        conn = self._store._conn  # noqa: SLF001
        if conn is None:
            return
        with self._store._lock:  # noqa: SLF001
            conn.execute(
                "DELETE FROM decision_explain_cache WHERE cache_key = ?",
                (cache_key,),
            )


# ---------------------------------------------------------------------------
# Singleton + projection hook registration
# ---------------------------------------------------------------------------


_CACHE: ExplainCache | None = None


def get_explain_cache() -> ExplainCache:
    """Return the process-wide ExplainCache. Lazy-bound to the same
    DecisionStore the projection writes through."""
    global _CACHE
    if _CACHE is None:
        from pocketpaw_ee.cloud.decisions.service import get_decision_graph

        graph = get_decision_graph()
        _CACHE = ExplainCache(graph.store)
        _register_projection_hook(graph)
    return _CACHE


def reset_explain_cache_for_tests() -> None:
    """Drop the singleton so the next call rebuilds against the current
    store (paired with `set_db_path` in test fixtures)."""
    global _CACHE
    _CACHE = None
    # Also clear the DDL-applied marker so the next test gets a fresh
    # ensure_schema call.
    _DDL_APPLIED.clear()


def _register_projection_hook(graph: Any) -> None:
    """Wire the cache's invalidator into the projection's post-apply hook.

    Idempotent — calling twice re-registers the same callback once. The
    projection exposes a stable callback registry per RFC 07 Slice 3a
    amendment so this layering stays one-way (explain → decisions).
    """
    projection = graph.projection
    if not hasattr(projection, "register_post_apply_hook"):
        # Older projection without the hook registry — degrade
        # gracefully (cache invalidation becomes manual via sweep_expired).
        logger.warning(
            "DecisionProjection.register_post_apply_hook missing; "
            "explain cache invalidation will rely on TTL only"
        )
        return

    def _hook(decision: Decision) -> None:
        try:
            cache = _CACHE
            if cache is None:
                return
            cache.invalidate_for_decisions([decision.id])
        except Exception:  # noqa: BLE001
            logger.warning("explain cache invalidation hook failed", exc_info=True)

    projection.register_post_apply_hook(_hook)


__all__ = [
    "DEFAULT_TTL",
    "ExplainCache",
    "build_cache_key",
    "get_explain_cache",
    "reset_explain_cache_for_tests",
]
