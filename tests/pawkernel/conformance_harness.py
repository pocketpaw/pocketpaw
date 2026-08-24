# Conformance harness for the paw composition kernel (Python runtime).
# Created: 2026-08-24 (feat/pawkernel-compose) — reads a language-neutral
#   fixture from tests/conformance/paw-compose/, builds the declared plugins
#   against pocketpaw.pawkernel, runs the steps, records the trace, and
#   compares it to expect_trace exactly. Per the harness contract, an unknown
#   fixture field, step op, or listener action is a loud failure, never a skip.

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocketpaw.pawkernel import (
    Context,
    EffectRejected,
    Fiber,
    Inject,
    Kernel,
    KernelEvent,
)
from pocketpaw.pawkernel.events import MODES
from pocketpaw.pawkernel.observer import FiberStateEvent, ServiceEvent

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "conformance" / "paw-compose"

FIXTURE_KEYS = {
    "id",
    "asserts",
    "semantics",
    "plugins",
    "steps",
    "expect_trace",
    "expect_trace_unordered",
    "regression_note",
}
PLUGIN_KEYS = {
    "provides",
    "inject",
    "effects",
    "effects_after_delay",
    "listeners",
    "children",
    "apply_throws",
    "apply_delay_ms",
    "dispose_delay_ms",
    "effect_during_dispose",
    "record_resolved",
}
INJECT_KEYS = {"required", "optional"}
LISTENER_KEYS = {"event", "mode", "id", "action", "wrap", "value", "delay_ms"}
STEP_KEYS = {
    "op",
    "plugin",
    "under",
    "scope",
    "service",
    "value",
    "event",
    "mode",
    "expect_result",
    "expect_state",
    "nowait",
}
OPS = {
    "mount",
    "dispose",
    "dispose_nowait",
    "provide",
    "withdraw",
    "dispatch",
    "settle",
    "isolate",
}
# Listener actions, split by the dispatch modes they are defined for. The
# upstream README maps each action to a mode; using one against the wrong mode
# is a fixture the harness cannot execute, so it fails loudly.
WATERFALL_ACTIONS = {"delegate", "shortcircuit", "wrap"}
PLAIN_ACTIONS = {"observe", "absent", "value"}
ACTIONS = WATERFALL_ACTIONS | PLAIN_ACTIONS


class FixtureError(AssertionError):
    """A fixture the harness cannot execute. Always a failure, never a skip."""


def _reject_unknown(where: str, got: Any, allowed: set[str]) -> None:
    if not isinstance(got, dict):
        raise FixtureError(f"{where}: expected an object, got {type(got).__name__}")
    unknown = set(got) - allowed
    if unknown:
        raise FixtureError(f"{where}: unknown field(s) {sorted(unknown)}")


class ApplyThrew(RuntimeError):
    """Raised by a fixture plugin whose declaration says apply_throws."""


