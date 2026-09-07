"""Cloud project storage took its tenant from a header, with no auth at all.

``src/pocketpaw/api/v1/cloud_projects.py`` declared its router with no
dependencies — ``grep -c 'Depends('`` over the file returned 0 — and resolved
the tenant with::

    workspace_id = http_request.headers.get("X-Workspace-Id", "default")

Ten routes store and read under ``projects/{workspace_id}/{user_id}/``: create,
list, browse, read, write, create-file, mkdir, rename, delete, and a
server-side ``git clone``. So an anonymous caller could read, write and
enumerate any tenant's project storage by naming the workspace in a header, and
workspace ids are not secret — the frontend sends one on every request.

WHY NOT require_scope

The obvious gate is wrong here, and the wrongness is invisible from inside this
file. The cloud frontend calls these routes as an ORDINARY workspace member,
authenticated by a session cookie. The EE auth bridge grants OSS ``full_access``
only to platform superusers, and a cookie sets neither ``api_key`` nor
``oauth_token`` — so ``require_scope(...)`` would 403 every legitimate cloud
user while an audit ticked the box.

The tenant instead comes from ``request.state.workspace_id``, which the EE auth
bridge stamps from the caller's own JWT. The OSS package cannot import
pocketpaw_ee (an import-linter contract forbids it), so a request-state
attribute is the seam.

WHAT DID NOT CHANGE, DELIBERATELY

The middle ``user_id`` segment stays the constant ``"local"``. It always was
one in practice: cloud callers authenticate by cookie, so the old resolver fell
through to its default on every cloud request. Making it a real user id would
read correctly and orphan every project already stored under
``projects/<workspace>/local/``. The workspace segment is the tenancy boundary;
this one is a vestigial path component.

Mutations that must fail these tests: reading the workspace from a header
again, returning a default instead of raising for an anonymous caller, and
dropping the dependency from any route.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from pocketpaw.api.v1.cloud_projects import resolve_project_scope

MODULE = "pocketpaw.api.v1.cloud_projects"
SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "pocketpaw" / "api" / "v1" / "cloud_projects.py"
)


def _request(headers: dict[str, str] | None = None, **state) -> Request:
    """A bare ASGI request with the given headers and request.state values."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": raw})
    for key, value in state.items():
        setattr(request.state, key, value)
    return request


# ---------------------------------------------------------------------------
# The resolver.
# ---------------------------------------------------------------------------


async def test_an_anonymous_caller_is_refused():
    """No session, no OSS credential: 401, not a default workspace.

    The old code returned ``"default"`` here, which is how every anonymous
    caller landed in the same namespace and could enumerate it.
    """
    with pytest.raises(HTTPException) as exc:
        await resolve_project_scope(_request())
    assert exc.value.status_code == 401


async def test_a_header_cannot_choose_the_tenant():
    """The reproduction, at the boundary itself.

    A caller with no session sends X-Workspace-Id and is refused rather than
    served that workspace.
    """
    with pytest.raises(HTTPException) as exc:
        await resolve_project_scope(_request({"X-Workspace-Id": "ws-victim"}))
    assert exc.value.status_code == 401


async def test_a_header_cannot_override_a_resolved_session():
    """Authenticated as one tenant, naming another: the session wins.

    This is the case a naive fix misses. Adding an auth dependency while
    leaving the header read in place authenticates the caller and then still
    lets them act on any tenant they name.
    """
    request = _request({"X-Workspace-Id": "ws-victim"}, workspace_id="ws-attacker")
    workspace_id, _ = await resolve_project_scope(request)
    assert workspace_id == "ws-attacker"


async def test_the_session_workspace_is_used_when_present():
    workspace_id, user_segment = await resolve_project_scope(_request(workspace_id="ws-1"))
    assert workspace_id == "ws-1"
    assert user_segment == "local"


@pytest.mark.parametrize(
    "state",
    [
        {"full_access": True},
        {"api_key": object()},
        {"oauth_token": object()},
    ],
)
async def test_an_oss_authenticated_caller_gets_the_local_workspace(state):
    """A single-tenant install still works, and keeps its existing key prefix.

    ``"default"`` is the value the old header default produced, so projects
    already on disk keep resolving.
    """
    workspace_id, user_segment = await resolve_project_scope(_request(**state))
    assert (workspace_id, user_segment) == ("default", "local")


# ---------------------------------------------------------------------------
# Every route, not just the ones a test remembered to name.
# ---------------------------------------------------------------------------


def test_every_route_resolves_its_tenant_through_the_dependency():
    module = importlib.import_module(MODULE)
    routes = [r for r in module.router.routes if isinstance(r, APIRoute)]
    assert routes, "expected the cloud projects router to expose HTTP routes"

    missing = [
        f"{sorted(r.methods or {'ANY'})[0]} {r.path}"
        for r in routes
        if not any(d.call is resolve_project_scope for d in r.dependant.dependencies)
    ]
    assert missing == [], (
        "cloud project routes that do not take their tenant from the "
        "authenticated caller:\n  " + "\n  ".join(missing)
    )


def test_the_module_never_reads_a_request_header():
    """Asserted against the parsed module, so the docstrings may still name the
    old header while explaining the boundary. A string search cannot tell an
    explanation from a live read."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    reads = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "headers"
    ]
    assert reads == [], (
        f"the router reads a request header; nothing the caller writes may "
        f"choose the tenant: {reads}"
    )


async def test_an_anonymous_request_to_a_real_route_is_rejected():
    """End to end through the ASGI stack, with no request.state set at all."""
    module = importlib.import_module(MODULE)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get(
            "/api/v1/cloud/projects", headers={"X-Workspace-Id": "ws-victim"}
        )
    assert response.status_code == 401, response.text
