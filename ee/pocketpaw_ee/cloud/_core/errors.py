"""Canonical error hierarchy for ee/cloud.

Routers must never raise `HTTPException`; services raise these
`CloudError` subclasses and `_core.http.cloud_error_handler` maps them
to JSON responses.

Re-exports remain accessible via `ee.cloud.shared.errors` (a shim) for
the transition period; new code should import from this module.
"""

from __future__ import annotations


class CloudError(Exception):
    """Base cloud error with status_code, code (machine-readable),
    message (human-readable)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict:
        """Return a JSON-serializable error envelope."""
        return {"error": {"code": self.code, "message": self.message}}


class NotFound(CloudError):
    """Resource not found (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        code = f"{resource}.not_found"
        if resource_id:
            message = f"{resource} '{resource_id}' not found"
        else:
            message = f"{resource} not found"
        super().__init__(404, code, message)


class Forbidden(CloudError):
    """Access denied (403)."""

    def __init__(self, code: str, message: str = "Access denied") -> None:
        super().__init__(403, code, message)


class ConflictError(CloudError):
    """Resource conflict (409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class ValidationError(CloudError):
    """Validation failure (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(422, code, message)


class SeatLimitError(CloudError):
    """Seat/billing limit reached (402)."""

    def __init__(self, seats: int) -> None:
        super().__init__(402, "billing.seat_limit", f"Seat limit of {seats} reached")


class RateLimited(CloudError):
    """Rate limit exceeded (429)."""

    def __init__(self, code: str, message: str = "Rate limit exceeded") -> None:
        super().__init__(429, code, message)


class Internal(CloudError):
    """Unexpected internal error (500). Use sparingly — prefer specific codes."""

    def __init__(self, code: str = "internal", message: str = "Internal server error") -> None:
        super().__init__(500, code, message)


def with_cause(error: CloudError, cause: BaseException) -> CloudError:
    """Attach an underlying exception for log context. Returns the same error
    so it can be used fluently: `raise with_cause(NotFound(...), exc)`.

    The cause is stored on `__cause__` (Python's standard exception-chaining
    slot). The `to_dict()` envelope sent to clients still contains only
    `code` and `message`; the cause is not leaked.
    """
    error.__cause__ = cause
    return error


__all__ = [
    "CloudError",
    "ConflictError",
    "Forbidden",
    "Internal",
    "NotFound",
    "RateLimited",
    "SeatLimitError",
    "ValidationError",
    "with_cause",
]
