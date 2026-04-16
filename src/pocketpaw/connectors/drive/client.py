# Google Drive client for the SourceAdapter — sync, rate-limited, point-in-time aware.
# Created: 2026-04-16 (Workstream C2 of the Org Architecture RFC).
#
# This is the connector-layer client. It is deliberately separate from
# ``pocketpaw.integrations.gdrive.DriveClient``, which is coupled to the
# global OAuth token store used by the ``drive_*`` built-in tools. The
# retrieval router hands us a short-lived ``Credential`` at dispatch time
# (via the credential broker) so we never reach for a global token.
#
# Sync on purpose: ``SourceAdapter.query`` is sync in soul-protocol 0.3.1
# (the router runs adapters on a thread pool under the ``parallel`` strategy).
# Using ``httpx.Client`` keeps us in the same HTTP lib the rest of pocketpaw
# already depends on without pulling in ``google-api-python-client`` as a
# runtime requirement — the extra still ships it for parity with the
# enterprise Gmail/Calendar stacks, but it is not imported here.
#
# Rate limit policy: Drive raises 429 (quota exceeded) and 403 with a
# ``userRateLimitExceeded`` or ``rateLimitExceeded`` reason — Google's guide
# explicitly lists both. We back off exponentially with jitter up to
# ``max_retries``; anything else (401, 404, network error) is normalised to
# one of the errors in ``errors.py`` and raised.

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from .errors import (
    DriveAuthError,
    DriveError,
    DriveNotFoundError,
    DriveRateLimitError,
)

logger = logging.getLogger(__name__)

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"

# Rate-limit tuning. Drive's per-user quota resets every 100 seconds, so
# capping the retry budget around that window keeps a single wedged worker
# from holding onto a thread forever.
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_BACKOFF_S = 1.0
_DEFAULT_MAX_BACKOFF_S = 30.0


@dataclass
class DriveFile:
    """A single Drive file as returned by the API, normalised to our shape."""

    id: str
    name: str
    mime_type: str = ""
    modified_time: str = ""
    size: int | None = None
    web_view_link: str = ""
    revision_id: str | None = None
    owners: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DriveFile:
        size_raw = data.get("size")
        size = int(size_raw) if size_raw is not None else None
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            mime_type=str(data.get("mimeType", "")),
            modified_time=str(data.get("modifiedTime", "")),
            size=size,
            web_view_link=str(data.get("webViewLink", "")),
            revision_id=data.get("headRevisionId") or data.get("revisionId"),
            owners=list(data.get("owners", [])),
            raw=dict(data),
        )


@dataclass
class DriveRevision:
    """One row from files.revisions.list — trimmed to what we actually use."""

    id: str
    modified_time: str
    keep_forever: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DriveRevision:
        return cls(
            id=str(data.get("id", "")),
            modified_time=str(data.get("modifiedTime", "")),
            keep_forever=bool(data.get("keepForever", False)),
            raw=dict(data),
        )


