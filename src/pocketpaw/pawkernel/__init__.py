# pawkernel — the paw composition kernel (reference runtime).
# Created: 2026-08-24 (feat/pawkernel-compose) — Python implementation of the
#   language-neutral composition semantics in paw-compose/SEMANTICS.md:
#   key-resolved contexts with isolate scopes (§1), injection-derived load
#   order (§2), reversible effects with LIFO run-once disposal (§3), the fiber
#   lifecycle including the dispose-during-load and failed-apply dragons (§4),
#   and four event dispatch modes (§5). Conformance is proved by the fixtures
#   under tests/conformance/paw-compose/, run by tests/pawkernel/.

from pocketpaw.pawkernel.context import Context
from pocketpaw.pawkernel.errors import (
    DispatchModeConflict,
    EffectRejected,
    PawKernelError,
)
from pocketpaw.pawkernel.events import EMIT, PARALLEL, SERIAL, WATERFALL, EventBus
from pocketpaw.pawkernel.fiber import (
    Disposer,
    Fiber,
    FiberState,
    Inject,
    Plugin,
    SimplePlugin,
)
from pocketpaw.pawkernel.kernel import Kernel
from pocketpaw.pawkernel.observer import (
    FiberStateEvent,
    KernelEvent,
    Observer,
    ServiceEvent,
)

__all__ = [
    "EMIT",
    "PARALLEL",
    "SERIAL",
    "WATERFALL",
    "Context",
    "DispatchModeConflict",
    "Disposer",
    "EffectRejected",
    "EventBus",
    "Fiber",
    "FiberState",
    "FiberStateEvent",
    "Inject",
    "Kernel",
    "KernelEvent",
    "Observer",
    "PawKernelError",
    "Plugin",
    "ServiceEvent",
    "SimplePlugin",
]
