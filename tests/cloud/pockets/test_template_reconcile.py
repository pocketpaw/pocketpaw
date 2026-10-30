# tests/cloud/pockets/test_template_reconcile.py
# Created: 2026-06-13 (feat/pocket-template-reconcile, P2.4 Template Reconcile)
# — service-level tests for the new reconcile primitive. Drives the public
# service API (preview_reconcile / apply_reconcile) against the mongo +
# RecordingBus fixtures, mirroring test_vertical_template_create.py.
#
# Coverage (the required matrix from the build spec):
#   * template-owned regions (ui / actions / sources / shape) get REFRESHED
#     from the source template on apply,
#   * instance-owned regions (state rows, selected_id, pending_proposal)
#     SURVIVE apply untouched,
#   * a hand-edited state row survives reconcile,
#   * preview is a dry-run — it writes NOTHING and the persisted spec is
#     byte-for-byte unchanged after a preview,
#   * a pocket with no template_slug errors cleanly (ValidationError),
#   * the diff payload distinguishes changed template regions from
#     preserved instance regions.
"""Service-level reconcile round-trips for the bundled vertical templates.

Installs the real bundled templates into a tmp dir, repoints the OSS
loader default at it, creates a pocket from a template, mutates its
instance-owned state + a template-owned region the way a live re-deploy
graft would, then reconciles and asserts the partition holds:
template owns ui/actions/sources/shape, the instance owns state.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.pockets import reconcile as reconcile_svc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest, UpdatePocketRequest
from pocketpaw_ee.cloud.shared.errors import NotFound, ValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_reconcile"
_USER = "user_reconcile"
_OTHER = "user_other"


@pytest.fixture
def installed_templates(tmp_path: Path, monkeypatch) -> Path:
    """Install the bundled templates into a tmp dir and point the OSS
    loader default at it, so the reconcile service resolves the real
    on-disk template (same fixture shape as test_vertical_template_create)."""
    import pocketpaw.bundled_templates.loader as loader_mod
    from pocketpaw.bundled_templates.installer import install_bundled_templates

    root = tmp_path / "templates"
    install_bundled_templates(destination_root=root)
    monkeypatch.setattr(loader_mod, "_DEFAULT_TEMPLATES_DIR", root)
    return root


async def _make_pocket(name: str = "Applications") -> str:
    """Create a pocket from applications-triage and return its id."""
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name=name, template_slug="applications-triage"),
    )
    return wire["_id"]


# ---------------------------------------------------------------------------
# apply — template-owned refreshed, instance-owned survives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_refreshes_template_regions(installed_templates: Path) -> None:
    """Apply re-applies the template's ui/actions/sources/shape after they
    were clobbered on the instance — the live-deploy graft scenario."""
    pocket_id = await _make_pocket()

    # Simulate a hand-graft that mangled template-owned regions: a re-run
    # script (or a careless edit) replaced the canvas with a stub and
    # dropped the actions. This is exactly what reconcile must heal.
    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(
            ripple_spec={
                "ui": {"type": "card", "props": {"title": "broken stub"}},
                "actions": [],
            }
        ),
    )
    broken = await pockets_service.get(pocket_id, _USER)
    assert broken["rippleSpec"]["ui"]["type"] == "card"
    assert broken["rippleSpec"]["actions"] == []

    result = await reconcile_svc.apply_reconcile(pocket_id, _WS, _USER)
    assert result["ok"] is True

    healed = await pockets_service.get(pocket_id, _USER)
    spec = healed["rippleSpec"]
    # ui restored to the template canvas (not the stub).
    assert spec["ui"]["type"] != "card"
    assert spec["ui"].get("children"), "template ui not restored"
    # actions restored from the template.
    action_names = {a["name"] for a in spec["actions"]}
    assert action_names == {
        "approve_application",
        "reject_application",
        "flag_for_review",
    }
    # sources + shape are template-owned too. (applications-triage compiles
    # to shape="custom" — the assertion pins the region is refreshed from the
    # template, whatever the template declares.)
    assert "applications" in spec["sources"]
    assert spec.get("shape") == "custom"


@pytest.mark.asyncio
async def test_apply_preserves_instance_state(installed_templates: Path) -> None:
    """Instance-owned state (rows, selected_id, pending_proposal) survives a
    reconcile that refreshes the template machinery."""
    pocket_id = await _make_pocket()

    # Mutate instance-owned state the way live use does: an operator
    # selected a row, a proposal is pending, and the row set changed.
    spec = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]
    new_state = dict(spec["state"])
    new_state["selected_id"] = "app_42"
    new_state["pending_proposal"] = {"action": "approve_application", "row_id": "app_42"}
    new_state["applications"] = [
        {"id": "app_42", "name": "Hand Edited Applicant", "status": "pending"}
    ]
    await pockets_service.update(
        pocket_id, _USER, UpdatePocketRequest(ripple_spec={"state": new_state})
    )

    await reconcile_svc.apply_reconcile(pocket_id, _WS, _USER)

    healed_state = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]["state"]
    # Every instance-owned field survived untouched.
    assert healed_state["selected_id"] == "app_42"
    assert healed_state["pending_proposal"] == {
        "action": "approve_application",
        "row_id": "app_42",
    }
    assert healed_state["applications"] == [
        {"id": "app_42", "name": "Hand Edited Applicant", "status": "pending"}
    ]


@pytest.mark.asyncio
async def test_apply_preserves_hand_edited_state_row(installed_templates: Path) -> None:
    """A single hand-edited row inside state survives reconcile verbatim —
    the regression that motivated this primitive (a wiped detail panel)."""
    pocket_id = await _make_pocket()

    spec = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]
    state = dict(spec["state"])
    # A bespoke row an operator typed in by hand, with a nested object the
    # template never declared.
    state["applications"] = [
        {
            "id": "hand_1",
            "name": "Bespoke Row",
            "status": "interview",
            "_notes": {"by": "operator", "detail": "do not wipe"},
        }
    ]
    await pockets_service.update(
        pocket_id, _USER, UpdatePocketRequest(ripple_spec={"state": state})
    )

    await reconcile_svc.apply_reconcile(pocket_id, _WS, _USER)

    rows = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]["state"]["applications"]
    assert rows == [
        {
            "id": "hand_1",
            "name": "Bespoke Row",
            "status": "interview",
            "_notes": {"by": "operator", "detail": "do not wipe"},
        }
    ]


@pytest.mark.asyncio
async def test_apply_preserves_pocket_identity(installed_templates: Path) -> None:
    """Reconcile never touches pocket name/owner/visibility — those are
    instance metadata, not template-owned."""
    pocket_id = await _make_pocket(name="My Renamed Pocket")
    # Rename + make it workspace-visible after install.
    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(name="My Renamed Pocket", visibility="workspace"),
    )

    await reconcile_svc.apply_reconcile(pocket_id, _WS, _USER)

    healed = await pockets_service.get(pocket_id, _USER)
    assert healed["name"] == "My Renamed Pocket"
    assert healed["visibility"] == "workspace"
    assert healed["owner"] == _USER


# ---------------------------------------------------------------------------
# preview — dry run, writes nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_writes_nothing(installed_templates: Path) -> None:
    """preview_reconcile is a pure dry-run: the persisted spec is byte-for-byte
    identical before and after the call."""
    pocket_id = await _make_pocket()
    # Break a template region so preview has something to report.
    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(ripple_spec={"ui": {"type": "card", "props": {"title": "stub"}}}),
    )

    before = copy.deepcopy((await pockets_service.get(pocket_id, _USER))["rippleSpec"])
    diff = await reconcile_svc.preview_reconcile(pocket_id, _WS, _USER)
    after = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]

    assert after == before, "preview must not mutate the persisted spec"
    # The preview still reports what WOULD change.
    assert diff["template_slug"] == "applications-triage"
    assert "ui" in diff["changed_regions"]


@pytest.mark.asyncio
async def test_preview_reports_changed_and_preserved_regions(installed_templates: Path) -> None:
    """The diff partitions template-owned regions (reportable as changed) from
    instance-owned regions (always reported as preserved)."""
    pocket_id = await _make_pocket()
    diff = await reconcile_svc.preview_reconcile(pocket_id, _WS, _USER)

    # The template-owned regions are the diff's domain.
    assert set(diff["template_owned_regions"]) == {"ui", "actions", "sources", "shape"}
    # state is always reported as preserved, never changed.
    assert "state" in diff["preserved_regions"]
    assert "state" not in diff["changed_regions"]
    # A freshly-installed pocket already matches its template, so nothing
    # template-owned changes (the canvas/actions were adopted at install).
    assert diff["changed_regions"] == []
    assert diff["has_changes"] is False


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_no_template_slug_errors(installed_templates: Path) -> None:
    """A pocket created WITHOUT a template_slug cannot be reconciled — both
    preview and apply raise a clean ValidationError, not an AttributeError."""
    wire = await pockets_service.create(
        _WS, _USER, CreatePocketRequest(name="No template", ripple_spec={"ui": {"type": "card"}})
    )
    pocket_id = wire["_id"]

    with pytest.raises(ValidationError):
        await reconcile_svc.preview_reconcile(pocket_id, _WS, _USER)
    with pytest.raises(ValidationError):
        await reconcile_svc.apply_reconcile(pocket_id, _WS, _USER)


@pytest.mark.asyncio
async def test_reconcile_unknown_pocket_errors(installed_templates: Path) -> None:
    """An unknown pocket id raises NotFound, scoped to the workspace."""
    with pytest.raises(NotFound):
        await reconcile_svc.preview_reconcile("000000000000000000000000", _WS, _USER)


@pytest.mark.asyncio
async def test_reconcile_cross_tenant_errors(installed_templates: Path) -> None:
    """A pocket in another workspace is invisible — NotFound, never a
    cross-tenant reconcile."""
    pocket_id = await _make_pocket()
    with pytest.raises(NotFound):
        await reconcile_svc.preview_reconcile(pocket_id, "ws_someone_else", _USER)


@pytest.mark.asyncio
async def test_apply_requires_edit_access(installed_templates: Path) -> None:
    """A caller with no edit access to a PRIVATE pocket cannot apply a
    reconcile (Forbidden), even though the pocket exists. The access gate
    must fire BEFORE the idempotent-skip path — a fresh pocket already
    matches its template, so without the explicit gate a non-editor could
    probe sync-state on a pocket they can't touch."""
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    # Private so a non-owner, non-shared workspace member is NOT an editor.
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Private", template_slug="applications-triage", visibility="private"
        ),
    )
    pocket_id = wire["_id"]
    with pytest.raises(Forbidden):
        await reconcile_svc.apply_reconcile(pocket_id, _WS, _OTHER)
    # Preview is gated too (read access).
    with pytest.raises(Forbidden):
        await reconcile_svc.preview_reconcile(pocket_id, _WS, _OTHER)


