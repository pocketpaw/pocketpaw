"""Unified error hierarchy for the cloud module.

Every domain package raises these instead of raw HTTPException so that
error handling, logging, and API responses stay consistent.
"""

from __future__ import annotations


class CloudError(Exception):
    """Base cloud error with status_code, code (machine-readable), message (human-readable)."""

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
