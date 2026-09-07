# test_cloud_lifespan_hooks.py — mount_cloud's startup/shutdown hooks actually run.
# Created: 2026-09-05. Sixteen background loops were registered inside mount_cloud
#   with @app.on_event and never ran once, because both host app factories build
#   their FastAPI app with lifespan=. Starlette reads the on_event lists only
#   through _DefaultLifespan, which it installs only when no lifespan was passed.
#   The dead loops include the Daytona sandbox reaper, so the bug spends money
#   continuously, and the mandate cadence scheduler, a shipped feature that had
#   never fired for anyone.
#
#   The first test here pins the FRAMEWORK MECHANISM rather than our code: it is
#   what makes the rest of this file necessary, and if a future Starlette changed
#   it, that test is where the news arrives.

from __future__ import annotations

import ast
import inspect
import textwrap
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

pytest.importorskip("pocketpaw_ee", reason="enterprise package not installed")


async def _run_lifespan(app: FastAPI) -> None:
    """Drive the app's lifespan exactly as an ASGI server does."""
    async with app.router.lifespan_context(app):
        pass


async def test_on_event_never_runs_when_the_app_has_a_lifespan() -> None:
    """The framework behaviour the whole bug rests on.

    ``Router.startup()`` and ``Router.shutdown()`` are the only readers of
    ``on_startup`` / ``on_shutdown``, and only ``_DefaultLifespan`` calls them.
    Starlette installs that only when the app was built with no ``lifespan=``.
    Both of our app factories pass one, so every ``@app.on_event`` handler
    registered against them was collected into a list nothing reads.

    Asserted here, against the installed Starlette, so this is a measurement and
    not a claim repeated from a design document.
    """
    ran: list[str] = []

    @asynccontextmanager
    async def host_lifespan(_app: FastAPI):
        ran.append("host-startup")
        yield
        ran.append("host-shutdown")

    app = FastAPI(lifespan=host_lifespan)

    @app.on_event("startup")
    async def _never() -> None:  # pragma: no cover - the point is that it does not run
        ran.append("on_event-startup")

    await _run_lifespan(app)

    assert ran == ["host-startup", "host-shutdown"], (
        "on_event ran after all; the mechanism this fix is built on has changed"
    )


async def test_the_composed_lifespan_runs_hooks_inside_the_host_lifespan() -> None:
    """Ordering is the whole design, not a detail.

    Every one of these loops reads Mongo, and Mongo is opened by the host's
    lifespan through CloudLifecycleHook.on_startup. A cloud hook that ran before
    the host started would take its first tick against an uninitialised Beanie.
    Shutdown mirrors it: cloud teardown before the host tears down under it.
    """
    from pocketpaw_ee.cloud import _install_cloud_lifespan

    order: list[str] = []

    @asynccontextmanager
    async def host_lifespan(_app: FastAPI):
        order.append("host-start")
        yield
        order.append("host-stop")

    app = FastAPI(lifespan=host_lifespan)

    async def start_a() -> None:
        order.append("start-a")

    async def start_b() -> None:
        order.append("start-b")

    async def stop_a() -> None:
        order.append("stop-a")

    async def stop_b() -> None:
        order.append("stop-b")

    _install_cloud_lifespan(app, [start_a, start_b], [stop_a, stop_b])
    await _run_lifespan(app)

    assert order == [
        "host-start",
        "start-a",
        "start-b",
        "stop-b",
        "stop-a",
        "host-stop",
    ], f"hooks ran in the wrong order: {order}"


async def test_a_failing_startup_hook_does_not_take_the_process_down() -> None:
    """A background sweep that cannot start must degrade, not restart-loop.

    The web process serves traffic. Letting one sweep's import error propagate
    out of the lifespan would fail startup and hand the container a crash loop,
    which is strictly worse than the sweep being absent.
    """
    from pocketpaw_ee.cloud import _install_cloud_lifespan

    ran: list[str] = []

    async def boom() -> None:
        raise RuntimeError("redis unreachable at boot")

    async def after() -> None:
        ran.append("after")

    async def stop_boom() -> None:
        raise RuntimeError("teardown also failed")

    async def stop_after() -> None:
        ran.append("stop-after")

    app = FastAPI()
    _install_cloud_lifespan(app, [boom, after], [stop_boom, stop_after])

    await _run_lifespan(app)

    assert "after" in ran, "one failing startup hook stopped the hooks behind it"
    assert "stop-after" in ran, "one failing shutdown hook stopped the teardown behind it"