@pytest.mark.asyncio
async def test_access_checked_before_template_resolution(installed_templates: Path) -> None:
    """A non-member previewing a PRIVATE pocket that has NO template gets
    Forbidden — access is gated BEFORE template resolution, so the absence of
    a template (or a stale one) never leaks to someone who can't see the
    pocket."""
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Private no-template",
            ripple_spec={"ui": {"type": "card"}},
            visibility="private",
        ),
    )
    pocket_id = wire["_id"]
    # _OTHER is not owner / shared / (the pocket is private) — must be Forbidden,
    # NOT the ValidationError a member would get for the missing template.
    with pytest.raises(Forbidden):
        await reconcile_svc.preview_reconcile(pocket_id, _WS, _OTHER)


@pytest.mark.asyncio
async def test_apply_stale_slug_errors(installed_templates: Path) -> None:
    """A pocket whose template_slug no longer resolves on disk errors cleanly
    rather than silently no-op'ing — the operator must know the template is
    gone before trusting a reconcile."""
    pocket_id = await _make_pocket()
    # Point the pocket at a slug that does not exist in the install.
    doc_wire = await pockets_service.get(pocket_id, _USER)
    assert doc_wire["templateSlug"] == "applications-triage"
    # Force a stale slug directly on the doc (update() would try to recompile
    # and is itself tolerant; we want the reconcile-time failure surfaced).
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    doc.template_slug = "does-not-exist-anywhere"
    await doc.save()

    with pytest.raises(ValidationError):
        await reconcile_svc.preview_reconcile(pocket_id, _WS, _USER)
