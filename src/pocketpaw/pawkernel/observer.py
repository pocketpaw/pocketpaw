# pawkernel observability seam.
# Created: 2026-08-24 (feat/pawkernel-compose) — the kernel emits structured
#   lifecycle events through an Observer callable. The kernel itself knows
#   nothing about conformance trace strings; the conformance harness (and any
#   other consumer) translates these events into whatever it needs.
# Updated: 2026-08-24 (feat/pawkernel-compose) — added DisposerErrorEvent for
#   SEMANTICS.md §3's fourth dragon: a throwing disposer does not abort the
#   chain, but the error must be observable rather than swallowed. The kernel
#   reports each one as it contains it.
# Updated: 2026-08-25 (feat/pawkernel-compose) — added ServiceRejectedEvent for
#   SEMANTICS.md §1's one-authority-per-key-per-scope rule.

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


@dataclass(frozen=True)
class DisposerErrorEvent:
    """A disposer raised while unwinding (SEMANTICS.md §3, fourth dragon).

    The chain continues regardless and the fiber still reaches its target
    state; this event is how the error stays observable at the moment it is
    contained. ``effect`` is the name given to ``Context.effect``, or None.
    """

    owner: str
    effect: str | None
    error: BaseException


@dataclass(frozen=True)
class ServiceRejectedEvent:
    """A publish was refused: the key is already live in this scope (§1)."""

    owner: str
    key: str


KernelEvent = FiberStateEvent | ServiceEvent | DisposerErrorEvent | ServiceRejectedEvent
Observer = Callable[[KernelEvent], None]


def null_observer(event: KernelEvent) -> None:
    """Default observer: drop everything."""
    return None
