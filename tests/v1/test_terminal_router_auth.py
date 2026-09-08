"""The terminal router spawns a shell, and shipped with no auth at all.

``src/pocketpaw/api/v1/terminal.py`` declared ``APIRouter(tags=["Terminal"])``
with no dependencies, no route-level guard, and not one ``Depends(`` in the
file. It is mounted unconditionally under ``/api/v1/``, where the global
AuthMiddleware runs its token cascade but deliberately SKIPS the 401 on the
stated assumption that every router below it authenticates for itself
(``auth_optional_prefixes`` in dashboard_auth.py). This one did not.

``POST /api/v1/terminal/input`` writes to the stdin of a module-level bash
process started with ``cwd=~`` and the server's environment, and
``GET /terminal/sse`` streams the output back. One unauthenticated request read
the API-key store and every provider key in the process env.

TWO GUARDS, TESTED SEPARATELY, AND THAT SEPARATION IS THE POINT.

The router now carries ``require_terminal_enabled`` then
``require_scope("admin")``. Belt and braces defeats a test that names only one
of them: with the terminal disabled, an anonymous caller gets 404 whether or not
the scope check exists, so "anonymous is rejected" passes against a router with
the auth dependency deleted. Every test below that means to exercise the scope
check therefore ENABLES the terminal first.

WHY THE GATE LIVES IN ITS OWN MODULE

terminal.py imports ``fcntl``, ``pty`` and ``termios``, so it cannot be imported
on Windows at all. A gate defined inside it would only ever be exercised on
Linux CI, which is the wrong place for the one check standing between the public
internet and a login shell. It lives in ``terminal_gate.py`` instead, which
imports anywhere, and the ``terminal_app`` fixture stubs the three POSIX modules
when they are missing so the live route assertions run on every platform too.

Mutations that must fail these tests: removing either dependency from the router
declaration, flipping the ``terminal_enabled`` default to True, dropping
``terminal_enabled`` from ``_IMMUTABLE_FIELDS``, and swapping the 404 for a 403.

One more thing a reader needs: the root conftest enables the ``require_scope``
testing bypass for every test unless a test carries the ``enforce_scope``
marker. A scope assertion written without it passes against a router with no
scope dependency at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.api.v1.terminal_gate import require_terminal_enabled, terminal_enabled

SRC = Path(__file__).resolve().parents[2] / "src" / "pocketpaw"
TERMINAL_PY = SRC / "api" / "v1" / "terminal.py"


# ---------------------------------------------------------------------------
# The gate, on every platform.
# ---------------------------------------------------------------------------


def test_the_terminal_is_off_unless_a_deployment_asks_for_it(monkeypatch):
    """The default is the finding. Nothing in the product calls these routes.

    paw-enterprise's /code surface uses the Daytona VM's own web terminal, the
    OSS dashboard and the Tauri client never reference them, and no test did
    either — so default-off costs no feature.
    """
    from pocketpaw.config import Settings, get_settings

    get_settings.cache_clear()
    assert Settings().terminal_enabled is False
    assert terminal_enabled() is False


async def test_the_gate_404s_rather_than_403s_when_disabled():
    """A deployment that does not want a shell should not advertise one."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await require_terminal_enabled()
    assert exc.value.status_code == 404


def test_the_settings_api_cannot_turn_the_terminal_on():
    """Otherwise an admin-scoped API key re-enables a host shell over HTTP.

    That is the same hole the router guard closes, one level up: the settings
    write route is reachable with ``admin`` scope, and its update loop is a
    blind setattr over whatever keys the body carries, bounded only by
    ``hasattr`` and this frozenset.
    """
    from pocketpaw.api.v1.settings import _IMMUTABLE_FIELDS

    assert "terminal_enabled" in _IMMUTABLE_FIELDS


# ---------------------------------------------------------------------------
# The wiring, without importing the POSIX-only module.
# ---------------------------------------------------------------------------


