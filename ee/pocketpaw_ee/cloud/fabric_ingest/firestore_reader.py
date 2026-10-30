# firestore_reader.py — Concrete FirestoreReader over google-cloud-firestore.
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
#
# Wraps the async google-cloud-firestore client behind the ``FirestoreReader``
# Protocol the service depends on. Kept in its own module so the google import
# is fully isolated: the service imports this lazily and only when no fake
# reader was injected, so pure-unit tests never touch google and the dependency
# stays OPTIONAL — ``google-cloud-firestore`` is an optional extra of the ee
# package (``pocketpaw-ee[firestore]``). If it isn't installed, constructing
# this reader raises a clear, actionable install error rather than an opaque
# ImportError deep in the call path.
#
# Cursor semantics: ``read_collection`` orders ascending by the configured
# ``cursor_field`` and returns only documents whose value is strictly greater
# than the supplied cursor (everything when the cursor is empty — backfill).
# Each document is shaped into the ``{path, data, update_time}`` dict the worker
# expects, where ``update_time`` is the snapshot update time as RFC3339 — the
# cursor fallback the service uses when a document has no ``cursor_field`` value.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "google-cloud-firestore is not installed. The Firestore→Fabric ingest "
    "worker needs it. Install the optional extra: `uv sync --group ee "
    "--extra firestore` (or `pip install 'pocketpaw-ee[firestore]'`)."
)


def _require_firestore() -> Any:
    """Import google-cloud-firestore's async client, or raise a clear error."""
    try:
        from google.cloud import firestore  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — exercised only without the dep
        raise RuntimeError(_INSTALL_HINT) from exc
    return firestore


class GoogleFirestoreReader:
    """Async ``FirestoreReader`` backed by ``google.cloud.firestore``.

    Credentials resolve through Application Default Credentials (the standard
    GOOGLE_APPLICATION_CREDENTIALS / workload-identity path); this reader takes
    no secrets of its own. ``project`` is optional — when omitted the client
    picks it up from the ambient credentials/environment.
    """

    def __init__(self, project: str | None = None) -> None:
        firestore = _require_firestore()
        # AsyncClient is the async surface; the sync Client would block the loop.
        self._client = firestore.AsyncClient(project=project)

    async def read_collection(
        self,
        collection: str,
        *,
        cursor_field: str,
        cursor: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = self._client.collection(collection)
        # Order + lower-bound on the cursor field when one is configured. With
        # no cursor_field we can't do an incremental window, so we read the page
        # as-is (the service still de-dupes via upsert, so this is safe — it
        # just re-reads each tick; configuring a cursor_field is recommended).
        if cursor_field:
            query = query.order_by(cursor_field)
            if cursor:
                query = query.start_after({cursor_field: cursor})
        query = query.limit(limit)

        docs: list[dict[str, Any]] = []
        async for snapshot in query.stream():
            update_time = getattr(snapshot, "update_time", None)
            docs.append(
                {
                    "path": snapshot.reference.path,
                    "data": snapshot.to_dict() or {},
                    "update_time": _iso(update_time),
                }
            )
        return docs


def _iso(update_time: Any) -> str:
    """Render a Firestore snapshot update_time as an RFC3339 string.

    Firestore returns a ``DatetimeWithNanoseconds`` (a datetime subclass) or a
    proto Timestamp depending on the client version; both expose ``isoformat``
    or ``rfc3339``. Fall back to ``str`` so an unexpected shape never crashes
    the read.
    """
    if update_time is None:
        return ""
    for attr in ("rfc3339", "isoformat"):
        fn = getattr(update_time, attr, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:  # noqa: BLE001 — fall through to str()
                pass
    return str(update_time)


__all__ = ["GoogleFirestoreReader"]
