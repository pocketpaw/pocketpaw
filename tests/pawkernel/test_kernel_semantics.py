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
from typing import Any

import pytest

from pocketpaw.pawkernel import (
    DispatchModeConflict,
    DuplicateProvider,
    EffectRejected,
    FiberState,
    Inject,
    Kernel,
    SimplePlugin,
)
from pocketpaw.pawkernel.observer import (
    DisposerErrorEvent,
    FiberStateEvent,
    ServiceEvent,
)


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
# §5 — dispatch modes. The three added in the 2026-08-24 amendment now have
# fixtures; these cover what those fixtures still cannot observe.
# --------------------------------------------------------------------------
async def test_emit_is_fire_and_forget_in_registration_order() -> None:
    kernel, _ = _tracing_kernel()
    seen: list[str] = []
    kernel.root.on("ev", lambda v: seen.append(f"1:{v}"), mode="emit")
    kernel.root.on("ev", lambda v: seen.append(f"2:{v}"), mode="emit")
    assert kernel.root.emit("ev", "x") is None
    assert seen == ["1:x", "2:x"]


async def test_parallel_fans_out_concurrently_and_awaits_every_listener() -> None:
    """Both that every listener settles AND that they genuinely overlap.

    `parallel-awaits-all` compares as a multiset, so it cannot tell a
    concurrent fan-out from a runtime that awaits each listener in turn —
    both produce the same bag of tokens. Asserting the interleaving is what
    makes the difference observable.
    """
    kernel, _ = _tracing_kernel()
    order: list[str] = []

    async def slow(_v: object) -> None:
        order.append("slow:enter")
        await asyncio.sleep(0.02)
        order.append("slow:exit")

    async def quick(_v: object) -> None:
        order.append("quick:enter")
        await asyncio.sleep(0.005)
        order.append("quick:exit")

    kernel.root.on("ev", slow, mode="parallel")
    kernel.root.on("ev", quick, mode="parallel")
    await kernel.root.parallel("ev", None)

    # Sequential execution would give slow:enter, slow:exit, quick:enter, ...
    assert order == ["slow:enter", "quick:enter", "quick:exit", "slow:exit"]


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


# --------------------------------------------------------------------------
# §3 fourth dragon — a throwing disposer must not abort the chain.
# `disposer-throws-still-unwinds` covers the dispose()-to-DISPOSED path. The
# same _teardown is shared by the rollback-to-FAILED and back-to-PENDING
# paths, which no fixture reaches, and by child disposal.
# --------------------------------------------------------------------------
def _effect(ctx, log: list[str], name: str, *, throws: bool = False) -> None:
    def setup():
        log.append(f"setup:{name}")

        def dispose() -> None:
            log.append(f"dispose:{name}")
            if throws:
                raise RuntimeError(f"boom:{name}")

        return dispose

    ctx.effect(setup, name=name)


async def test_dispose_reports_every_error_after_the_chain_has_unwound() -> None:
    kernel, trace = _tracing_kernel()
    log: list[str] = []

    def apply(ctx) -> None:
        _effect(ctx, log, "e1", throws=True)
        _effect(ctx, log, "e2")
        _effect(ctx, log, "e3", throws=True)

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))

    with pytest.raises(ExceptionGroup) as caught:
        await fiber.dispose()

    # Every disposer ran, in LIFO order, despite two of them throwing.
    assert log == [
        "setup:e1",
        "setup:e2",
        "setup:e3",
        "dispose:e3",
        "dispose:e2",
        "dispose:e1",
    ]
    # All errors reported, not just the first.
    assert len(caught.value.exceptions) == 2
    assert {str(e) for e in caught.value.exceptions} == {"boom:e3", "boom:e1"}
    # The fiber still reached its target state, and reached it before the
    # error was reported.
    assert fiber.state == FiberState.DISPOSED
    assert trace == ["a:LOADING", "a:ACTIVE", "a:UNLOADING", "a:DISPOSED"]
    assert fiber._effects == []


async def test_failed_apply_with_a_throwing_disposer_still_reaches_failed() -> None:
    """The rollback path: no caller to raise at, so errors stay observable."""
    kernel, _ = _tracing_kernel()
    log: list[str] = []

    def apply(ctx) -> None:
        _effect(ctx, log, "e1")
        _effect(ctx, log, "e2", throws=True)
        raise RuntimeError("apply boom")

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))
    await kernel.settle()

    assert log == ["setup:e1", "setup:e2", "dispose:e2", "dispose:e1"]
    assert fiber.state == FiberState.FAILED
    assert fiber._effects == []
    assert [str(e) for e in fiber.teardown_errors] == ["boom:e2"]
    assert [str(e) for e in kernel.errors] == ["boom:e2"]