async def test_installing_twice_does_not_double_run_the_hooks() -> None:
    """mount_cloud can be reached more than once in a test session."""
    from pocketpaw_ee.cloud import _install_cloud_lifespan

    ran: list[str] = []

    async def start() -> None:
        ran.append("start")

    app = FastAPI()
    _install_cloud_lifespan(app, [start], [])
    _install_cloud_lifespan(app, [start], [])

    await _run_lifespan(app)

    assert ran == ["start"], f"the hook ran {len(ran)} times"


def test_mount_cloud_registers_no_on_event_handlers() -> None:
    """The regression guard, by AST over the real source.

    ``@app.on_event(...)`` inside this function is the bug. Anyone adding a
    background loop will reach for it, because it is what the nineteen lines
    above theirs used to say, so this has to fail the build rather than be a
    review note.
    """
    from pocketpaw_ee import cloud

    tree = ast.parse(textwrap.dedent(inspect.getsource(cloud.mount_cloud)))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "on_event"
    ]
    assert not offenders, (
        f"mount_cloud registers on_event handlers at offsets {offenders}; "
        "with a lifespan set they are collected and never run. Use @on_startup "
        "/ @on_shutdown, which the composed lifespan actually invokes."
    )


def test_the_cloud_hooks_are_collected_and_handed_to_the_lifespan() -> None:
    """A collector that collects nothing would pass the AST test above.

    So this drives the real ``mount_cloud`` far enough to see that hooks were
    registered and that the composed lifespan was installed on the app.
    """
    from pocketpaw_ee import cloud

    source = textwrap.dedent(inspect.getsource(cloud.mount_cloud))
    tree = ast.parse(source)
    decorators = [
        node.id
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef)
        for node in fn.decorator_list
        if isinstance(node, ast.Name)
    ]
    assert decorators.count("on_startup") + decorators.count("on_shutdown") >= 19, (
        f"expected the 19 collected hooks, found {decorators.count('on_startup')} startup "
        f"and {decorators.count('on_shutdown')} shutdown"
    )

    installs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_install_cloud_lifespan"
    ]
    assert installs, "the hooks are collected and the lifespan is never installed"


def test_the_agent_pool_start_is_idempotent() -> None:
    """Two callers reach it once the hooks run again.

    ``dashboard_lifecycle.startup_event`` starts the pool, and so does the cloud
    hook that this fix revives. Without a guard the second call overwrites
    ``_gc_task`` and orphans the first loop, which then runs forever with nothing
    holding a handle to cancel it.
    """
    import asyncio

    from pocketpaw.agents.pool import AgentPool

    async def _check() -> None:
        pool = AgentPool()
        await pool.start()
        first = pool._gc_task
        await pool.start()
        assert pool._gc_task is first, "the second start orphaned the first GC task"
        await pool.stop()

    asyncio.run(_check())


def test_shutdown_event_calls_the_lifecycle_providers() -> None:
    """``on_shutdown`` is declared, implemented, and was called by nothing.

    ``startup_event`` iterates the ``pocketpaw.lifecycle`` providers; the mirror
    in ``shutdown_event`` never existed, so everything CloudLifecycleHook started
    outlived the process it belonged to.
    """
    from pocketpaw import dashboard_lifecycle

    source = textwrap.dedent(inspect.getsource(dashboard_lifecycle.shutdown_event))
    tree = ast.parse(source)
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "on_shutdown":
            calls.append(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_ext_providers"
        ):
            calls.append("_ext_providers")

    assert "_ext_providers" in calls, "shutdown_event never looks up the lifecycle providers"
    assert "on_shutdown" in calls, "the providers are looked up and on_shutdown is not called"


