# Discriminating unit tests for the paw composition kernel.
# Created: 2026-08-24 (feat/pawkernel-compose) — the conformance fixtures are
#   the acceptance criteria, but two of their rules are not actually observable
#   through the declared traces: "dispose during LOADING must not run cleanup
#   concurrently with the rest of apply" (dispose-during-load passes even with
#   the await removed, because its only effect is collected before the delay)
#   and "a dependent must not load mid-apply" (load-order-inject passes even
#   with the deferred-work queue removed, because its provider never awaits
#   after publishing). These tests close both gaps, and cover the asyncio
#   cancellation behaviour and the event modes no fixture exercises.

from __future__ import annotations

import asyncio

import pytest

from pocketpaw.pawkernel import (
    DispatchModeConflict,
    EffectRejected,
    FiberState,
    Inject,
    Kernel,
    SimplePlugin,
)
from pocketpaw.pawkernel.observer import FiberStateEvent, ServiceEvent


def _tracing_kernel() -> tuple[Kernel, list[str]]:
    trace: list[str] = []

    def observe(event: object) -> None:
        if isinstance(event, FiberStateEvent):
            trace.append(f"{event.fiber}:{event.state}")
        elif isinstance(event, ServiceEvent):
            trace.append(f"{event.owner}:{event.kind}:{event.key}")

    return Kernel(observer=observe), trace


# --------------------------------------------------------------------------
# §4 dragon — dispose during LOADING
# --------------------------------------------------------------------------
async def test_dispose_during_load_awaits_the_whole_apply() -> None:
    """Cleanup must not interleave with the remainder of apply.

    The plugin registers one effect, awaits, then registers a second. A
    runtime that tears down concurrently would never see ``e2`` collected —
    or would reject it — and would dispose ``e1`` before apply finished.
    """
    kernel, trace = _tracing_kernel()
    log: list[str] = []

    def make_effect(ctx, name: str) -> None:
        def setup():
            log.append(f"setup:{name}")
            return lambda: log.append(f"dispose:{name}")

        ctx.effect(setup)

    async def apply(ctx) -> None:
        make_effect(ctx, "e1")
        await asyncio.sleep(0.05)
        make_effect(ctx, "e2")

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply), nowait=True)
    kernel.spawn(fiber.dispose())
    await kernel.settle()

    assert log == ["setup:e1", "setup:e2", "dispose:e2", "dispose:e1"]
    assert fiber.state == FiberState.DISPOSED
    assert trace == ["a:LOADING", "a:UNLOADING", "a:DISPOSED"]
    assert not kernel.errors


async def test_dependent_does_not_load_mid_apply() -> None:
    """A dependent activated by a mid-apply publish waits for the provider.

    The provider publishes and then awaits. If the dependent's re-check ran
    on the event loop instead of the kernel's deferred queue, ``b:LOADING``
    would land inside the provider's apply — before ``a:ACTIVE``.
    """
    kernel, trace = _tracing_kernel()

    async def provider(ctx) -> None:
        ctx.provide("svcA", "impl")
        await asyncio.sleep(0.01)

    await kernel.mount(
        SimplePlugin(name="b", apply_fn=lambda ctx: None, inject=Inject(required=("svcA",)))
    )
    await kernel.mount(SimplePlugin(name="a", apply_fn=provider))
    await kernel.settle()

    assert trace == [
        "b:PENDING",
        "a:LOADING",
        "a:provide:svcA",
        "a:ACTIVE",
        "b:LOADING",
        "b:ACTIVE",
    ]


# --------------------------------------------------------------------------
# §3 — effects, disposal, cancellation
# --------------------------------------------------------------------------
async def test_disposer_runs_at_most_once() -> None:
    kernel, _ = _tracing_kernel()
    calls: list[int] = []

    fiber = await kernel.mount(
        SimplePlugin(
            name="a",
            apply_fn=lambda ctx: ctx.effect(lambda: lambda: calls.append(1)),
        )
    )
    await fiber.dispose()
    await fiber.dispose()
    await fiber.dispose()
    assert calls == [1]
    assert fiber.state == FiberState.DISPOSED


async def test_async_disposer_is_not_abandoned_when_the_caller_is_cancelled() -> None:
    """Repeated disposal under cancellation is idempotent, and cleanup finishes."""
    kernel, _ = _tracing_kernel()
    finished: list[str] = []

    def apply(ctx) -> None:
        async def dispose() -> None:
            await asyncio.sleep(0.05)
            finished.append("done")

        ctx.effect(lambda: dispose)

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))

    first = asyncio.ensure_future(fiber.dispose())
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    # The cancelled caller must not have abandoned the awaiting disposer.
    await fiber.dispose()
    assert finished == ["done"]
    assert fiber.state == FiberState.DISPOSED


async def test_effect_is_rejected_once_the_fiber_has_settled() -> None:
    kernel, _ = _tracing_kernel()
    holder: dict[str, object] = {}
    fiber = await kernel.mount(
        SimplePlugin(name="a", apply_fn=lambda ctx: holder.setdefault("ctx", ctx))
    )
    await fiber.dispose()
    with pytest.raises(EffectRejected):
        holder["ctx"].effect(lambda: None)  # type: ignore[attr-defined]


