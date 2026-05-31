# Drive connector error hierarchy — normalised surface for API failures.
# Created: 2026-04-16 (Workstream C2 of the Org Architecture RFC).
#
# Kept deliberately small. Callers should be able to tell "bad token" from
# "rate limited" from "not found" without string-sniffing.

from __future__ import annotations


class DriveError(Exception):
    """Base class for all errors raised by the Drive connector."""


class DriveAuthError(DriveError):
    """The OAuth token is missing, expired, or rejected by Drive."""


class DriveRateLimitError(DriveError):
    """Drive returned 429 (or 403 quota reason) after retries were exhausted."""


class DriveNotFoundError(DriveError):
    """The requested file, revision, or permission does not exist."""
