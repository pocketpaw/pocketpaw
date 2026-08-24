# pawkernel observability seam.
# Created: 2026-08-24 (feat/pawkernel-compose) — the kernel emits structured
#   lifecycle events through an Observer callable. The kernel itself knows
#   nothing about conformance trace strings; the conformance harness (and any
#   other consumer) translates these events into whatever it needs.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class FiberStateEvent:
    """A fiber entered a new lifecycle state (SEMANTICS.md §4)."""

    fiber: str
    state: str


@dataclass(frozen=True)
class ServiceEvent:
    """A service was published or withdrawn (SEMANTICS.md §1).

    ``owner`` is the plugin name when the change was made from inside a
    plugin's own context, and the context label (``root``, or an isolate's
    label) when it was made directly on a context.
    """

    owner: str
    key: str
    kind: str  # "provide" | "withdraw"


KernelEvent = FiberStateEvent | ServiceEvent
Observer = Callable[[KernelEvent], None]


def null_observer(event: KernelEvent) -> None:
    """Default observer: drop everything."""
    return None
