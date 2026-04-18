# pocketpaw.connectors.drive — Google Drive adapter module.
# Created: 2026-04-16 (Workstream C2 of the Org Architecture RFC).
#
# Public surface is the SourceAdapter (zero-copy live federation). The
# matching IngestAdapter (copy-on-ingest) lives alongside and will land in
# a follow-up PR once the ingest primitive from #939 settles.

from __future__ import annotations

from .auth import resolve_bearer_token
from .client import DriveClient, DriveFile, DriveRevision
from .errors import (
    DriveAuthError,
    DriveError,
    DriveNotFoundError,
    DriveRateLimitError,
)
from .source import DriveSourceAdapter

__all__ = [
    "DriveAuthError",
    "DriveClient",
    "DriveError",
    "DriveFile",
    "DriveNotFoundError",
    "DriveRateLimitError",
    "DriveRevision",
    "DriveSourceAdapter",
    "resolve_bearer_token",
]
