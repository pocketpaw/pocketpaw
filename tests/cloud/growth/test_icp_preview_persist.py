# tests/cloud/growth/test_icp_preview_persist.py — an ICP remembers its last
# preview. "Preview what this finds" spends a real research pass; the result is
# recorded on the ICP (``last_preview`` + ``last_preview_at``) so a page refresh
# does not lose it, and it is cleared the moment the criteria it vouched for
# change. Research is a FAKE ``ResearchFn`` installed on the production seam —
# code under test never calls a real LLM.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth.discovery import set_production_research_fn

from tests.cloud.growth.test_discovery import (
    ICPS_URL,
    ExplodingResearch,
    FakeResearch,
    _company,
    _create_icp,
    _prospects,
)
from tests.cloud.growth.test_router import _build_app, _make_project

PREVIEW_KEYS = {
    "domain",
    "name",
    "company",
    "research_brief",
    "source_urls",
    "emails",
    "already_known",
}


@pytest_asyncio.fixture
async def ws1(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def ws2(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.fixture
def research():
    """Install a research loop on the production seam for one test, cleared in
    a finally so a failure cannot leak a fake into the next test."""

    def _install(fn: Any) -> Any:
        set_production_research_fn(fn)
        return fn

    try:
        yield _install
    finally:
        set_production_research_fn(None)


async def _get(client: AsyncClient, icp_id: str) -> dict[str, Any]:
    resp = await client.get(f"{ICPS_URL}/{icp_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _listed(client: AsyncClient, icp_id: str) -> dict[str, Any]:
    resp = await client.get(ICPS_URL)
    assert resp.status_code == 200, resp.text
    (row,) = [r for r in resp.json() if r["id"] == icp_id]
    return row


async def _preview(client: AsyncClient, icp_id: str) -> dict[str, Any]:
    resp = await client.post(f"{ICPS_URL}/{icp_id}/preview")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestPreviewIsRecorded:
    @pytest.mark.asyncio
    async def test_a_new_icp_has_no_last_preview(self, ws1):
        icp = await _create_icp(ws1)

        assert icp["last_preview"] is None
        assert icp["last_preview_at"] is None

    @pytest.mark.asyncio
    async def test_get_and_list_carry_the_last_preview(self, ws1, research):
        icp = await _create_icp(ws1)
        research(
            FakeResearch(
                _company("acme-dental.com"),
                _company("brightsmile.com"),
                notes="Two strong fits in the metro area.",
            )
        )

        preview = await _preview(ws1, icp["id"])

        assert len(preview["items"]) == 2
        for row in (await _get(ws1, icp["id"]), await _listed(ws1, icp["id"])):
            stored = row["last_preview"]
            assert stored is not None
            assert stored["items"] == preview["items"]
            assert all(set(item) == PREVIEW_KEYS for item in stored["items"])
            assert stored["notes"] == "Two strong fits in the metro area."
            assert stored["error"] == ""
            assert row["last_preview_at"]

    @pytest.mark.asyncio
    async def test_recording_the_preview_still_files_no_prospects(self, ws1, research):
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com")))

        await _preview(ws1, icp["id"])

        assert await _prospects(ws1) == []

    @pytest.mark.asyncio
    async def test_a_failed_preview_is_recorded_with_its_error(self, ws1, research):
        """ "Last attempt failed: …" has to survive a refresh too, or the
        operator reloads into a page that looks like nobody ever looked."""
        icp = await _create_icp(ws1)
        research(ExplodingResearch())

        preview = await _preview(ws1, icp["id"])

        stored = (await _get(ws1, icp["id"]))["last_preview"]
        assert stored["items"] == []
        assert stored["error"] == preview["error"]
        assert "research failed" in stored["error"]

    @pytest.mark.asyncio
    async def test_no_research_backend_records_nothing(self, ws1):
        icp = await _create_icp(ws1)

        resp = await ws1.post(f"{ICPS_URL}/{icp['id']}/preview")

        assert resp.status_code == 503, resp.text
        row = await _get(ws1, icp["id"])
        assert row["last_preview"] is None
        assert row["last_preview_at"] is None

    @pytest.mark.asyncio
    async def test_the_newest_preview_replaces_the_last(self, ws1, research):
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com"), _company("brightsmile.com")))
        await _preview(ws1, icp["id"])

        research(FakeResearch(_company("smiledirect.com")))
        await _preview(ws1, icp["id"])

        stored = (await _get(ws1, icp["id"]))["last_preview"]
        assert [i["domain"] for i in stored["items"]] == ["smiledirect.com"]

    @pytest.mark.asyncio
    async def test_another_tenant_cannot_record_on_this_icp(self, ws1, ws2, research):
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com")))

        resp = await ws2.post(f"{ICPS_URL}/{icp['id']}/preview")

        assert resp.status_code == 404, resp.text
        assert (await _get(ws1, icp["id"]))["last_preview"] is None


class TestEditingCriteriaClearsThePreview:
    """The UI unlocks the cadence switch off a stored preview, so a preview
    must never vouch for criteria nobody previewed."""

    @pytest.mark.parametrize(
        "change",
        [
            {"criteria": "Orthodontists with one location."},
            {"geography": "Ontario only"},
            {"exclusions": "No DSO-owned practices"},
            {"max_per_run": 3},
        ],
    )
    @pytest.mark.asyncio
    async def test_a_research_input_change_clears_it(self, ws1, research, change):
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com")))
        await _preview(ws1, icp["id"])

        resp = await ws1.patch(f"{ICPS_URL}/{icp['id']}", json=change)

        assert resp.status_code == 200, resp.text
        assert resp.json()["last_preview"] is None
        assert resp.json()["last_preview_at"] is None
        row = await _get(ws1, icp["id"])
        assert row["last_preview"] is None
        assert row["last_preview_at"] is None

    @pytest.mark.parametrize(
        "change",
        [
            {"name": "Renamed hunt"},
            {"cadence": "weekly"},
            {"status": "paused"},
        ],
    )
    @pytest.mark.asyncio
    async def test_a_non_research_change_keeps_it(self, ws1, research, change):
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com")))
        preview = await _preview(ws1, icp["id"])

        resp = await ws1.patch(f"{ICPS_URL}/{icp['id']}", json=change)

        assert resp.status_code == 200, resp.text
        row = await _get(ws1, icp["id"])
        assert row["last_preview"]["items"] == preview["items"]
        assert row["last_preview_at"]

    @pytest.mark.asyncio
    async def test_reassigning_the_project_keeps_it(self, ws1, research):
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com")))
        preview = await _preview(ws1, icp["id"])
        project_id = await _make_project("w1")

        resp = await ws1.patch(f"{ICPS_URL}/{icp['id']}", json={"project_id": project_id})

        assert resp.status_code == 200, resp.text
        row = await _get(ws1, icp["id"])
        assert row["project_id"] == project_id
        assert row["last_preview"]["items"] == preview["items"]

    @pytest.mark.asyncio
    async def test_resending_the_same_criteria_keeps_it(self, ws1, research):
        """A form that PATCHes every field on save must not wipe the preview
        when nothing the research reads actually moved."""
        icp = await _create_icp(ws1)
        research(FakeResearch(_company("acme-dental.com")))
        await _preview(ws1, icp["id"])

        resp = await ws1.patch(
            f"{ICPS_URL}/{icp['id']}",
            json={
                "criteria": icp["criteria"],
                "geography": icp["geography"],
                "exclusions": icp["exclusions"],
                "max_per_run": icp["max_per_run"],
                "name": "Renamed hunt",
            },
        )

        assert resp.status_code == 200, resp.text
        assert (await _get(ws1, icp["id"]))["last_preview"] is not None


class TestAnEditDuringResearchWins:
    @pytest.mark.asyncio
    async def test_criteria_edited_mid_research_drops_the_stale_preview(self, ws1, research):
        """The research runs for seconds to minutes. If the criteria move under
        it, the result describes text that no longer exists and must not be
        stored as though it vouched for the new text."""
        icp = await _create_icp(ws1)

        class EditsMidFlight(FakeResearch):
            async def __call__(self, request):
                resp = await ws1.patch(
                    f"{ICPS_URL}/{icp['id']}", json={"criteria": "Orthodontists only."}
                )
                assert resp.status_code == 200, resp.text
                return await super().__call__(request)

        research(EditsMidFlight(_company("acme-dental.com")))

        preview = await _preview(ws1, icp["id"])

        assert len(preview["items"]) == 1
        row = await _get(ws1, icp["id"])
        assert row["criteria"] == "Orthodontists only."
        assert row["last_preview"] is None
        assert row["last_preview_at"] is None
