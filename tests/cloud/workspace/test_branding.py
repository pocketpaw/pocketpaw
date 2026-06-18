# tests/cloud/workspace/test_branding.py — WB-1 white-label branding tests.
# Created: 2026-06-14
#
# Covers the per-tenant Branding model on the Workspace:
#   - Branding round-trips through PATCH -> GET (service + DTO).
#   - accent_color format is validated (#RRGGBB only) at the DTO boundary.
#   - logo_asset / favicon_asset referencing an asset owned by ANOTHER
#     workspace is rejected (cross-workspace asset ref) in the service.
#   - A non-admin caller is blocked by the existing workspace.update gate
#     (router-level, same 403 as the rename path) — see test_rbac_routes.py
#     additions; this file proves the data + validation layers.

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from beanie import PydanticObjectId
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.workspace import Branding
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUploadDoc
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.domain import Branding as BrandingDomain
from pocketpaw_ee.cloud.workspace.domain import Workspace as WorkspaceDomain
from pocketpaw_ee.cloud.workspace.dto import (
    BrandingPatch,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
    workspace_to_dto,
)
from pydantic import ValidationError as PydanticValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Helpers (mirror test_service_v2.py)
# ---------------------------------------------------------------------------


async def _seed_user(*, email: str = "u@x.c", full_name: str = "U") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name=full_name,
        workspaces=[],
    )
    await doc.insert()
    return doc


def _ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the resolver so create()/update() don't explode (the real one
    needs init_realtime which unit tests don't run)."""
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest.fixture
async def owner() -> _UserDoc:
    return await _seed_user(email="owner@x.c", full_name="Owner")


async def _seed_asset(*, file_id: str, workspace: str, owner_id: str) -> _FileUploadDoc:
    doc = _FileUploadDoc(
        file_id=file_id,
        storage_key=f"key/{file_id}",
        filename=f"{file_id}.png",
        mime="image/png",
        size=128,
        workspace=workspace,
        owner=owner_id,
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# DTO layer — accent_color validation + WorkspaceOut serialization
# ---------------------------------------------------------------------------


def test_branding_patch_accepts_valid_accent_color() -> None:
    patch = BrandingPatch(display_name="Acme", accent_color="#1A2b3C")
    assert patch.accent_color == "#1A2b3C"


@pytest.mark.parametrize("bad", ["blue", "#ZZZ", "#12345", "1A2B3C", "#1234567", ""])
def test_branding_patch_rejects_malformed_accent_color(bad: str) -> None:
    with pytest.raises(PydanticValidationError):
        BrandingPatch(accent_color=bad)


def test_update_workspace_request_carries_branding() -> None:
    req = UpdateWorkspaceRequest(branding={"display_name": "Acme", "show_paw_mark": False})
    assert req.branding is not None
    assert req.branding.display_name == "Acme"
    assert req.branding.show_paw_mark is False


def test_workspace_out_serializes_branding() -> None:
    ws = WorkspaceDomain(
        id="w1",
        name="Acme",
        slug="acme",
        owner="u1",
        plan="team",
        seats=5,
        created_at=datetime(2026, 6, 14, tzinfo=UTC),
        member_count=1,
        branding=BrandingDomain(display_name="Acme Co", accent_color="#FF0000"),
    )
    dump = workspace_to_dto(ws).model_dump(by_alias=True)
    assert "branding" in dump
    assert dump["branding"]["display_name"] == "Acme Co"
    assert dump["branding"]["accent_color"] == "#FF0000"
    # Unset sub-fields fall through as None — the frontend applies Paw defaults.
    assert dump["branding"]["logo_asset"] is None
    assert dump["branding"]["show_paw_mark"] is True


def test_workspace_out_branding_absent_is_null() -> None:
    ws = WorkspaceDomain(
        id="w1",
        name="Acme",
        slug="acme",
        owner="u1",
        plan="team",
        seats=5,
        created_at=datetime(2026, 6, 14, tzinfo=UTC),
        member_count=1,
    )
    dump = workspace_to_dto(ws).model_dump(by_alias=True)
    assert dump["branding"] is None


# ---------------------------------------------------------------------------
# Service layer — PATCH -> GET round-trip
# ---------------------------------------------------------------------------


async def test_branding_round_trips_through_update_and_get(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )

    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(
            branding=BrandingPatch(
                display_name="Acme Co",
                accent_color="#1A2B3C",
                show_paw_mark=False,
            )
        ),
    )

    read = await workspace_service.get(_ctx(str(owner.id)), ws.id)
    assert read.branding is not None
    assert read.branding.display_name == "Acme Co"
    assert read.branding.accent_color == "#1A2B3C"
    assert read.branding.show_paw_mark is False
    # Untouched sub-fields stay at their defaults.
    assert read.branding.logo_asset is None

    # And it survives serialization onto the wire DTO (the read path the
    # shell consumes at load via GET /workspaces/{id}).
    dump = workspace_to_dto(read).model_dump(by_alias=True)
    assert dump["branding"]["display_name"] == "Acme Co"
    assert dump["branding"]["accent_color"] == "#1A2B3C"
    assert dump["branding"]["show_paw_mark"] is False


