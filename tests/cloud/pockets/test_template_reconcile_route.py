# tests/cloud/pockets/test_template_reconcile_route.py
# Created: 2026-06-13 (feat/pocket-template-reconcile, P2.4) — HTTP-layer
# tests for the thin reconcile REST adapter. Mounts the real pockets_router
# with auth/license overridden (mirroring tests/cloud/conftest.py's
# cloud_app_client recipe but with the pockets router) and asserts the two
# endpoints delegate to the service and return its wire shape. The deep
# behaviour (partition correctness, instance-state preservation) is covered
# by test_template_reconcile.py; this file only pins the wire contract +
# auth gating of the adapter.
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"  # matches the conftest _fixed_workspace override
_USER = "u1"  # matches the conftest _fixed_user override


@pytest.fixture
def installed_templates(tmp_path: Path, monkeypatch) -> Path:
    import pocketpaw.bundled_templates.loader as loader_mod
    from pocketpaw.bundled_templates.installer import install_bundled_templates

    root = tmp_path / "templates"
    install_bundled_templates(destination_root=root)
    monkeypatch.setattr(loader_mod, "_DEFAULT_TEMPLATES_DIR", root)
    return root


@pytest_asyncio.fixture
async def pockets_client() -> AsyncClient:
    """A FastAPI app with the real pockets router mounted and auth/license
    overridden to the fixed test identity (u1 / w1)."""
    from fastapi import FastAPI

    # The create/patch routes use current_user_id / current_workspace_id; the
    # reconcile + read routes use current_optional_user (the user OBJECT) and
    # the require_pocket_edit guard uses current_active_user. Override all
    # three to the fixed identity so the real service access checks run against
    # the real doc.
    from pocketpaw_ee.cloud import auth as auth_mod
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.pockets.router import router as pockets_router
    from pocketpaw_ee.cloud.shared import deps as deps_mod
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    class _U:
        id = _USER
        active_workspace = _WS

    app = FastAPI()
    add_error_handler(app)
    app.include_router(pockets_router, prefix="/api/v1")
    app.dependency_overrides[current_user_id] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: _WS
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[deps_mod.current_active_user] = lambda: _U()
    app.dependency_overrides[auth_mod.current_optional_user] = lambda: _U()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _create_pocket(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/v1/pockets",
        json={"name": "Applications", "templateSlug": "applications-triage"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["_id"]


@pytest.mark.asyncio
async def test_preview_route_returns_diff(
    installed_templates: Path, pockets_client: AsyncClient
) -> None:
    pocket_id = await _create_pocket(pockets_client)
    resp = await pockets_client.post(f"/api/v1/pockets/{pocket_id}/reconcile/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_slug"] == "applications-triage"
    assert set(body["template_owned_regions"]) == {"ui", "actions", "sources", "shape"}
    assert body["preserved_regions"] == ["state"]
    # Fresh pocket already matches the template.
    assert body["has_changes"] is False


@pytest.mark.asyncio
async def test_apply_route_heals_and_returns_ok(
    installed_templates: Path, pockets_client: AsyncClient
) -> None:
    pocket_id = await _create_pocket(pockets_client)
    # Break a template region via the standard PATCH update route (the
    # spec-merge endpoint uses the loopback-bypass auth path, not the
    # current_user_id dep this harness overrides).
    merge = await pockets_client.patch(
        f"/api/v1/pockets/{pocket_id}",
        json={"rippleSpec": {"actions": []}},
    )
    assert merge.status_code == 200, merge.text

    resp = await pockets_client.post(f"/api/v1/pockets/{pocket_id}/reconcile/apply")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["skipped"] is False
    assert "actions" in body["diff"]["changed_regions"]
    # The pocket wire dict came back with the actions restored.
    restored = {a["name"] for a in body["pocket"]["rippleSpec"]["actions"]}
    assert restored == {"approve_application", "reject_application", "flag_for_review"}


@pytest.mark.asyncio
async def test_preview_route_no_template_is_4xx(
    installed_templates: Path, pockets_client: AsyncClient
) -> None:
    resp = await pockets_client.post(
        "/api/v1/pockets",
        json={"name": "No template", "rippleSpec": {"ui": {"type": "card"}}},
    )
    pocket_id = resp.json()["_id"]
    preview = await pockets_client.post(f"/api/v1/pockets/{pocket_id}/reconcile/preview")
    # ValidationError -> 422 in the cloud error taxonomy; envelope is
    # {"error": {"code": ...}}.
    assert preview.status_code == 422, preview.text
    assert preview.json()["error"]["code"] == "reconcile.no_template"
