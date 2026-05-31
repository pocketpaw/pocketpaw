"""Error hierarchy for the upload adapter."""

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


class StorageFailure(UploadError):
    code = "storage_error"
