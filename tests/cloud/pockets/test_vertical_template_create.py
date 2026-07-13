# tests/cloud/pockets/test_vertical_template_create.py
# Created: 2026-06-11 (feat/triage-member-templates) — end-to-end create()
# of a pocket from each generic vertical template (applications-triage,
# member-360) through the EE pockets service with the mongo fixture. This
# pins the full install-time seam: install_bundled_templates -> the loader
# default points at the tmp install -> service.create(template_slug=...) ->
# load_template + _compile_template_to_runtime_dict -> merge into the pocket
# rippleSpec. The seed-shape + compile unit assertions live in
# tests/unit/test_vertical_templates.py; this file proves the service path.
# Modified: 2026-06-11 (fix/template-ui-compile) — regression coverage for
# the empty-canvas bug found on a live deploy: the round-trips now assert
# the template's ui tree and seed state actually land on the created
# pocket (previously only sources/state-binding/actions were asserted,
# which is how the dropped ui slipped through). Added the canvas-ownership
# tests: a caller-supplied ui survives create-with-template, a
# user-modified ui survives a recompile, and a recompile over an empty
# canvas adopts the template ui.
"""Service-level create()/update() round-trips for the bundled vertical templates.

Installs the real bundled templates into a tmp dir, repoints the OSS
loader's default templates root at it (the service calls
``load_template(slug, strict=False)`` with no override), then drives
``pockets_service.create`` / ``update`` and asserts both halves of the
template instantiation land on the pocket:

* the compiled runtime machinery (sources + state binding + actions), and
* the authored canvas (the sibling ripple_spec's ``ui`` tree + seed
  state) — adopted only while the pocket's own canvas is empty, never
  over a user-authored ``ui``.

Uses the shared ``mongo_db`` + autouse RecordingBus fixtures from
tests/cloud/conftest.py, mirroring test_template_needs.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest, UpdatePocketRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_vertical"
_USER = "user_vertical"


@pytest.fixture
def installed_templates(tmp_path: Path, monkeypatch) -> Path:
    """Install the bundled templates into a tmp dir and point the OSS
    loader default at it, so service.create resolves the real templates."""
    import pocketpaw.bundled_templates.loader as loader_mod
    from pocketpaw.bundled_templates.installer import install_bundled_templates

    root = tmp_path / "templates"
    install_bundled_templates(destination_root=root)
    monkeypatch.setattr(loader_mod, "_DEFAULT_TEMPLATES_DIR", root)
    return root


# ---------------------------------------------------------------------------
# create(template_slug=...) — machinery AND canvas both land
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_applications_triage_pocket(installed_templates: Path) -> None:
    """Creating a pocket from applications-triage compiles the template AND
    adopts its canvas: the data_source becomes a runtime ``sources`` entry,
    the gated actions flow through, the ui tree renders non-empty, and the
    seed state the ui binds against is present."""
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Membership intake", template_slug="applications-triage"),
    )

    assert wire["name"] == "Membership intake"
    assert wire["_id"]
    assert wire["templateSlug"] == "applications-triage"

    spec = wire["rippleSpec"]
    assert spec is not None
    # Compile-on-install merged the runtime machinery in.
    assert "applications" in spec["sources"]
    assert spec["state"]["entity_type"] == "Application"
    # The three gated actions rode through the compile passthrough.
    action_names = {a["name"] for a in spec["actions"]}
    assert action_names == {"approve_application", "reject_application", "flag_for_review"}
    assert all(a["instinct_policy"] == "require_approval" for a in spec["actions"])

    # THE EMPTY-CANVAS REGRESSION (live-deploy bug): the template's ui
    # tree must be adopted on a fresh create — previously the merge
    # dropped it and the pocket rendered ui.children: 0.
    ui = spec.get("ui")
    assert isinstance(ui, dict) and ui, "template ui was not adopted"
    assert ui.get("children"), "adopted ui has no children — empty canvas"
    # The seed state the ui binds against rode along (compiled binding
    # keys coexist with the seed keys; compiled wins collisions).
    assert spec["state"]["applications"], "seed applications missing"
    assert spec["state"]["status_counts"], "burndown seed counts missing"
    assert "selected_id" in spec["state"]
    # Authoring-only sibling key never lands on a pocket.
    assert "_placeholder_note" not in spec


@pytest.mark.asyncio
async def test_create_member_360_pocket(installed_templates: Path) -> None:
    """Creating a pocket from member-360 compiles the template AND adopts
    its canvas: runtime source, no actions, a non-empty ui tree, and the
    seed member/profile/lists state."""
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Member lookup", template_slug="member-360"),
    )

    assert wire["name"] == "Member lookup"
    assert wire["_id"]
    assert wire["templateSlug"] == "member-360"

    spec = wire["rippleSpec"]
    assert spec is not None
    assert "member" in spec["sources"]
    assert spec["state"]["entity_type"] == "Member"
    # Read-only: no actions compiled in.
    assert spec.get("actions") == []

    # Empty-canvas regression: ui adopted, seed state present.
    ui = spec.get("ui")
    assert isinstance(ui, dict) and ui, "template ui was not adopted"
    assert ui.get("children"), "adopted ui has no children — empty canvas"
    assert spec["state"]["member"]["name"], "seed member missing"
    for key in ("profile", "membership", "tickets", "orders", "notes"):
        assert spec["state"].get(key), f"seed state missing {key}"
    assert "_placeholder_note" not in spec


# ---------------------------------------------------------------------------
# Canvas ownership — user-authored ui is never clobbered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_with_user_ui_keeps_user_canvas(installed_templates: Path) -> None:
    """A caller-supplied non-empty ui at create is user-owned: the template
    machinery merges in but the template canvas is NOT adopted."""
    user_ui = {"type": "card", "props": {"title": "user-owned"}}
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Custom canvas",
            template_slug="applications-triage",
            ripple_spec={"ui": user_ui},
        ),
    )

    spec = wire["rippleSpec"]
    assert spec["ui"]["type"] == "card"
    assert spec["ui"]["props"]["title"] == "user-owned"
    # Machinery still landed.
    assert "applications" in spec["sources"]
    assert spec["state"]["entity_type"] == "Application"


@pytest.mark.asyncio
async def test_recompile_preserves_user_modified_ui(installed_templates: Path) -> None:
    """A recompile (update with the same template_slug) over a pocket whose
    ui the user has since modified refreshes the machinery only — the
    user's canvas survives untouched."""
    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Edited later", template_slug="applications-triage"),
    )
    pocket_id = created["_id"]

    # Simulate a user rearranging the canvas: replace the spec with their
    # own ui (an update without template_slug is a wholesale spec write).
    user_ui = {"type": "card", "props": {"title": "rearranged-by-user"}}
    await pockets_service.update(pocket_id, _USER, UpdatePocketRequest(ripple_spec={"ui": user_ui}))

    # Recompile against the template (same slug = forced refresh).
    updated = await pockets_service.update(
        pocket_id, _USER, UpdatePocketRequest(template_slug="applications-triage")
    )

    spec = updated["rippleSpec"]
    # The user's canvas survived the recompile.
    assert spec["ui"]["type"] == "card"
    assert spec["ui"]["props"]["title"] == "rearranged-by-user"
    # The machinery was refreshed from the template.
    assert "applications" in spec["sources"]
    assert {a["name"] for a in spec["actions"]} == {
        "approve_application",
        "reject_application",
        "flag_for_review",
    }


@pytest.mark.asyncio
async def test_recompile_adopts_template_ui_when_canvas_empty(installed_templates: Path) -> None:
    """An update that sets a template_slug on a pocket with NO canvas adopts
    the template ui — empty ui means template-owned, on update as well as
    create."""
    created = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="plain"))
    pocket_id = created["_id"]
    assert created["rippleSpec"] is None

    updated = await pockets_service.update(
        pocket_id, _USER, UpdatePocketRequest(template_slug="member-360")
    )

    spec = updated["rippleSpec"]
    ui = spec.get("ui")
    assert isinstance(ui, dict) and ui.get("children"), "template ui not adopted on update"
    assert spec["state"]["member"]["name"]
