"""A caller may not name a path outside the project directory in their VM.

``_vm_project_abs_path`` used to be a string join. It called ``strip("/")`` on
the caller's path, which removes leading and trailing slashes and leaves ``..``
completely alone, so::

    GET /cloud/projects/p/vm/files/content?path=../../../../root/.ssh/id_rsa

resolved to ``/root/.ssh/id_rsa`` inside the sandbox. The write route was the
same join, so the matching request wrote ``authorized_keys``. The sibling media
router had this right all along (``media/router.py`` rejects any ``..``), which
is what makes this a slip rather than an unknown.

There are TWO ways out of the project directory and a fix for one reads exactly
like a fix for both:

  1. the relative path (``?path=``/``req.path``), joined onto the project root;
  2. ``project_name``, which is a PATH PARAMETER. Starlette's default converter
     matches any run of non-slash characters, and ``..`` qualifies. That one
     moves the root itself, so a containment check written afterwards will
     faithfully confine the caller to a root the caller chose.

Both are asserted here. Tenancy — which workspace's VM a route reaches at all —
is a separate property and lives in test_daytona_router_auth.py.

Mutations that must fail these tests: returning the joined path without the
containment check, comparing with ``os.path`` instead of ``posixpath`` (passes
on Linux, silently passes everything on a Windows host), and dropping the
``project_name`` guard.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id

ROUTER = "pocketpaw_ee.cloud.daytona.router"

ESCAPES = [
    "../../../../root/.ssh/authorized_keys",
    "..",
    "../sibling-project/secret.env",
    "a/../../escape",
    "/../etc/passwd",
    "./../../etc/passwd",
    # A sibling whose NAME BEGINS WITH the project's name. This one is here
    # because a mutation found it and the other five did not: a containment
    # check written as ``joined.startswith(root)``, with no separator on the
    # end, is satisfied by "/workspace/proj-evil/..." when the root is
    # "/workspace/proj". Every other case above resolves to a path that fails a
    # bare prefix test too, so all five would pass against the broken check.
    "../proj-evil/secret.env",
    "../projX",
]

STAYS_INSIDE = [
    ("", "/workspace/proj"),
    (".", "/workspace/proj"),
    ("src/main.py", "/workspace/proj/src/main.py"),
    ("/src/main.py", "/workspace/proj/src/main.py"),
    ("a/b/../c", "/workspace/proj/a/c"),
    ("nested/..", "/workspace/proj"),
]


@pytest.fixture
def module():
    return importlib.import_module(ROUTER)


@pytest.mark.parametrize("relative", ESCAPES)
def test_the_join_refuses_a_path_that_leaves_the_project(module, relative):
    with pytest.raises(HTTPException) as exc:
        module._vm_project_abs_path("/workspace/proj", relative)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(("relative", "expected"), STAYS_INSIDE)
def test_the_join_still_resolves_ordinary_paths(module, relative, expected):
    assert module._vm_project_abs_path("/workspace/proj", relative) == expected


def test_a_project_root_of_slash_does_not_disable_the_check(module):
    """The prefix test must not degrade when the root is ``/``.

    ``root + "/"`` is ``"//"`` in that case, which nothing starts with, so a
    naive prefix comparison would reject every legitimate path. Getting this
    wrong in the other direction is the interesting failure: a check written as
    ``joined.startswith(root)`` with ``root == "/"`` accepts everything.
    """
    assert module._vm_project_abs_path("/", "etc/passwd") == "/etc/passwd"


async def _write(module, monkeypatch, project_name: str, path: str):
    """POST the write route with a stubbed VM, returning (response, uploads)."""
    store = importlib.import_module("pocketpaw_ee.cloud.daytona.store")

    async def _sandbox_id(workspace_id: str) -> str:
        return "sandbox-1"

    async def _config(workspace_id: str) -> dict:
        return {"root_dir": "/workspace"}

    monkeypatch.setattr(store, "get_workspace_vm_sandbox_id", _sandbox_id)
    monkeypatch.setattr(store, "get_workspace_vm_config", _config)
    monkeypatch.setattr(module, "daytona_enabled", lambda: True)

    uploads: list[str] = []

    class _Client:
        async def get_sandbox_by_id(self, sandbox_id: str):
            return SimpleNamespace(state="started", id=sandbox_id)

        async def create_folder(self, sandbox_id: str, path: str) -> None:
            return None

        async def upload_bytes(self, sandbox_id: str, content: bytes, path: str) -> None:
            uploads.append(path)

    monkeypatch.setattr(module, "get_daytona_client", lambda: _Client())

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/cloud/projects/{project_name}/vm/files/write",
            json={"path": path, "content": "pwned"},
        )
    return response, uploads


async def test_a_write_cannot_escape_the_project_directory(module, monkeypatch):
    """End to end, on the route that plants a file the VM will later run."""
    response, uploads = await _write(
        module, monkeypatch, "proj", "../../../../root/.ssh/authorized_keys"
    )
    assert response.status_code == 403, response.text
    assert uploads == [], f"bytes were written outside the project: {uploads}"


async def test_a_percent_encoded_dotdot_project_name_cannot_move_the_root(module, monkeypatch):
    """The second vector: escape before the containment check runs.

    ``project_name=..`` makes the project root ``/`` — after which
    ``etc/cron.d/x`` is 'inside' it and the join is happy to say so.

    Sent PERCENT-ENCODED, and that is the whole point rather than a detail of
    the test client. A literal ``..`` segment is collapsed out of the URL by
    every well-behaved client (httpx included) before the request is sent, so
    a test written the obvious way asserts a 404 from a route that was never
    reached and proves nothing. ``%2e%2e`` survives the client untouched,
    Starlette matches it as one path segment, and the handler is handed the
    decoded ``..``. That is the request an attacker actually sends.
    """
    response, uploads = await _write(module, monkeypatch, "%2e%2e", "etc/cron.d/backdoor")
    assert response.status_code == 403, response.text
    assert uploads == [], f"bytes were written outside the workspace: {uploads}"


async def test_the_project_name_guard_rejects_dotdot_directly(module):
    """The same guard at the unit level, without a client in the way.

    Belt to the route test's braces: if a future client or proxy stops
    normalising, or Starlette's matching changes, this keeps asserting the
    property rather than the plumbing.

    Nothing is stubbed, and it still raises 403 rather than the 404/501/409
    this function reaches for an unprovisioned VM. That is the assertion:
    the name is checked before any of them, so a bare 403 can only have come
    from the guard. Move the check back below the awaits and this test starts
    reporting whichever of those it hits first.
    """
    for bad in ("..", ".", "", "a/b", "a\\b"):
        with pytest.raises(HTTPException) as exc:
            await module._require_workspace_vm("ws-1", bad)
        assert exc.value.status_code == 403, (bad, exc.value.status_code)


async def test_an_ordinary_write_still_lands(module, monkeypatch):
    """The fix must not break the feature it guards."""
    response, uploads = await _write(module, monkeypatch, "proj", "src/main.py")
    assert response.status_code == 200, response.text
    assert uploads == ["/workspace/proj/src/main.py"]
