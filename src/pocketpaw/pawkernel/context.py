# pawkernel context — SEMANTICS.md §1.
# Created: 2026-08-24 (feat/pawkernel-compose) — a context is a repository of
#   services resolved by key. Child contexts inherit the parent's services;
#   an absent key resolves to None rather than raising; isolate(key) gives a
#   child a fresh scope for that one key while every other key still resolves
#   through the parent.
# Updated: 2026-08-25 (feat/pawkernel-compose) — §1 one authority per key per
#   scope: publishing a key already live in the same scope is rejected, and
#   the provide-disposer no longer restores a previous value. The old
#   unconditional restore let an earlier provider clobber a live later one on
#   unload, and a later one resurrect the dead earlier one.

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pocketpaw.pawkernel.errors import DuplicateProvider
from pocketpaw.pawkernel.events import EMIT
from pocketpaw.pawkernel.observer import ServiceEvent, ServiceRejectedEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pocketpaw.pawkernel.fiber import Fiber
    from pocketpaw.pawkernel.kernel import Kernel

ABSENT = None


class Cell:
    """One service slot. Fibers that inject the key watch the cell."""

    __slots__ = ("key", "value", "watchers", "_kernel")

    def __init__(self, key: str, kernel: Kernel) -> None:
        self.key = key
        self.value: Any = ABSENT
        self.watchers: list[Fiber] = []
        self._kernel = kernel

    def set(self, value: Any) -> None:
        if value is self.value:
            return
        self.value = value
        for fiber in list(self.watchers):
            self._kernel.defer(fiber.recheck)

    def watch(self, fiber: Fiber) -> None:
        if fiber not in self.watchers:
            self.watchers.append(fiber)

    def unwatch(self, fiber: Fiber) -> None:
        if fiber in self.watchers:
            self.watchers.remove(fiber)


class Context:
    """A service repository. Never resolve by concrete type — only by key."""

    def __init__(
        self,
        kernel: Kernel,
        parent: Context | None = None,
        label: str = "root",
        fiber: Fiber | None = None,
    ) -> None:
        self._kernel = kernel
        self._parent = parent
        self.label = label
        self._fiber = fiber
        # Keys this context owns outright. The root owns every key it is asked
        # for; an isolate owns exactly the key it isolated.
        self._cells: dict[str, Cell] = {}

    # -- structure --------------------------------------------------------
    @property
    def fiber(self) -> Fiber | None:
        return self._fiber

    def child(self, label: str | None = None, fiber: Fiber | None = None) -> Context:
        return Context(
            kernel=self._kernel,
            parent=self,
            label=label if label is not None else self.label,
            fiber=fiber,
        )

    def isolate(self, key: str, label: str | None = None) -> Context:
        """Child context in which ``key`` resolves against a fresh scope."""
        scope = self.child(label=label)
        scope._cells[key] = Cell(key, self._kernel)
        return scope

    def _cell(self, key: str) -> Cell:
        if key in self._cells:
            return self._cells[key]
        if self._parent is not None:
            return self._parent._cell(key)
        cell = Cell(key, self._kernel)
        self._cells[key] = cell
        return cell

    # -- services ---------------------------------------------------------
    def get(self, key: str) -> Any:
        """Resolve ``key``. An absent key yields None; it never raises."""
        return self._cell(key).value

    def _owner_name(self) -> str:
        return self._fiber.name if self._fiber is not None else self.label

    def provide(self, key: str, value: Any) -> Callable[[], None]:
        """Publish ``value`` under ``key``. Publishing is a reversible effect."""
        cell = self._cell(key)
        owner = self._owner_name()

        # §1: one authority per key per scope. A key already live in THIS
        # scope may not be claimed by a second provider. Rejecting is the
        # only safe answer — every restore policy is wrong in one direction
        # or the other: restoring the previous value on unload clobbers a
        # newer provider and resurrects a dead one, while never restoring
        # downgrades the key to absent while an older provider is still live.
        # A different implementation of the same key belongs in isolate(key).
        if cell.value is not ABSENT:
            self._kernel.notify(ServiceRejectedEvent(owner=owner, key=key))
            raise DuplicateProvider(key=key, owner=owner)

        def setup() -> Callable[[], None]:
            cell.set(value)
            self._kernel.notify(ServiceEvent(owner=owner, key=key, kind="provide"))

            def dispose() -> None:
                # No "previous" to restore: the rule above guarantees this
                # scope held nothing when we claimed the key.
                cell.set(ABSENT)
                self._kernel.notify(ServiceEvent(owner=owner, key=key, kind="withdraw"))

            return dispose

        return self.effect(setup)

    def withdraw(self, key: str) -> None:
        """Withdraw ``key`` from this context's scope."""
        cell = self._cell(key)
        cell.set(ABSENT)
        self._kernel.notify(ServiceEvent(owner=self._owner_name(), key=key, kind="withdraw"))

    # -- effects ----------------------------------------------------------
    def effect(self, setup: Callable[[], Any], name: str | None = None) -> Any:
        """Run ``setup``; its optional return value is the disposer (§3)."""
        if self._fiber is not None:
            return self._fiber.effect(setup, name=name)
        # Root-level effects have no fiber to tear them down; the caller owns
        # the returned disposer.
        return setup()

    # -- events -----------------------------------------------------------
    def on(self, event: str, callback: Callable[..., Any], mode: str = EMIT) -> Any:
        """Register a listener. Registration is an effect (§5)."""

        def setup() -> Callable[[], None]:
            return self._kernel.bus.on(event, callback, mode=mode)

        return self.effect(setup)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._kernel.bus.emit(event, *args, **kwargs)

    def waterfall(self, event: str, *args: Any) -> Any:
        return self._kernel.bus.waterfall(event, *args)

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> None:
        await self._kernel.bus.parallel(event, *args, **kwargs)

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> Any:
        return await self._kernel.bus.serial(event, *args, **kwargs)

    # -- composition ------------------------------------------------------
    async def plugin(self, plugin: Any, nowait: bool = False) -> Fiber:
        """Mount ``plugin`` as a child of this context."""
        return await self._kernel.mount(plugin, ctx=self, nowait=nowait)
