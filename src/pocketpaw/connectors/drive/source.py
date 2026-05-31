# DriveSourceAdapter — zero-copy federation over the Drive v3 API.
# Created: 2026-04-16 (Workstream C2 of the Org Architecture RFC).
#
# This is the first concrete ``SourceAdapter`` in pocketpaw. The pattern
# established here will be followed for Salesforce, Slack, Gmail, etc.:
#
#   1. Accept a ``Credential`` from the router's broker.
#   2. Resolve a bearer token via ``auth.resolve_bearer_token``.
#   3. Build a scoped sync HTTP client.
#   4. Translate the request's ``query`` into a source-native query string.
#   5. Return ``RetrievalCandidate`` rows with ``content`` shaped as a
#      DataRef dict (live pointer, not copied bytes) and ``as_of`` pinned
#      to the effective point-in-time.
#   6. Empty results return ``[]`` — never raise.
#
# Point-in-time handling: soul-protocol 0.3.1's ``RetrievalRequest`` does
# not yet carry a structured ``point_in_time`` field (tracked for a future
# soul-protocol bump). We support it today via a lightweight convention
# embedded in the query string:
#
#     "@at=2026-04-01T12:00:00Z | Q3 forecast"
#
# ``_split_point_in_time`` peels the prefix off and leaves the remainder
# as the free-text query. When present, we walk the file's revision list
# and pin the candidate to the matching historical revision id.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from soul_protocol.engine.retrieval import Credential
from soul_protocol.spec.retrieval import RetrievalCandidate, RetrievalRequest

from .auth import resolve_bearer_token
from .client import DriveClient, DriveFile, DriveRevision
from .errors import DriveAuthError, DriveError

logger = logging.getLogger(__name__)

_POINT_IN_TIME_PREFIX = "@at="


class DriveSourceAdapter:
    """``SourceAdapter`` implementation for Google Drive.

    Fields:
        source_name: Key used when the router matches ``sources_failed``
            entries and ``CandidateSource.adapter_ref`` pointers. Defaults
            to ``"drive"`` — keep stable unless running side-by-side with
            a second Drive instance (e.g. personal vs enterprise).
    """

    # SourceAdapter Protocol attribute — we emit live DataRef payloads.
    supports_dataref: bool = True

    def __init__(
        self,
        *,
        source_name: str = "drive",
        client_factory: Any = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._source_name = source_name
        # ``client_factory`` is injectable so tests can swap in a stub
        # without monkey-patching module globals. Signature: (token) -> DriveClient.
        self._client_factory = client_factory or (lambda token: DriveClient(token))
        self._env = env

    def query(
        self,
        request: RetrievalRequest,
        credential: Credential | None,
    ) -> list[RetrievalCandidate]:
        try:
            token = resolve_bearer_token(credential, env=self._env)
        except DriveAuthError:
            # Let the router surface this as a per-source failure. The
            # router catches the exception and records it — we do NOT want
            # to swallow it here because that would hide credential issues.
            raise

        point_in_time, text_query = _split_point_in_time(request.query)
        drive_query = _translate_query(text_query)

        client = self._client_factory(token)
        try:
            files = client.list_files(
                query=drive_query,
                page_size=max(1, min(request.limit, 100)),
            )
        except DriveError:
            # Preserve the error type; the router records the reason.
            raise

        if not files:
            return []

        as_of = point_in_time or datetime.now(UTC)
        candidates: list[RetrievalCandidate] = []
        for position, drive_file in enumerate(files):
            revision = None
            if point_in_time is not None:
                try:
                    revision = client.revision_at(drive_file.id, point_in_time)
                except DriveError as e:
                    logger.warning(
                        "revision lookup failed for %s: %s; emitting head candidate",
                        drive_file.id,
                        e,
                    )
            score = _score_for_position(position, len(files))
            candidates.append(
                RetrievalCandidate(
                    source=self._source_name,
                    content=_dataref_payload(
                        drive_file,
                        revision=revision,
                        request_scopes=list(request.scopes),
                    ),
                    score=score,
                    as_of=as_of,
                    cached=False,
                )
            )
        return candidates


# -- helpers ---------------------------------------------------------------


def _split_point_in_time(raw: str) -> tuple[datetime | None, str]:
    """Peel an optional ``@at=<iso>|rest`` prefix off the request query."""
    stripped = raw.strip()
    if not stripped.startswith(_POINT_IN_TIME_PREFIX):
        return None, raw
    remainder = stripped[len(_POINT_IN_TIME_PREFIX) :]
    if "|" not in remainder:
        return _parse_iso(remainder.strip()), ""
    ts_text, rest = remainder.split("|", 1)
    return _parse_iso(ts_text.strip()), rest.strip()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _translate_query(text: str) -> str | None:
    """Map a free-text query to Drive's search syntax.

    If the caller already passed a Drive-native expression (contains an
    operator like ``fullText``, ``mimeType``, ``name``, ``owners``, or
    ``and``) we pass it through unchanged. Otherwise we wrap the text in
    ``fullText contains '...'`` with single quotes escaped.
    """
    if not text:
        return None
    lowered = text.lower()
    drive_operators = (
        "fulltext contains",
        "name contains",
        "mimetype",
        "owners in",
        "modifiedtime",
        "sharedwithme",
        "starred",
    )
    if any(op in lowered for op in drive_operators):
        return text
    escaped = text.replace("'", "\\'")
    return f"fullText contains '{escaped}'"


def _dataref_payload(
    drive_file: DriveFile,
    *,
    revision: DriveRevision | None,
    request_scopes: list[str],
) -> dict[str, Any]:
    """Build the DataRef-shaped dict the router hands to the decision-maker.

    Shape is deliberately loose (soul-protocol's ``RetrievalCandidate.content``
    is ``dict[str, Any]`` so the router never inspects it). We follow the
    Zero-Copy convention agreed in the RFC: ``kind="dataref"``, plus enough
    identifiers for a downstream resolver to fetch live bytes on demand.
    """
    ref: dict[str, Any] = {
        "kind": "dataref",
        "source": "drive",
        "id": drive_file.id,
        "name": drive_file.name,
        "mime_type": drive_file.mime_type,
        "modified_time": drive_file.modified_time,
        "web_view_link": drive_file.web_view_link,
        "scopes": request_scopes,
    }
    if drive_file.size is not None:
        ref["size"] = drive_file.size
    if drive_file.owners:
        ref["owners"] = drive_file.owners
    if revision is not None:
        ref["revision_id"] = revision.id
        ref["revision_modified_time"] = revision.modified_time
    elif drive_file.revision_id:
        ref["revision_id"] = drive_file.revision_id
    return ref


def _score_for_position(position: int, total: int) -> float:
    """Linear falloff so Drive's native ordering is preserved after merge.

    Drive returns files sorted by ``modifiedTime desc`` (or by Google's
    relevance ranking when ``fullText`` is used). We map that ordering into
    a ``[0.1, 1.0]`` score so multi-source merges with other adapters
    still respect Drive's intent without swamping projection sources that
    produce true relevance scores.
    """
    if total <= 1:
        return 1.0
    # position 0 -> 1.0, position total-1 -> 0.1
    return max(0.1, 1.0 - (position / (total - 1)) * 0.9)