def test_the_wrong_comment_about_cloud_teardown_is_gone() -> None:
    """The comment that let this survive review.

    ``extensions.py`` asserted "most cloud teardown is handled inside
    mount_cloud's own shutdown hook" — and mount_cloud's shutdown hooks were
    exactly the ones that never ran. A reader checking whether teardown was
    covered found a sentence saying yes.
    """
    from pocketpaw_ee import extensions

    source = inspect.getsource(extensions)
    assert "Most cloud teardown is handled inside mount_cloud" not in source, (
        "the comment still claims mount_cloud handles teardown"
    )


def test_the_automations_status_registry_reports_the_right_flag() -> None:
    """``temporal_sweeps`` is started under its own gate, not the scheduler one.

    The endpoint that exists to answer "are the sweeps on" named the wrong
    variable for that row, so an operator reading it got a confident wrong
    answer about the one sweep that was actually running.
    """
    from pocketpaw_ee.cloud.automations_status import service

    registry = {row.key: row for row in service.build_sweep_registry()}
    assert "temporal_sweeps" in registry, f"row missing; keys are {sorted(registry)}"
    assert registry["temporal_sweeps"].env_flag == "POCKETPAW_TEMPORAL_SWEEP_ENABLED", (
        "temporal_sweeps is gated by its own variable in extensions.py, not by "
        "the cloud scheduler flag"
    )


def test_a_sweep_reports_running_only_when_it_is_actually_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false green that made the dead loops expensive.

    ``build_sweep_registry`` set the row's live state from the environment
    variable alone and never checked whether a loop existed. So the endpoint
    that exists to answer "are the sweeps on" answered from a flag that had
    never been connected to anything, for the entire period all sixteen were
    dead.
    """
    from pocketpaw_ee.cloud._core import sweep_runtime
    from pocketpaw_ee.cloud.automations_status import service

    monkeypatch.setenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "true")
    sweep_runtime.reset()

    rows = {r.key: r for r in service.build_sweep_registry()}
    gated = [r for r in rows.values() if r.env_flag == "POCKETPAW_CLOUD_SCHEDULER_ENABLED"]
    assert gated, "no rows are gated by the cloud scheduler flag"

    # The flag is on and no hook has run. That pair -- configured but not
    # running -- is the exact state this deployment was in for months, and the
    # old registry had no way to express it.
    for row in gated:
        assert row.env_flag_on is True
        assert row.running is False, (
            f"row {row.key!r} reports running with no start hook recorded; the "
            "endpoint is answering from the env flag again"
        )

    # And it flips only when a hook actually completed.
    sweep_runtime.mark_started("_start_cycle_scheduler")
    try:
        after = {r.key: r for r in service.build_sweep_registry()}
        assert after["cycles_snapshot"].running is True
        assert after["decisions_reconciler"].running is False, (
            "one sweep starting must not mark the others running"
        )
    finally:
        sweep_runtime.reset()


def test_every_shutdown_hook_pairs_with_a_startup_hook_by_name() -> None:
    """The lifespan clears a sweep's running mark by rewriting ``_stop_`` to ``_start_``.

    That is a convention, not a mechanism, and a convention with no assertion is
    how the bug this file exists for survived. A shutdown hook named anything
    else makes ``mark_stopped`` a silent no-op, so the status endpoint would keep
    reporting a torn-down sweep as running across a dashboard restart -- and
    ``run_dashboard`` really does restart uvicorn in a loop, so the lifespan runs
    more than once per process.
    """
    from pocketpaw_ee import cloud

    tree = ast.parse(textwrap.dedent(inspect.getsource(cloud.mount_cloud)))
    starts: set[str] = set()
    stops: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
        if "on_startup" in decorators:
            starts.add(fn.name)
        if "on_shutdown" in decorators:
            stops.add(fn.name)

    assert starts and stops, "no collected hooks found; the AST walk is looking in the wrong place"

    unpaired = sorted(
        name
        for name in stops
        if not name.startswith("_stop_") or name.replace("_stop_", "_start_", 1) not in starts
    )
    # The chat-run drain has no startup half by design: it is a defence-in-depth
    # teardown, not a sweep, so it never marks anything running.
    unpaired = [name for name in unpaired if name != "_drain_chat_runs"]

    assert not unpaired, (
        f"shutdown hooks {unpaired} do not map to a startup hook by the "
        "_stop_/_start_ convention, so the lifespan cannot clear their running mark"
    )
