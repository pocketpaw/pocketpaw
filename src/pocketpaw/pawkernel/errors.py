# pawkernel errors.
# Created: 2026-08-24 (feat/pawkernel-compose) — the composition kernel's
#   error surface: EffectRejected (SEMANTICS.md §3 dragon: no registration
#   while the owner is tearing down) and ApplyFailed (the wrapper the kernel
#   stores on a fiber that landed in FAILED).

from __future__ import annotations


class PawKernelError(Exception):
    """Base class for every error the composition kernel raises."""


class EffectRejected(PawKernelError):
    """Raised when an effect is created on an owner that can no longer hold one.

    SEMANTICS.md §3 (dragon): creating a new effect while the owner is
    UNLOADING MUST be rejected. Without this, a cleanup-time registration
    escapes the unload snapshot and leaks.
    """

    def __init__(self, owner: str, state: str) -> None:
        super().__init__(f"effect rejected: owner {owner!r} is {state}")
        self.owner = owner
        self.state = state


class DispatchModeConflict(PawKernelError):
    """Raised when an event name is used with two different dispatch modes.

    SEMANTICS.md §5: an event's dispatch mode is part of its public contract
    and MUST NOT vary by call site.
    """

    def __init__(self, event: str, declared: str, attempted: str) -> None:
        super().__init__(f"event {event!r} is declared {declared!r}, not {attempted!r}")
        self.event = event
        self.declared = declared
        self.attempted = attempted
