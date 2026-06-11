# tests/cloud/pockets/test_vertical_template_create.py
# Created: 2026-06-11 (feat/triage-member-templates) — end-to-end create()
# of a pocket from each generic vertical template (applications-triage,
# member-360) through the EE pockets service with the mongo fixture. This
# pins the full install-time seam: install_bundled_templates -> the loader
# default points at the tmp install -> service.create(template_slug=...) ->
# load_template + _compile_template_to_runtime_dict -> merge into the pocket
# rippleSpec. The seed-shape + compile unit assertions live in
# tests/unit/test_vertical_templates.py; this file proves the service path.
"""Service-level create() round-trip for the bundled vertical templates.

Installs the real bundled templates into a tmp dir, repoints the OSS
loader's default templates root at it (the service calls
``load_template(slug, strict=False)`` with no override), then drives
``pockets_service.create`` with each vertical template_slug and asserts
the compiled runtime dict (sources + state + actions) merged into the
created pocket's rippleSpec. Uses the shared ``mongo_db`` + autouse
RecordingBus fixtures from tests/cloud/conftest.py, mirroring
test_template_needs.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

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


@pytest.mark.asyncio
async def test_create_applications_triage_pocket(installed_templates: Path) -> None:
    """Creating a pocket from applications-triage compiles the template:
    the data_source becomes a runtime ``sources`` entry, the gated actions
    flow through, and the pocket is created (never blocked)."""
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


@pytest.mark.asyncio
async def test_create_member_360_pocket(installed_templates: Path) -> None:
    """Creating a pocket from member-360 compiles the template: the
    data_source becomes a runtime ``sources`` entry, the view stays
    action-free, and the pocket is created."""
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