@dataclass
class FixturePlugin:
    """A plugin built from a declarative fixture entry."""

    name: str
    decl: dict[str, Any]
    run: ConformanceRun
    inject: Inject = field(default_factory=Inject)

    def __post_init__(self) -> None:
        _reject_unknown(f"plugin {self.name!r}", self.decl, PLUGIN_KEYS)
        inject_spec = self.decl.get("inject")
        if inject_spec is not None:
            _reject_unknown(f"plugin {self.name!r}.inject", inject_spec, INJECT_KEYS)
        self.inject = Inject.of(inject_spec)
        # Validate listener declarations up front. Raising from inside apply
        # would be caught by the kernel and reported as a FAILED fiber, which
        # reads as a trace mismatch instead of "the harness cannot run this".
        for spec in self.decl.get("listeners") or ():
            _reject_unknown(f"plugin {self.name!r} listener", spec, LISTENER_KEYS)
            action = spec.get("action", "delegate")
            if action not in ACTIONS:
                raise FixtureError(f"plugin {self.name!r}: unknown listener action {action!r}")
            mode = spec.get("mode", "emit")
            if mode not in MODES:
                raise FixtureError(f"plugin {self.name!r}: unknown dispatch mode {mode!r}")
            wants_waterfall = action in WATERFALL_ACTIONS
            if wants_waterfall != (mode == "waterfall"):
                raise FixtureError(
                    f"plugin {self.name!r}: listener action {action!r} is not "
                    f"defined for dispatch mode {mode!r}"
                )
            for required in ("event", "id"):
                if required not in spec:
                    raise FixtureError(f"plugin {self.name!r} listener: missing {required!r}")

    # -- effect bodies ----------------------------------------------------
    def _dispose_delay(self) -> float:
        return float(self.decl.get("dispose_delay_ms") or 0) / 1000.0

    def _make_effect(self, ctx: Context, effect_id: str, is_first: bool) -> None:
        trace = self.run.trace
        name = self.name
        delay = self._dispose_delay()
        during = self.decl.get("effect_during_dispose")

        def setup() -> Any:
            trace.append(f"{name}:effect:{effect_id}:setup")

            async def dispose() -> None:
                if delay:
                    await asyncio.sleep(delay)
                trace.append(f"{name}:effect:{effect_id}:dispose")
                if during and is_first:
                    # DRAGON (§3): registering an effect from inside cleanup
                    # must be refused, not silently leaked past the snapshot.
                    try:
                        ctx.effect(lambda: None, name=during)
                    except EffectRejected:
                        trace.append(f"{name}:effect:{during}:rejected")
                    else:  # pragma: no cover - a conformance failure
                        trace.append(f"{name}:effect:{during}:setup")

            return dispose

        ctx.effect(setup, name=effect_id)

    def _make_listener(self, ctx: Context, spec: dict[str, Any]) -> None:
        # Shape already validated in __post_init__.
        action = spec.get("action", "delegate")
        mode = spec.get("mode", "emit")
        listener_id = spec["id"]
        event = spec["event"]
        trace = self.run.trace
        name = self.name
        wrap = spec.get("wrap")
        short_value = spec.get("value")

        if mode == "waterfall":

            def waterfall_listener(value: Any = None, *, next: Any) -> Any:
                trace.append(f"{name}:listener:{listener_id}:enter")
                if action == "shortcircuit":
                    trace.append(f"{name}:listener:{listener_id}:exit")
                    return short_value
                result = next()
                trace.append(f"{name}:listener:{listener_id}:exit")
                if action == "wrap":
                    return f"{wrap}({result})"
                return result

            ctx.on(event, waterfall_listener, mode=mode)
            return

        # observe / absent / value. A listener with delay_ms must actually
        # await, otherwise `parallel` cannot be shown to fan out.
        delay = float(spec.get("delay_ms") or 0) / 1000.0
        result = short_value if action == "value" else None

        if delay:

            async def plain_listener(value: Any = None) -> Any:
                trace.append(f"{name}:listener:{listener_id}:enter")
                await asyncio.sleep(delay)
                trace.append(f"{name}:listener:{listener_id}:exit")
                return result

        else:

            def plain_listener(value: Any = None) -> Any:  # type: ignore[misc]
                trace.append(f"{name}:listener:{listener_id}:enter")
                trace.append(f"{name}:listener:{listener_id}:exit")
                return result

        ctx.on(event, plain_listener, mode=mode)

    # -- apply ------------------------------------------------------------
    async def apply(self, ctx: Context) -> None:
        """Run the declared body.

        The order is normative (upstream README, "Ordering inside apply"):
        provides -> record_resolved -> effects -> listeners -> children
                 -> apply_delay_ms -> effects_after_delay -> apply_throws
        """
        decl = self.decl
        for service in decl.get("provides") or ():
            ctx.provide(service, f"{self.name}:{service}")

        for key in decl.get("record_resolved") or ():
            self.run.trace.append(f"{self.name}:resolved:{key}:{ctx.get(key)}")

        first = True
        for effect_id in decl.get("effects") or ():
            self._make_effect(ctx, effect_id, is_first=first)
            first = False

        for spec in decl.get("listeners") or ():
            self._make_listener(ctx, spec)

        # Children load inline — awaited inside the parent's apply.
        for child_name in decl.get("children") or ():
            await ctx.plugin(self.run.plugin(child_name))

        delay = float(decl.get("apply_delay_ms") or 0) / 1000.0
        if delay:
            await asyncio.sleep(delay)

        # Effects created after the yield. A runtime that tore down
        # concurrently with apply either never sees these or rejects them.
        for effect_id in decl.get("effects_after_delay") or ():
            self._make_effect(ctx, effect_id, is_first=first)
            first = False

        if decl.get("apply_throws"):
            self.run.trace.append(f"{self.name}:apply:throw")
            raise ApplyThrew(self.name)


