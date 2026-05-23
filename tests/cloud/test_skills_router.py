# test_skills_router.py — HTTP + service tests for ee/cloud/skills.
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — smokes the new
# POST /api/v1/skills/api-doc surface: a valid OpenAPI upload installs a
# per-backend API skill, an oversized upload is rejected (422), a
# bad-extension upload is rejected (422), a spec with no `paths` is
# rejected (422), and the auth/RBAC seams (401 unauthenticated, 403
# without skills.manage). The service is exercised directly too.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.skills.router import router as skills_router


def _minimal_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Demo API"},
        "servers": [{"url": "https://demo.example.com"}],
        "paths": {
            "/things": {
                "get": {
                    "tags": ["Things"],
                    "summary": "List things",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _fake_user(user_id: str = "u1", workspace_id: str | None = "w1") -> SimpleNamespace:
    """A User stand-in shaped like ``ee.cloud.models.user.User`` — only the
    attributes the skills-router auth chain reads are filled in."""
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")] if workspace_id else [],
    )


def _build_app(
    *,
    workspace_id: str | None = "w1",
    user_id: str = "u1",
    skip_auth_override: bool = False,
    permission_denier: bool = False,
    monkeypatch=None,
) -> FastAPI:
    """Build a FastAPI app wired to the skills router. Auth and RBAC are
    patched the same way ``test_audit_router.py`` does."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(skills_router)
    app.dependency_overrides[require_license] = lambda: None

    if not skip_auth_override:
        user = _fake_user(user_id=user_id, workspace_id=workspace_id)

        async def _fake_user_dep():
            return user

        app.dependency_overrides[current_active_user] = _fake_user_dep

        if monkeypatch is not None:
            from pocketpaw_ee.cloud._core import deps as core_deps
            from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

            if permission_denier:

                def _deny(*_a, **_k):
                    raise GuardForbidden(
                        code="workspace.insufficient_role",
                        detail="no skills.manage",
                    )

                monkeypatch.setattr(core_deps, "check_workspace_action", _deny)
            else:
                monkeypatch.setattr(core_deps, "check_workspace_action", lambda *a, **k: None)
    return app


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path) -> AsyncClient:
    # Point the skills install dir at a tmp ~/.pocketpaw so the real
    # user dir is never touched.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = _build_app(monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_install_api_doc_installs_skill(client: AsyncClient, tmp_path) -> None:
    """A valid OpenAPI upload installs a per-backend API skill and the
    SKILL.md lands under ~/.pocketpaw/skills/."""
    files = {"file": ("spec.json", json.dumps(_minimal_spec()), "application/json")}
    r = await client.post("/skills/api-doc", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["slug"] == "api-demo-example-com"

    skill_md = tmp_path / ".pocketpaw" / "skills" / "api-demo-example-com" / "SKILL.md"
    assert skill_md.is_file()
    assert "`GET /things`" in skill_md.read_text(encoding="utf-8")


async def test_install_api_doc_accepts_yaml(client: AsyncClient) -> None:
    """A ``.yaml`` upload installs correctly."""
    import yaml

    files = {"file": ("spec.yaml", yaml.safe_dump(_minimal_spec()), "application/x-yaml")}
    r = await client.post("/skills/api-doc", files=files)
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "api-demo-example-com"


async def test_install_api_doc_uses_name_form_field(client: AsyncClient) -> None:
    """The optional ``name`` form field drives the slug when the spec
    names no server."""
    spec = {"openapi": "3.0.0", "paths": {"/x": {"get": {"responses": {}}}}}
    files = {"file": ("spec.json", json.dumps(spec), "application/json")}
    r = await client.post("/skills/api-doc", files=files, data={"name": "named-backend.io"})
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "api-named-backend-io"


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


async def test_install_api_doc_rejects_bad_extension(client: AsyncClient) -> None:
    """An upload that is not .json/.yaml/.yml is rejected."""
    files = {"file": ("spec.txt", json.dumps(_minimal_spec()), "text/plain")}
    r = await client.post("/skills/api-doc", files=files)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.bad_extension"


async def test_install_api_doc_rejects_oversized_file(client: AsyncClient) -> None:
    """An upload larger than 2 MB is rejected."""
    huge = '{"_pad": "' + ("x" * (2 * 1024 * 1024 + 1)) + '"}'
    files = {"file": ("spec.json", huge, "application/json")}
    r = await client.post("/skills/api-doc", files=files)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.too_large"


async def test_install_api_doc_rejects_spec_with_no_paths(client: AsyncClient) -> None:
    """A spec that carries no ``paths`` object is rejected."""
    files = {"file": ("spec.json", json.dumps({"openapi": "3.0.0"}), "application/json")}
    r = await client.post("/skills/api-doc", files=files)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.invalid_spec"


async def test_install_api_doc_rejects_unparseable(client: AsyncClient) -> None:
    """A file that is neither valid JSON nor YAML is rejected."""
    files = {"file": ("spec.json", "{not valid: [json", "application/json")}
    r = await client.post("/skills/api-doc", files=files)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.unparseable"


# ---------------------------------------------------------------------------
# Auth / RBAC seams
# ---------------------------------------------------------------------------


async def test_unauthenticated_returns_401(tmp_path, monkeypatch) -> None:
    """Without a ``current_active_user`` override the auth chain 401s."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = _build_app(skip_auth_override=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        files = {"file": ("spec.json", json.dumps(_minimal_spec()), "application/json")}
        r = await c.post("/skills/api-doc", files=files)
        assert r.status_code == 401


async def test_without_skills_manage_returns_403(tmp_path, monkeypatch) -> None:
    """A caller without the ``skills.manage`` role gets a 403."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = _build_app(permission_denier=True, monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        files = {"file": ("spec.json", json.dumps(_minimal_spec()), "application/json")}
        r = await c.post("/skills/api-doc", files=files)
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Service direct — re-validation + RBAC action is registered
# ---------------------------------------------------------------------------


async def test_service_install_api_doc_direct(tmp_path, monkeypatch) -> None:
    """The service installs a skill when called directly (bus/job path)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from pocketpaw_ee.cloud.skills import service as skills_service
    from pocketpaw_ee.cloud.skills.domain import ApiDocInstall

    body = ApiDocInstall(
        workspace_id="w1",
        user_id="u1",
        filename="spec.json",
        spec_bytes=json.dumps(_minimal_spec()).encode("utf-8"),
    )
    out = await skills_service.install_api_doc("w1", "u1", body)
    assert out.ok is True
    assert out.slug == "api-demo-example-com"


def test_skills_manage_action_is_registered() -> None:
    """``skills.manage`` is in the RBAC action registry — an unknown
    action would make ``require_action_any_workspace`` fail loud."""
    from pocketpaw_ee.guards.actions import ACTIONS

    assert "skills.manage" in ACTIONS


# Silence ruff unused-import nudge for the pytest import (used implicitly).
_ = pytest
