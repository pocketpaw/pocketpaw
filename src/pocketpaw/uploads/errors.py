"""Error hierarchy for the upload adapter.

2026-09-14 (feat/uploads-multipart-adapter): added ``InvalidPart``. Multipart
is the first adapter path taking a caller-supplied *number*, and on the relay
path that number becomes a filename — so a bad one needs a catchable error, not
a bare ``ValueError``. Its ``code`` is the contract's wire code.
"""

from __future__ import annotations


class UploadError(Exception):
    """Base class for all upload-related errors."""

    code: str = "upload_error"


class TooLarge(UploadError):
    code = "too_large"


class UnsupportedMime(UploadError):
    code = "unsupported_mime"


class EmptyFile(UploadError):
    code = "empty"

    def __init__(self, message: str = "file is empty") -> None:
        super().__init__(message)


class NotFound(UploadError):
    code = "not_found"

    def __init__(self, message: str = "not found") -> None:
        super().__init__(message)


class AccessDenied(UploadError):
    code = "access_denied"

    def __init__(self, message: str = "access denied") -> None:
        super().__init__(message)


class InvalidPart(UploadError):
    """Part number outside 1..10000, or not a plain int.

    Refused before it can become a path segment on the relay path.
    """

    code = "multipart.invalid"

    def __init__(self, message: str = "invalid part number") -> None:
        super().__init__(message)


class StorageFailure(UploadError):
    code = "storage_error"
