# tests/ee/sites/test_deployed_at_and_revert.py
# Created: 2026-06-19 (P2b-backend — "Last Deployed" + revert endpoint).
# Reproduce-first cover for the two P2b-backend deliverables:
#   1. ``deployed_at`` — the Site model now carries a ``deployed_at`` (UTC) stamped
#      ONLY on a successful non-preview deploy (when ``deployed`` flips True), and
#      both SiteResponse and SiteStatusResponse expose it as an ISO string|None.
#      Asserted: publish stamps it; a PREVIEW build does NOT; pocket_status reads
#      None before any deploy and the ISO string after.
#   2. revert — ``revert_pocket_version`` resolves a pocket's version_no → the
#      durable ArtifactVersion row (tenant-scoped, main branch) and writes a NEW
#      forward-moving DRAFT snapshot of that version's content. Asserted: a revert
#      creates a draft whose content == the target version; an unknown version_no
#      raises ValueError (the router maps it to 404).
from __future__ import annotations

import pytest
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions

pytestmark = pytest.mark.asyncio


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


# ---------------------------------------------------------------------------
# P2b — deployed_at stamping + DTO exposure
# ---------------------------------------------------------------------------


async def test_publish_stamps_deployed_at(beanie_test_db):
    """A successful live publish stamps the Site doc's ``deployed_at`` (UTC) when
    ``deployed`` flips True — the 'last shipped' marker. Before the field existed,
    a deployed Site carried no deploy time at all."""
    from datetime import datetime

    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk-da",
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Bright Smile",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    assert site.deployed is True
    assert site.deployed_at is not None, "a successful deploy must stamp deployed_at"
    assert isinstance(site.deployed_at, datetime)


async def test_site_response_exposes_deployed_at_iso(beanie_test_db):
    """``_to_response`` surfaces ``deployed_at`` as an ISO-8601 string on
    SiteResponse after a deploy (the FE renders 'Last deployed <time>')."""
    gen, cf = _FakeGenerator(), _FakeCF()
    doc = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk-da2",
        ripple_spec={"type": "container"},
        theme={},
        name="Bright Smile",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    resp = sites_service._to_response(doc)
    assert resp.deployed_at is not None
    # An ISO-8601 string the FE can parse round-trips back to the stamped datetime.
    from datetime import datetime

    assert datetime.fromisoformat(resp.deployed_at) == doc.deployed_at


async def test_status_deployed_at_none_before_deploy_then_iso_after(beanie_test_db):
    """pocket_status reads ``deployed_at`` None before any deploy (no Site doc) and
    the ISO string after a deploy — the DTO exposes None before the first deploy."""
    # No Site doc yet → status reads deployed_at None (and not live).
    before = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk-da3")
    assert before.deployed_at is None
    assert before.is_live is False

    gen, cf = _FakeGenerator(), _FakeCF()
    await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk-da3",
        ripple_spec={"type": "container"},
        theme={},
        name="Bright Smile",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )

    after = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk-da3")
    assert after.deployed_at is not None
    from datetime import datetime

    # Parses as a real ISO timestamp.
    datetime.fromisoformat(after.deployed_at)


async def test_preview_does_not_stamp_deployed_at(beanie_test_db, monkeypatch, tmp_path):
    """A PREVIEW/edit build returns a transient, NON-persisted Site doc with
    ``deployed=False`` and NO ``deployed_at`` — the stamp is reserved for a real
    live deploy, so an edit preview never marks the pocket as 'just deployed'."""
    from unittest.mock import AsyncMock, patch

    # Local mode so the preview path serves from disk (no CF).
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    gen = _FakeGenerator()
    wire = {"name": "Preview Site", "rippleSpec": {"type": "container"}}

    def _fake_local_deploy(site_id: str, project_dir: str) -> str:
        return f"http://127.0.0.1:9999/{site_id}/"

    with patch("pocketpaw_ee.cloud.pockets.service.get", new=AsyncMock(return_value=wire)):
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk-da4",
            preview=True,
            _generator=gen,
            _local_deploy=_fake_local_deploy,
        )

    assert site.deployed is False, "a preview is not a live deploy"
    assert site.deployed_at is None, "a preview must NOT stamp deployed_at"

    # And no canonical deployed Site doc was persisted, so status reads no deploy time.
    status = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk-da4")
    assert status.deployed_at is None


# ---------------------------------------------------------------------------
# P2b — revert endpoint (service half)
# ---------------------------------------------------------------------------

WS = "ws-rev"
USER = "user-rev"
ONE = {"index.html": "<h1>one</h1>\n"}
TWO = {"index.html": "<h1>two</h1>\n"}


async def _pocket(source: dict) -> str:
    """An html site pocket in ``WS`` owned by ``USER``; a revert reads and writes it."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=WS,
        owner_id=USER,
        name="Rev",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source=dict(source),
        trusted=True,
    )
    assert err is None, err
    return pocket_id


async def test_revert_creates_draft_from_prior_version(beanie_test_db):
    """``revert_pocket_version`` writes a NEW forward-moving DRAFT whose content is a
    snapshot of the target version — the normal review/publish flow then applies."""
    pocket = await _pocket(TWO)
    # v1 (published live), v2 (a later edit). We revert back to v1.
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=pocket, workspace_id=WS, content=dict(ONE)
    )
    await versions.publish(
        scope_type="pocket", scope_id=pocket, workspace_id=WS, version_id=str(v1.id)
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=pocket, workspace_id=WS, content=dict(TWO)
    )

    new_draft = await sites_service.revert_pocket_version(
        workspace_id=WS, user_id=USER, pocket_id=pocket, version_no=v1.version_no
    )

    # A NEW draft, forward of the head, carrying v1's content (revert never mutates
    # history — it is a fresh snapshot the operator can then publish).
    assert new_draft.status == "draft"
    assert new_draft.content == ONE
    assert new_draft.version_no > v1.version_no
    assert new_draft.author == USER
    assert new_draft.label == f"Revert to v{v1.version_no}"

    # It is the current draft for the pocket (request-publish would pick it up).
    head_draft = await versions.get_draft(scope_type="pocket", scope_id=pocket)
    assert head_draft is not None
    assert head_draft.id == new_draft.id


async def test_revert_unknown_version_no_raises(beanie_test_db):
    """An unknown version_no → ValueError (the router maps it to a 404)."""
    pocket = await _pocket(ONE)
    await versions.write_draft(
        scope_type="pocket", scope_id=pocket, workspace_id=WS, content=dict(ONE)
    )
    with pytest.raises(ValueError):
        await sites_service.revert_pocket_version(
            workspace_id=WS, user_id=USER, pocket_id=pocket, version_no=999
        )


async def test_revert_is_tenant_scoped(beanie_test_db):
    """A version_no that exists only under ANOTHER workspace is 'not found' for this
    caller — the tenant guard treats it as no such version → ValueError."""
    pocket = await _pocket(ONE)
    foreign = await versions.write_draft(
        scope_type="pocket", scope_id=pocket, workspace_id="ws-OTHER", content=dict(TWO)
    )
    with pytest.raises(ValueError):
        await sites_service.revert_pocket_version(
            workspace_id=WS, user_id=USER, pocket_id=pocket, version_no=foreign.version_no
        )