class DriveClient:
    """Sync HTTP client for the Drive v3 API with exponential-backoff retries.

    The client is re-entrant-safe but not thread-safe — create one per thread
    (or per dispatch) when sharing is inconvenient. The ``RetrievalRouter``
    already isolates per-request state via its thread pool, so the intended
    pattern is ``DriveClient(token=credential.token)`` inside
    ``SourceAdapter.query``.
    """

    def __init__(
        self,
        token: str,
        *,
        http: httpx.Client | None = None,
        timeout_s: float = 15.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_backoff_s: float = _DEFAULT_BASE_BACKOFF_S,
        max_backoff_s: float = _DEFAULT_MAX_BACKOFF_S,
    ) -> None:
        if not token:
            raise DriveAuthError("DriveClient requires a non-empty OAuth bearer token")
        self._token = token
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout_s)
        self._max_retries = max_retries
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> DriveClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public API -------------------------------------------------------

    def list_files(
        self,
        *,
        query: str | None = None,
        page_size: int = 20,
        order_by: str = "modifiedTime desc",
        fields: str | None = None,
    ) -> list[DriveFile]:
        """List or search files. ``query`` is a Drive search expression."""
        fields = fields or (
            "files(id,name,mimeType,modifiedTime,size,webViewLink,headRevisionId,owners)"
        )
        params: dict[str, Any] = {
            "pageSize": max(1, min(page_size, 100)),
            "fields": fields,
            "orderBy": order_by,
        }
        if query:
            params["q"] = query
        data = self._request("GET", f"{_DRIVE_BASE}/files", params=params)
        return [DriveFile.from_api(f) for f in data.get("files", [])]

    def search(self, text: str, *, page_size: int = 20) -> list[DriveFile]:
        """Full-text search wrapper over ``list_files``."""
        safe = text.replace("'", "\\'")
        return self.list_files(query=f"fullText contains '{safe}'", page_size=page_size)

    def get_file(self, file_id: str) -> DriveFile:
        """Fetch metadata for a single file (no body)."""
        params = {
            "fields": ("id,name,mimeType,modifiedTime,size,webViewLink,headRevisionId,owners")
        }
        data = self._request("GET", f"{_DRIVE_BASE}/files/{file_id}", params=params)
        return DriveFile.from_api(data)

    def list_revisions(self, file_id: str, *, page_size: int = 50) -> list[DriveRevision]:
        """List the revision history of a file, oldest first (Drive's order)."""
        params = {
            "pageSize": max(1, min(page_size, 200)),
            "fields": "revisions(id,modifiedTime,keepForever)",
        }
        data = self._request("GET", f"{_DRIVE_BASE}/files/{file_id}/revisions", params=params)
        return [DriveRevision.from_api(r) for r in data.get("revisions", [])]

    def revision_at(self, file_id: str, point_in_time: datetime) -> DriveRevision | None:
        """Return the most recent revision at or before ``point_in_time``.

        Drive's revision timestamps are ISO-8601 UTC. We parse them with
        ``datetime.fromisoformat`` after swapping the trailing ``Z`` —
        anything malformed is silently skipped so a single bad row cannot
        break the walk.
        """
        if point_in_time.tzinfo is None:
            raise ValueError("point_in_time must be timezone-aware")
        revisions = self.list_revisions(file_id)
        best: DriveRevision | None = None
        for rev in revisions:
            ts = _parse_drive_ts(rev.modified_time)
            if ts is None:
                continue
            if ts <= point_in_time and (best is None or ts > _parse_drive_ts(best.modified_time)):  # type: ignore[operator]
                best = rev
        return best

    def get_content(
        self, file_id: str, *, revision_id: str | None = None, max_bytes: int = 1_000_000
    ) -> bytes:
        """Download the file body, optionally pinned to a specific revision.

        Google-native formats (Docs/Sheets/Slides) are exported to PDF — raw
        ``alt=media`` returns a 400 for those. ``max_bytes`` caps the download
        so the adapter never hands a 4 GB Drive video to the router.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        # Need mimeType to decide export vs raw download.
        meta = self.get_file(file_id)
        export_map = {
            "application/vnd.google-apps.document": "application/pdf",
            "application/vnd.google-apps.presentation": "application/pdf",
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        }
        if meta.mime_type in export_map:
            url = f"{_DRIVE_BASE}/files/{file_id}/export"
            params: dict[str, Any] = {"mimeType": export_map[meta.mime_type]}
        else:
            url = f"{_DRIVE_BASE}/files/{file_id}"
            params = {"alt": "media"}
            if revision_id:
                # Drive serves historical bytes from the revisions sub-resource.
                url = f"{_DRIVE_BASE}/files/{file_id}/revisions/{revision_id}"

        resp = self._raw_request("GET", url, params=params, headers=headers, stream=True)
        try:
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    break
            return bytes(buf[:max_bytes])
        finally:
            resp.close()

    # -- internals --------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a JSON request with retries and error normalisation."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        resp = self._raw_request(method, url, params=params, headers=headers, json=json)
        try:
            return resp.json()
        except ValueError as e:
            raise DriveError(f"Drive returned non-JSON body: {e}") from e
        finally:
            resp.close()

    def _raw_request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                if stream:
                    resp = self._http.send(
                        self._http.build_request(
                            method, url, params=params, headers=headers, json=json
                        ),
                        stream=True,
                    )
                else:
                    resp = self._http.request(
                        method, url, params=params, headers=headers, json=json
                    )
            except httpx.HTTPError as e:
                last_exc = e
                if attempt >= self._max_retries:
                    raise DriveError(f"Drive transport error: {e}") from e
                self._sleep_backoff(attempt)
                continue

            status = resp.status_code
            if status == 401:
                resp.close()
                raise DriveAuthError("Drive rejected credentials (HTTP 401)")
            if status == 404:
                resp.close()
                raise DriveNotFoundError(f"Drive resource not found: {url}")
            if status == 429 or (status == 403 and _is_rate_limit(resp)):
                reason = _reason_from(resp)
                resp.close()
                if attempt >= self._max_retries:
                    raise DriveRateLimitError(
                        f"Drive rate limit exceeded after {attempt + 1} attempts ({reason})"
                    )
                logger.info(
                    "drive rate-limited on attempt %d (%s); backing off", attempt + 1, reason
                )
                self._sleep_backoff(attempt)
                continue
            if status >= 400:
                body = _safe_body(resp)
                resp.close()
                raise DriveError(f"Drive error {status}: {body}")

            return resp

        # Should never land here — the loop either returns or raises.
        raise DriveError(f"drive request exhausted retries: {last_exc}")

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(
            self._max_backoff_s,
            self._base_backoff_s * (2**attempt) + random.uniform(0, 0.25),
        )
        time.sleep(delay)


# -- helpers ----------------------------------------------------------------


def _parse_drive_ts(value: str) -> datetime | None:
    """Parse a Drive ``modifiedTime`` string into a tz-aware datetime."""
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _is_rate_limit(resp: httpx.Response) -> bool:
    """Drive packs quota errors into 403 with a specific reason string."""
    try:
        body = resp.json()
    except ValueError:
        return False
    errors = body.get("error", {}).get("errors", []) if isinstance(body, dict) else []
    for err in errors:
        if not isinstance(err, dict):
            continue
        reason = err.get("reason", "")
        if reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
            return True
    return False


def _reason_from(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if not isinstance(body, dict):
        return f"HTTP {resp.status_code}"
    errors = body.get("error", {}).get("errors", [])
    if errors and isinstance(errors[0], dict):
        return errors[0].get("reason", f"HTTP {resp.status_code}")
    return f"HTTP {resp.status_code}"


def _safe_body(resp: httpx.Response) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return f"<unreadable body, status={resp.status_code}>"