async def test_withdrawn_dependency_with_a_throwing_disposer_reaches_pending() -> None:
    """The unload-to-PENDING path, likewise driven by the kernel."""
    kernel, _ = _tracing_kernel()
    log: list[str] = []

    def apply(ctx) -> None:
        _effect(ctx, log, "e1")
        _effect(ctx, log, "e2", throws=True)

    kernel.root.provide("svcX", "impl")
    fiber = await kernel.mount(
        SimplePlugin(name="b", apply_fn=apply, inject=Inject(required=("svcX",)))
    )
    await kernel.settle()

    kernel.root.withdraw("svcX")
    await kernel.settle()

    assert log == ["setup:e1", "setup:e2", "dispose:e2", "dispose:e1"]
    assert fiber.state == FiberState.PENDING
    assert [str(e) for e in fiber.teardown_errors] == ["boom:e2"]


async def test_a_child_disposer_error_does_not_block_the_parents_effects() -> None:
    kernel, _ = _tracing_kernel()
    log: list[str] = []

    def child_apply(ctx) -> None:
        _effect(ctx, log, "child", throws=True)

    async def parent_apply(ctx) -> None:
        _effect(ctx, log, "parent")
        await ctx.plugin(SimplePlugin(name="c", apply_fn=child_apply))

    fiber = await kernel.mount(SimplePlugin(name="p", apply_fn=parent_apply))

    with pytest.raises(ExceptionGroup):
        await fiber.dispose()

    # The child's failure must not strand the parent's own effect.
    assert log == ["setup:parent", "setup:child", "dispose:child", "dispose:parent"]
    assert fiber.state == FiberState.DISPOSED


async def test_a_disposer_error_reports_the_kernel_event_as_it_is_contained() -> None:
    seen: list[object] = []
    kernel = Kernel(observer=seen.append)
    log: list[str] = []

    fiber = await kernel.mount(
        SimplePlugin(name="a", apply_fn=lambda ctx: _effect(ctx, log, "e1", throws=True))
    )
    with pytest.raises(ExceptionGroup):
        await fiber.dispose()

    errors = [e for e in seen if isinstance(e, DisposerErrorEvent)]
    assert len(errors) == 1
    assert errors[0].owner == "a"
    assert errors[0].effect == "e1"
    assert str(errors[0].error) == "boom:e1"


# --------------------------------------------------------------------------
# The cancellation interaction: containment must not swallow a CancelledError,
# and shielded cleanup must still finish when a disposer throws.
# --------------------------------------------------------------------------
async def test_a_disposer_error_does_not_mask_a_cancelled_caller() -> None:
    kernel, _ = _tracing_kernel()
    log: list[str] = []

    def apply(ctx) -> None:
        _effect(ctx, log, "e1", throws=True)

        def slow_setup():
            async def dispose() -> None:
                await asyncio.sleep(0.05)
                log.append("dispose:slow")

            return dispose

        ctx.effect(slow_setup, name="slow")

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))

    first = asyncio.ensure_future(fiber.dispose())
    await asyncio.sleep(0.01)
    first.cancel()
    # The cancelled caller sees CancelledError, never the ExceptionGroup.
    with pytest.raises(asyncio.CancelledError):
        await first

    # Cleanup was not abandoned: the slow disposer settled and the throwing
    # one still ran, and the error is still reported to the next caller.
    with pytest.raises(ExceptionGroup):
        await fiber.dispose()
    assert log == ["setup:e1", "dispose:slow", "dispose:e1"]
    assert fiber.state == FiberState.DISPOSED


async def test_a_cancelled_disposer_is_not_contained_as_a_disposer_error() -> None:
    """CancelledError is a cancellation, not a failed cleanup step."""
    kernel, _ = _tracing_kernel()

    def apply(ctx) -> None:
        def setup():
            def dispose() -> None:
                raise asyncio.CancelledError

            return dispose

        ctx.effect(setup, name="e1")

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))
    with pytest.raises(asyncio.CancelledError):
        await fiber.dispose()
    # Not swallowed into an ExceptionGroup, and not recorded as one.
    assert fiber.teardown_errors == []


# --------------------------------------------------------------------------
# §4 — dispose() is total. `dispose-failed-fiber` covers the FAILED edge; the
# error's survival and the PENDING edge are not visible in a trace.
# --------------------------------------------------------------------------
async def test_disposing_a_failed_fiber_retires_it_and_keeps_the_cause() -> None:
    kernel, trace = _tracing_kernel()

    def apply(ctx) -> None:
        raise RuntimeError("apply boom")

    fiber = await kernel.mount(SimplePlugin(name="a", apply_fn=apply))
    await kernel.settle()
    assert fiber.state == FiberState.FAILED

    await fiber.dispose()
    assert fiber.state == FiberState.DISPOSED
    # The originating error is still available on the retired handle.
    assert isinstance(fiber.error, RuntimeError)
    assert str(fiber.error) == "apply boom"
    # No unwinding: FAILED already held nothing live.
    assert trace == ["a:LOADING", "a:FAILED", "a:DISPOSED"]

    # And disposal stays idempotent from the FAILED edge too.
    await fiber.dispose()
    assert trace.count("a:DISPOSED") == 1