def test_the_router_declares_both_dependencies_in_order():
    """Parsed, not imported, so this still runs on a Windows checkout.

    Order is asserted because it is behavioural, not cosmetic: the enabled
    check must come first so a disabled deployment answers 404 to an
    authenticated caller too, instead of leaking that the surface exists.
    """
    tree = ast.parse(open(TERMINAL_PY, encoding="utf-8").read())

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "APIRouter"
    ]
    assert len(calls) == 1, "expected exactly one APIRouter() in terminal.py"

    deps = [kw for kw in calls[0].keywords if kw.arg == "dependencies"]
    assert deps, (
        "the terminal router declares no dependencies. These routes write to "
        "the stdin of a bash process running as the server user."
    )

    names: list[str] = []
    for element in deps[0].value.elts:
        # Depends(require_terminal_enabled) or Depends(require_scope("admin"))
        inner = element.args[0]
        if isinstance(inner, ast.Name):
            names.append(inner.id)
        elif isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
            names.append(inner.func.id)

    assert names == ["require_terminal_enabled", "require_scope"], names


def test_the_module_still_has_no_per_route_dependency_to_hide_behind():
    """The guard is router-wide, so a new route cannot be added unguarded.

    Adding ``@router.post("/terminal/exec")`` inherits both dependencies. This
    asserts the property that makes that true, rather than enumerating routes
    that would go stale.
    """
    tree = ast.parse(open(TERMINAL_PY, encoding="utf-8").read())
    decorated = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for deco in node.decorator_list
        if isinstance(deco, ast.Call)
        and isinstance(deco.func, ast.Attribute)
        and isinstance(deco.func.value, ast.Name)
        and deco.func.value.id == "router"
    ]
    assert decorated, "expected the terminal router to still declare routes"


# ---------------------------------------------------------------------------
# Live behaviour, on every platform.
# ---------------------------------------------------------------------------


@pytest.fixture
def terminal_app(monkeypatch):
    """Mount the real router, stubbing the POSIX modules where they are absent.

    terminal.py imports fcntl, pty and termios at module scope, so on Windows
    it cannot be imported and these assertions would skip — leaving the one
    check that stands between the internet and a login shell verified only on
    CI. The stubs are inert, and that is safe HERE specifically because both
    dependencies run BEFORE the handler: a passing test never touches them. If
    a guard were removed the handler would reach the fake pty and raise, so the
    test still fails, just with a 500 instead of a 403.
    """
    import importlib
    import sys
    import types

    for name in ("fcntl", "termios", "pty"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.ioctl = lambda *a, **k: None
            stub.TIOCSWINSZ = 0
            stub.openpty = lambda: (0, 1)
            monkeypatch.setitem(sys.modules, name, stub)

    module = importlib.import_module("pocketpaw.api.v1.terminal")
    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1")
    return app


@pytest.mark.enforce_scope
async def test_an_anonymous_caller_cannot_reach_the_shell_when_enabled(terminal_app, monkeypatch):
    """The reproduction, with the terminal ENABLED so the 404 gate is out of
    the way and the scope check is the only thing left standing.

    Delete ``require_scope("admin")`` from the router and this test fails; the
    disabled-by-default tests above would not.

    The ``enforce_scope`` marker is load-bearing and not boilerplate. The root
    conftest turns ``pocketpaw.api.deps._TESTING_FULL_ACCESS`` ON for every test
    by default, so ``require_scope`` returns immediately and any test written
    without this marker reaches the HANDLER. Written the obvious way, this test
    asserted a 403 that the guard never produced — it was the stubbed pty
    raising from inside the handler that gave it away.
    """
    from pocketpaw.config import get_settings

    monkeypatch.setenv("POCKETPAW_TERMINAL_ENABLED", "true")
    get_settings.cache_clear()
    try:
        transport = ASGITransport(app=terminal_app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.post(
                "/api/v1/terminal/input",
                json={"data": "cat ~/.pocketpaw/api_keys.json; env\n"},
            )
        assert response.status_code == 403, response.text
    finally:
        get_settings.cache_clear()


async def test_the_routes_are_absent_by_default(terminal_app, monkeypatch):
    from pocketpaw.config import get_settings

    monkeypatch.delenv("POCKETPAW_TERMINAL_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        transport = ASGITransport(app=terminal_app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.post("/api/v1/terminal/input", json={"data": "id\n"})
        assert response.status_code == 404, response.text
    finally:
        get_settings.cache_clear()
