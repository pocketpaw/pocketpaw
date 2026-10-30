"""FastAPI must keep flattening include_router()'s routes into ``app.routes``.

opentelemetry-instrumentation-fastapi reads ``route.path`` off the entries in
``app.routes``. FastAPI 0.137 stopped flattening: ``include_router`` now appends
a single ``_IncludedRouter`` that matches lazily and carries no ``.path``. OTel's
``_get_route_details`` reads it unconditionally on a PARTIAL match — path matches,
method does not — which is every CORS preflight. The OTel ASGI middleware runs on
every request, so under fastapi>=0.137 each OPTIONS returns 500 and the browser
reports it as a CORS failure.

That is not hypothetical. Production on 2026-09-06 served 163 preflight 500s on
/chat/groups, /auth/ws/ticket, /agents and /auth/me, and the frontend could not
reach any of them. It reached that version because the deploy installs with
``uv pip install '.[all]'``, which resolves from pyproject and ignores uv.lock —
so the constraint in pyproject, not the lock, is what production obeys.

The real fix is opentelemetry-instrumentation-fastapi>=0.64b0, which handles both
match shapes. It is unreachable while logfire pins opentelemetry-sdk<1.43.0 and
0.64b0 requires >=1.43 (pydantic/logfire#2041).

Two tests, deliberately different in kind. The first measures FastAPI's actual
behaviour, so it reports the day an upgrade reintroduces the shape whatever the
version number says. The second pins the constraint that keeps production off it.

Mutation that must fail these: widen the fastapi bound in pyproject.toml back to
``fastapi>=0.134.0``.
"""

from __future__ import annotations

import pathlib
import re

from fastapi import APIRouter, FastAPI


def test_included_routes_still_expose_a_path():
    """The property OTel depends on, measured rather than assumed."""
    app = FastAPI()
    sub = APIRouter()

    @sub.get("/thing")
    async def _thing() -> dict:
        return {}

    app.include_router(sub, prefix="/sub")

    pathless = [r for r in app.routes if not hasattr(r, "path")]
    assert pathless == [], (
        "app.routes contains entries with no `.path`: "
        f"{[type(r).__name__ for r in pathless]}. "
        "opentelemetry-instrumentation-fastapi reads .path on every request and "
        "will 500 each CORS preflight. Either hold fastapi below the release "
        "that did this, or upgrade opentelemetry-instrumentation-fastapi to "
        ">=0.64b0 once logfire's opentelemetry-sdk<1.43.0 ceiling lifts."
    )


def test_pyproject_holds_fastapi_below_the_release_that_broke_it():
    """The lock does not govern the deploy; this constraint does."""
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text(encoding="utf-8")

    match = re.search(r'^\s*"fastapi(?P<spec>[^"]*)",', text, re.M)
    assert match, "no fastapi requirement found in pyproject.toml"

    spec = match.group("spec")
    assert "<0.137" in spec, (
        f"fastapi is declared as 'fastapi{spec}' with no ceiling below 0.137. "
        "The deploy resolves from pyproject, so removing this bound puts "
        "fastapi>=0.137 into production and 500s every CORS preflight. Lift it "
        "only once opentelemetry-instrumentation-fastapi>=0.64b0 is installable."
    )