async def test_branding_persists_on_the_workspace_document(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(branding=BrandingPatch(display_name="Persisted")),
    )
    doc = await _WorkspaceDoc.get(PydanticObjectId(ws.id))
    assert doc is not None
    assert isinstance(doc.branding, Branding)
    assert doc.branding.display_name == "Persisted"


async def test_branding_patch_does_not_clobber_other_fields(owner) -> None:
    """A branding patch leaves name/settings untouched, and a later name
    patch leaves branding untouched."""
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Original", slug="a")
    )
    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(branding=BrandingPatch(display_name="Brand")),
    )
    # Now rename — branding must survive.
    await workspace_service.update(
        _ctx(str(owner.id)), ws.id, UpdateWorkspaceRequest(name="Renamed")
    )
    read = await workspace_service.get(_ctx(str(owner.id)), ws.id)
    assert read.name == "Renamed"
    assert read.branding is not None
    assert read.branding.display_name == "Brand"


# ---------------------------------------------------------------------------
# Service layer — asset ownership (cross-workspace rejection)
# ---------------------------------------------------------------------------


async def test_branding_accepts_own_workspace_asset(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await _seed_asset(file_id="logo_ok", workspace=ws.id, owner_id=str(owner.id))

    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(branding=BrandingPatch(logo_asset="logo_ok")),
    )
    read = await workspace_service.get(_ctx(str(owner.id)), ws.id)
    assert read.branding is not None
    assert read.branding.logo_asset == "logo_ok"


async def test_branding_rejects_logo_asset_owned_by_another_workspace(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    other_ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="B", slug="b")
    )
    # The asset belongs to other_ws, not ws.
    await _seed_asset(file_id="foreign_logo", workspace=other_ws.id, owner_id=str(owner.id))

    with pytest.raises(Forbidden) as exc:
        await workspace_service.update(
            _ctx(str(owner.id)),
            ws.id,
            UpdateWorkspaceRequest(branding=BrandingPatch(logo_asset="foreign_logo")),
        )
    assert exc.value.code == "workspace.branding_asset_not_owned"


async def test_branding_rejects_favicon_asset_owned_by_another_workspace(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    other_ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="B", slug="b")
    )
    await _seed_asset(file_id="foreign_fav", workspace=other_ws.id, owner_id=str(owner.id))

    with pytest.raises(Forbidden) as exc:
        await workspace_service.update(
            _ctx(str(owner.id)),
            ws.id,
            UpdateWorkspaceRequest(branding=BrandingPatch(favicon_asset="foreign_fav")),
        )
    assert exc.value.code == "workspace.branding_asset_not_owned"


async def test_branding_rejects_nonexistent_asset(owner) -> None:
    """An asset id that doesn't exist anywhere is treated the same as a
    cross-workspace ref — it isn't owned by this workspace."""
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    with pytest.raises(Forbidden) as exc:
        await workspace_service.update(
            _ctx(str(owner.id)),
            ws.id,
            UpdateWorkspaceRequest(branding=BrandingPatch(logo_asset="ghost")),
        )
    assert exc.value.code == "workspace.branding_asset_not_owned"


# ---------------------------------------------------------------------------
# Role gate — branding PATCH rides the SAME workspace.update action as rename
#
# The route guard is ``require_action("workspace.update")``, which wraps
# ``check_workspace_action(user, ws, "workspace.update")``. Exercising that
# function directly proves a non-admin is rejected with the same 403 deny
# code the rename path gives — without depending on the rbac-routes HTTP
# harness (which has a pre-existing dependency-override wiring issue).
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str, role: str, workspace: str = "ws1") -> None:
        self.id = user_id
        self.active_workspace = workspace
        self.workspaces = [_FakeMembership(workspace, role)]


def test_member_denied_branding_update_via_gate() -> None:
    """A MEMBER hitting workspace.update (the gate the branding PATCH uses)
    is rejected with the workspace.insufficient_role deny code — same 403 the
    rename path gives."""
    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

    member = _FakeUser("u-member", role="member")
    with pytest.raises(GuardForbidden) as exc:
        check_workspace_action(member, "ws1", "workspace.update")
    assert exc.value.code == "workspace.insufficient_role"


def test_admin_allowed_branding_update_via_gate() -> None:
    """An ADMIN passes the same workspace.update gate the branding PATCH
    rides — sanity that the gate isn't accidentally over-restrictive."""
    from pocketpaw_ee.guards.deps import check_workspace_action

    admin = _FakeUser("u-admin", role="admin")
    # Returns the resolved role on success; no raise.
    role = check_workspace_action(admin, "ws1", "workspace.update")
    assert role is not None