async def test_disposing_a_pending_fiber_retires_it_without_unwinding() -> None:
    kernel, trace = _tracing_kernel()
    fiber = await kernel.mount(
        SimplePlugin(name="b", apply_fn=lambda ctx: None, inject=Inject(required=("never",)))
    )
    assert fiber.state == FiberState.PENDING
    await fiber.dispose()
    assert fiber.state == FiberState.DISPOSED
    assert trace == ["b:PENDING", "b:DISPOSED"]


# --------------------------------------------------------------------------
# §1 — one authority per key per scope.
# --------------------------------------------------------------------------
async def test_a_second_provider_of_a_live_key_is_rejected() -> None:
    kernel, _ = _tracing_kernel()
    kernel.root.provide("svcA", "first")

    with pytest.raises(DuplicateProvider) as caught:
        kernel.root.provide("svcA", "second")

    assert caught.value.key == "svcA"
    # The incumbent is completely undisturbed.
    assert kernel.root.get("svcA") == "first"


async def test_a_rejected_publish_registers_no_effect() -> None:
    """The rejection must not leave a half-registered effect behind."""
    kernel, _ = _tracing_kernel()
    kernel.root.provide("svcA", "first")
    captured: dict[str, Any] = {}

    def apply(ctx) -> None:
        captured["fiber"] = ctx.fiber
        ctx.effect(lambda: lambda: None, name="real")
        ctx.provide("svcA", "second")

    fiber = await kernel.mount(SimplePlugin(name="p2", apply_fn=apply))
    await kernel.settle()

    assert fiber.state == FiberState.FAILED
    assert fiber._effects == []
    assert kernel.root.get("svcA") == "first"


async def test_a_rejected_provider_leaves_the_incumbents_dependents_alone() -> None:
    kernel, trace = _tracing_kernel()

    await kernel.mount(SimplePlugin(name="p1", apply_fn=lambda ctx: ctx.provide("svcA", "p1")))
    consumer = await kernel.mount(
        SimplePlugin(name="c", apply_fn=lambda ctx: None, inject=Inject(required=("svcA",)))
    )
    await kernel.settle()
    assert consumer.state == FiberState.ACTIVE

    await kernel.mount(SimplePlugin(name="p2", apply_fn=lambda ctx: ctx.provide("svcA", "p2")))
    await kernel.settle()

    # The consumer never flickered through UNLOADING or PENDING.
    assert consumer.state == FiberState.ACTIVE
    assert "c:UNLOADING" not in trace
    assert "c:PENDING" not in trace
    assert kernel.root.get("svcA") == "p1"


async def test_sequential_publication_of_the_same_key_is_legal() -> None:
    """Once a provider unloads and the key goes absent, another may claim it."""
    kernel, _ = _tracing_kernel()

    first = await kernel.mount(
        SimplePlugin(name="p1", apply_fn=lambda ctx: ctx.provide("svcA", "p1"))
    )
    await first.dispose()
    assert kernel.root.get("svcA") is None

    second = await kernel.mount(
        SimplePlugin(name="p2", apply_fn=lambda ctx: ctx.provide("svcA", "p2"))
    )
    await kernel.settle()
    assert second.state == FiberState.ACTIVE
    assert kernel.root.get("svcA") == "p2"


async def test_two_providers_of_one_key_in_different_scopes_are_legal() -> None:
    """isolate(key) is the sanctioned way to run a second implementation.

    This is the whole point of the rejection rule — it must not make the
    isolate path collateral damage.
    """
    kernel, _ = _tracing_kernel()
    scope = kernel.root.isolate("svcA", label="s1")

    root_provider = await kernel.mount(
        SimplePlugin(name="p_root", apply_fn=lambda ctx: ctx.provide("svcA", "rootimpl"))
    )
    iso_provider = await kernel.mount(
        SimplePlugin(name="p_iso", apply_fn=lambda ctx: ctx.provide("svcA", "isoimpl")),
        ctx=scope,
    )
    await kernel.settle()

    assert root_provider.state == FiberState.ACTIVE
    assert iso_provider.state == FiberState.ACTIVE
    assert kernel.root.get("svcA") == "rootimpl"
    assert scope.get("svcA") == "isoimpl"

    # Unloading the isolated one must not touch the root's live service.
    await iso_provider.dispose()
    assert scope.get("svcA") is None
    assert kernel.root.get("svcA") == "rootimpl"


async def test_an_unloading_provider_does_not_resurrect_a_dead_predecessor() -> None:
    """The exact shape of the bug the rejection rule replaced.

    Under the old unconditional restore, p1 unloading clobbered p2's live
    service, and p2 unloading resurrected p1's dead one. With one authority
    per key, the sequence simply cannot arise — but assert the end state.
    """
    kernel, _ = _tracing_kernel()

    p1 = await kernel.mount(SimplePlugin(name="p1", apply_fn=lambda ctx: ctx.provide("svcA", "p1")))
    await p1.dispose()
    p2 = await kernel.mount(SimplePlugin(name="p2", apply_fn=lambda ctx: ctx.provide("svcA", "p2")))
    await kernel.settle()
    await p2.dispose()

    # Absent, not "p1" resurrected from the grave.
    assert kernel.root.get("svcA") is None