async def test_failed_fiber_holds_no_live_effects() -> None:
    kernel, trace = _tracing_kernel()
    log: list[str] = []

    def apply(ctx) -> None:
        ctx.effect(lambda: lambda: log.append("rollback"))
        raise RuntimeError("boom")

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))
    await kernel.settle()

    assert fiber.state == FiberState.FAILED
    assert log == ["rollback"]
    assert fiber._effects == []
    assert isinstance(fiber.error, RuntimeError)
    # FAILED is reached directly from LOADING — no UNLOADING transition.
    assert trace == ["a:LOADING", "a:FAILED"]


async def test_a_provided_service_is_withdrawn_when_its_provider_unloads() -> None:
    kernel, _ = _tracing_kernel()
    fiber = await kernel.mount(
        SimplePlugin(name="a", apply_fn=lambda ctx: ctx.provide("svcA", "impl"))
    )
    assert kernel.root.get("svcA") == "impl"
    await fiber.dispose()
    assert kernel.root.get("svcA") is None


# --------------------------------------------------------------------------
# §1 — context and isolation
# --------------------------------------------------------------------------
async def test_absent_key_resolves_to_none() -> None:
    kernel, _ = _tracing_kernel()
    assert kernel.root.get("nothing-here") is None


async def test_isolate_leaves_the_parent_and_other_keys_alone() -> None:
    kernel, _ = _tracing_kernel()
    kernel.root.provide("svcA", "rootimpl")
    kernel.root.provide("svcB", "shared")

    scope = kernel.root.isolate("svcA", label="s1")
    scope.provide("svcA", "isoimpl")

    assert scope.get("svcA") == "isoimpl"
    assert kernel.root.get("svcA") == "rootimpl"
    assert scope.get("svcB") == "shared"


# --------------------------------------------------------------------------
# §2 — injection
# --------------------------------------------------------------------------
async def test_optional_inject_never_gates_activation() -> None:
    kernel, _ = _tracing_kernel()
    seen: list[object] = []

    fiber = await kernel.mount(
        SimplePlugin(
            name="a",
            apply_fn=lambda ctx: seen.append(ctx.get("maybe")),
            inject=Inject(optional=("maybe",)),
        )
    )
    await kernel.settle()
    assert fiber.state == FiberState.ACTIVE
    assert seen == [None]


async def test_withdrawn_dependency_returns_to_pending_and_reactivates() -> None:
    kernel, trace = _tracing_kernel()
    kernel.root.provide("svcX", "impl")
    fiber = await kernel.mount(
        SimplePlugin(name="b", apply_fn=lambda ctx: None, inject=Inject(required=("svcX",)))
    )
    await kernel.settle()
    assert fiber.state == FiberState.ACTIVE

    kernel.root.withdraw("svcX")
    await kernel.settle()
    assert fiber.state == FiberState.PENDING

    kernel.root.provide("svcX", "impl-again")
    await kernel.settle()
    assert fiber.state == FiberState.ACTIVE
    assert FiberState.DISPOSED not in trace


# --------------------------------------------------------------------------
# §5 — the dispatch modes no fixture exercises
# --------------------------------------------------------------------------
async def test_emit_is_fire_and_forget_in_registration_order() -> None:
    kernel, _ = _tracing_kernel()
    seen: list[str] = []
    kernel.root.on("ev", lambda v: seen.append(f"1:{v}"), mode="emit")
    kernel.root.on("ev", lambda v: seen.append(f"2:{v}"), mode="emit")
    assert kernel.root.emit("ev", "x") is None
    assert seen == ["1:x", "2:x"]


async def test_parallel_awaits_every_listener() -> None:
    kernel, _ = _tracing_kernel()
    done: list[str] = []

    async def slow(_v: object) -> None:
        await asyncio.sleep(0.02)
        done.append("slow")

    async def quick(_v: object) -> None:
        done.append("quick")

    kernel.root.on("ev", slow, mode="parallel")
    kernel.root.on("ev", quick, mode="parallel")
    await kernel.root.parallel("ev", None)
    assert sorted(done) == ["quick", "slow"]


async def test_serial_returns_the_first_non_absent_result() -> None:
    kernel, _ = _tracing_kernel()
    seen: list[str] = []

    def first(_v: object) -> None:
        seen.append("first")
        return None

    def second(_v: object) -> str:
        seen.append("second")
        return "hit"

    def third(_v: object) -> str:  # pragma: no cover - must never run
        seen.append("third")
        return "late"

    for cb in (first, second, third):
        kernel.root.on("ev", cb, mode="serial")
    assert await kernel.root.serial("ev", None) == "hit"
    assert seen == ["first", "second"]


async def test_waterfall_with_no_listeners_returns_the_threaded_value() -> None:
    kernel, _ = _tracing_kernel()
    assert kernel.root.waterfall("ev", "base") == "base"


async def test_dispatch_mode_cannot_vary_by_call_site() -> None:
    kernel, _ = _tracing_kernel()
    kernel.root.on("ev", lambda v: None, mode="emit")
    with pytest.raises(DispatchModeConflict):
        kernel.root.on("ev", lambda v, *, next: next(), mode="waterfall")
    with pytest.raises(DispatchModeConflict):
        kernel.root.waterfall("ev", "base")
