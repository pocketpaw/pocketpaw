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


class ObjectNotDescribed(StorageFailure):
    """The write SUCCEEDED and the follow-up describe did not.

    Raised by ``complete_multipart`` when the object assembled fine but the HEAD
    that reads back its size and content type failed. S3 forces this seam:
    ``complete_multipart_upload`` returns neither ``ContentLength`` nor
    ``ContentType``, so a separate HEAD is the only honest source for them, and
    that HEAD can fail on its own (a permissions gap, a blip) long after the
    bytes are safely stored.

    A distinct TYPE rather than a distinguishable message, because of what the
    caller does with the answer. Told this, it keeps the object and fills the
    size and mime from its own records. Told a plain ``StorageFailure``, it must
    assume nothing was written. Get that backwards in one direction and a
    successful multi-gigabyte upload is reported as failed, with the retry
    404ing on an upload id storage has already consumed; backwards in the other
    and a library row is written for an object that does not exist. Deciding
    that on a substring of an error message was a footgun waiting for the first
    person to reword the message.

    Subclasses ``StorageFailure`` so a caller that does not know about this case
    keeps the old, safe behaviour: it sees a storage failure and refuses.
    """

    code = "storage.not_described"
