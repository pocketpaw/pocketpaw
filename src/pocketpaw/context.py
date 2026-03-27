"""
Contextvars-based request tracing for async tasks.
Created: 2026-03-27
"""

import contextvars
from uuid import uuid4

# The shared request context contains the correlation ID for the current task.
# This ID is automatically propagated across awaited coroutines.
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def get_correlation_id() -> str | None:
    """Return the correlation ID for the current context."""
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> contextvars.Token:
    """Set the correlation ID for the current context."""
    return _correlation_id.set(value)


def generate_correlation_id() -> str:
    """Generate a new unique correlation ID."""
    return str(uuid4())