class ConformanceRun:
    """One fixture execution: kernel, trace, named contexts, named fibers."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        _reject_unknown(f"fixture {fixture.get('id')!r}", fixture, FIXTURE_KEYS)
        if "expect_trace" not in fixture and "expect_trace_unordered" not in fixture:
            raise FixtureError(f"fixture {fixture.get('id')!r}: no expect_trace declared")
        self.fixture = fixture
        self.trace: list[str] = []
        self.kernel = Kernel(observer=self._observe)
        self.contexts: dict[str, Context] = {"root": self.kernel.root}
        self.fibers: dict[str, Fiber] = {}
        self._plugins: dict[str, FixturePlugin] = {}
        for name, decl in (fixture.get("plugins") or {}).items():
            self._plugins[name] = FixturePlugin(name=name, decl=decl, run=self)

    # -- observability ----------------------------------------------------
    def _observe(self, event: KernelEvent) -> None:
        if isinstance(event, FiberStateEvent):
            self.trace.append(f"{event.fiber}:{event.state}")
        elif isinstance(event, ServiceEvent):
            self.trace.append(f"{event.owner}:{event.kind}:{event.key}")
        else:  # pragma: no cover - defensive
            raise FixtureError(f"unknown kernel event {event!r}")

    def plugin(self, name: str) -> FixturePlugin:
        if name not in self._plugins:
            raise FixtureError(f"fixture declares no plugin {name!r}")
        return self._plugins[name]

    def _context(self, step: dict[str, Any]) -> Context:
        if step.get("under"):
            parent = step["under"]
            if parent not in self.fibers:
                raise FixtureError(f"step refers to unmounted parent {parent!r}")
            return self.fibers[parent].ctx
        scope = step.get("scope", "root")
        if scope not in self.contexts:
            raise FixtureError(f"step refers to unknown scope {scope!r}")
        return self.contexts[scope]

    # -- execution --------------------------------------------------------
    async def execute(self) -> None:
        for index, step in enumerate(self.fixture.get("steps") or ()):
            _reject_unknown(f"step {index}", step, STEP_KEYS)
            op = step.get("op")
            if op is not None:
                if op not in OPS:
                    raise FixtureError(f"step {index}: unknown op {op!r}")
                await self._run_op(index, op, step)
            if "expect_state" in step:
                self._check_state(index, step["expect_state"])
            if op is None and "expect_state" not in step:
                raise FixtureError(f"step {index}: neither an op nor an assertion")

    async def _run_op(self, index: int, op: str, step: dict[str, Any]) -> None:
        if op == "mount":
            name = step["plugin"]
            fiber = await self.kernel.mount(
                self.plugin(name),
                ctx=self._context(step),
                nowait=bool(step.get("nowait")),
            )
            self.fibers[name] = fiber
            # Child plugins mounted during apply must be addressable too.
            self._index_children(fiber)
        elif op == "dispose":
            await self._fiber(step["plugin"]).dispose()
        elif op == "dispose_nowait":
            self.kernel.spawn(self._fiber(step["plugin"]).dispose())
        elif op == "provide":
            value = step.get("value", True)
            self._context(step).provide(step["service"], value)
        elif op == "withdraw":
            self._context(step).withdraw(step["service"])
        elif op == "isolate":
            scope = step["scope"]
            if scope in self.contexts:
                raise FixtureError(f"step {index}: scope {scope!r} already exists")
            self.contexts[scope] = self.kernel.root.isolate(step["service"], label=scope)
        elif op == "dispatch":
            await self._dispatch(index, step)
        elif op == "settle":
            await self.kernel.settle()
        else:  # pragma: no cover - OPS guard above makes this unreachable
            raise FixtureError(f"step {index}: unhandled op {op!r}")

    def _index_children(self, fiber: Fiber) -> None:
        for child in fiber.children:
            self.fibers.setdefault(child.name, child)
            self._index_children(child)

    def _fiber(self, name: str) -> Fiber:
        if name not in self.fibers:
            raise FixtureError(f"step refers to unmounted plugin {name!r}")
        return self.fibers[name]

    async def _dispatch(self, index: int, step: dict[str, Any]) -> None:
        event = step["event"]
        mode = step.get("mode", "emit")
        args = () if "value" not in step else (step["value"],)
        bus = self.kernel.bus
        if mode == "waterfall":
            result: Any = bus.waterfall(event, *args)
        elif mode == "emit":
            bus.emit(event, *args)
            result = None
        elif mode == "parallel":
            await bus.parallel(event, *args)
            result = None
        elif mode == "serial":
            result = await bus.serial(event, *args)
        else:
            raise FixtureError(f"step {index}: unknown dispatch mode {mode!r}")
        if "expect_result" in step:
            expected = step["expect_result"]
            assert result == expected, (
                f"step {index}: dispatch {event!r} returned {result!r}, expected {expected!r}"
            )

    def _check_state(self, index: int, expected: dict[str, str]) -> None:
        for name, want in expected.items():
            # Fibers created as children during apply are indexed on mount.
            for fiber in list(self.fibers.values()):
                self._index_children(fiber)
            got = self._fiber(name).state
            assert got == want, f"step {index}: plugin {name!r} is {got}, expected {want}"

    # -- verdict ----------------------------------------------------------
    def check_trace(self) -> None:
        if "expect_trace_unordered" in self.fixture:
            expected = sorted(self.fixture["expect_trace_unordered"])
            assert sorted(self.trace) == expected, (
                f"trace multiset mismatch\n  got:      {sorted(self.trace)}\n  expected: {expected}"
            )
            return
        expected = self.fixture["expect_trace"]
        assert self.trace == expected, _trace_diff(self.trace, expected)


def _trace_diff(got: list[str], expected: list[str]) -> str:
    lines = ["trace mismatch (exact order required)", "  idx  got                       expected"]
    for i in range(max(len(got), len(expected))):
        g = got[i] if i < len(got) else "-"
        e = expected[i] if i < len(expected) else "-"
        mark = " " if g == e else "*"
        lines.append(f"{mark} {i:>3}  {g:<24}  {e}")
    return "\n".join(lines)


def load_fixtures() -> list[dict[str, Any]]:
    """Load every fixture. A missing directory is a failure, never a skip."""
    if not FIXTURE_DIR.is_dir():
        raise FixtureError(f"conformance fixtures missing at {FIXTURE_DIR}")
    paths = sorted(FIXTURE_DIR.glob("*.json"))
    if not paths:
        raise FixtureError(f"no fixtures found in {FIXTURE_DIR}")
    fixtures = []
    for path in paths:
        data = json.loads(path.read_text())
        data.setdefault("id", path.stem)
        fixtures.append(data)
    return fixtures


async def run_fixture(fixture: dict[str, Any]) -> ConformanceRun:
    run = ConformanceRun(fixture)
    await run.execute()
    await run.kernel.settle()
    run.check_trace()
    return run
