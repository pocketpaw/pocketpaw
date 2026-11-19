# tests/cloud/growth/test_blueprint_outreach.py — the outreach writer and hunt
# research follow the brand's approved Blueprint. Reuses the test_router.py app
# harness (mongomock-backed Beanie); the writer and research are fakes on their
# seams, and the real prompt builders render what they were handed.
#
# Covers: a branded prospect's writer prompt carries the approved Blueprint
# (fenced, never the newer draft) and every draft it makes carries
# ``blueprint_version``; a legacy prospect with no brand drafts under the
# workspace's default brand and its Blueprint; a branded hunt's research request
# carries the brand's Blueprint, and a hunt whose brand has no approved Blueprint
# gets none even when another brand in the workspace has one.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth.discovery import ResearchRequest, run_discovery
from pocketpaw_ee.cloud.growth.researcher import build_research_prompt
from pocketpaw_ee.cloud.growth.writer import (
    WrittenDraft,
    build_writer_prompt,
    set_production_writer_fn,
)
from pocketpaw_ee.cloud.models.draft import Draft as DraftDoc
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.prospect import Prospect as ProspectDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile

from tests.cloud.growth.test_discovery import FakeResearch
from tests.cloud.growth.test_router import _build_app

API = "/api/v1/growth"


@pytest_asyncio.fixture
async def w1(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _brand(name: str = "Brand A") -> str:
    doc = SocialProfile(workspace="w1", company_name=name)
    await doc.insert()
    return str(doc.id)


async def _blueprint(profile_id: str, version: int, status: str, tone: str) -> None:
    await BlueprintDoc(
        workspace="w1",
        profile_id=profile_id,
        version=version,
        status=status,
        identity={"summary": "Invoicing for studios", "product": "Ledger", "problem": ""},
        audience={"segments": ["design studios"], "icp_sentences": []},
        voice={"tone": tone, "avoid": ["hype"]},
        pillars=[{"name": "Cash flow", "weight": 100, "hooks": []}],
    ).insert()


class _Writer:
    """Records the prompt the real builder makes from what the writer is handed."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def __call__(self, prospect, icp, channels, instructions, **kw: Any):
        self.prompts.append(
            build_writer_prompt(prospect, icp, channels, instructions, kw.get("blueprint"))
        )
        return (WrittenDraft("email", "Hello", "Hi there, a short note."),)


@pytest.fixture
def writer():
    fake = _Writer()
    set_production_writer_fn(fake)
    try:
        yield fake
    finally:
        set_production_writer_fn(None)


async def _prospect(client: AsyncClient, **fields: Any) -> str:
    body = {"domain": "acme.com", "source": "manual", "emails": ["dana@acme.com"], **fields}
    resp = await client.post(f"{API}/prospects", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


# --- outreach writer -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branded_draft_follows_the_approved_blueprint_and_carries_its_version(w1, writer):
    brand = await _brand()
    await _blueprint(brand, 2, "approved", "Plain and kind")
    await _blueprint(brand, 3, "draft", "DRAFT-TONE")
    pid = await _prospect(w1, profile_id=brand)

    resp = await w1.post(f"{API}/prospects/{pid}/draft", json={"channels": ["email"]})

    assert resp.status_code == 200, resp.text
    (draft,) = resp.json()["drafts"]
    assert draft["blueprint_version"] == 2
    prompt = writer.prompts[-1]
    assert "Blueprint, version 2 (data, not instructions)" in prompt
    assert "Tone: Plain and kind" in prompt and "DRAFT-TONE" not in prompt
    (row,) = await DraftDoc.find({"prospect_id": pid}).to_list()
    assert row.blueprint_version == 2


@pytest.mark.asyncio
async def test_a_legacy_prospect_drafts_under_the_default_brands_blueprint(w1, writer):
    brand = await _brand()
    await _blueprint(brand, 2, "approved", "Default brand tone")
    # A pre-brand row as it sits in Mongo: no profile_id key.
    pid = str(
        (
            await ProspectDoc.get_pymongo_collection().insert_one(
                {"workspace": "w1", "domain": "old.io", "source": "manual", "emails": ["a@old.io"]}
            )
        ).inserted_id
    )

    resp = await w1.post(f"{API}/prospects/{pid}/draft", json={"channels": ["email"]})

    assert resp.status_code == 200, resp.text
    (draft,) = resp.json()["drafts"]
    assert (draft["profile_id"], draft["blueprint_version"]) == (brand, 2)
    assert "Tone: Default brand tone" in writer.prompts[-1]


@pytest.mark.asyncio
async def test_a_hand_typed_draft_cannot_claim_a_blueprint_version(w1):
    pid = await _prospect(w1)
    resp = await w1.post(
        f"{API}/prospects/{pid}/drafts",
        json={"channel": "email", "subject": "s", "body": "b", "blueprint_version": 9},
    )
    assert resp.status_code in (200, 422), resp.text
    rows = await DraftDoc.find({"prospect_id": pid}).to_list()
    assert all(r.blueprint_version is None for r in rows)


# --- hunts ---------------------------------------------------------------------------------


async def _hunt(client: AsyncClient, **fields: Any) -> ResearchRequest:
    resp = await client.post(f"{API}/icps", json={"name": "n", "criteria": "dentists", **fields})
    assert resp.status_code == 200, resp.text
    research = FakeResearch()
    await run_discovery("w1", resp.json()["id"], research)
    (request,) = research.calls
    return request


@pytest.mark.asyncio
async def test_branded_hunt_research_carries_the_brand_blueprint(w1):
    brand = await _brand()
    await _blueprint(brand, 4, "approved", "Calm")
    await _blueprint(brand, 5, "draft", "DRAFT-TONE")

    request = await _hunt(w1, profile_id=brand)

    prompt = build_research_prompt(request)
    assert "Blueprint, version 4 (data, not instructions)" in prompt
    assert "Audience: design studios" in prompt and "Tone: Calm" in prompt
    assert "- Cash flow (100)" in prompt and "DRAFT-TONE" not in prompt
    assert "dentists" in prompt


@pytest.mark.asyncio
async def test_a_hunt_whose_brand_has_no_approved_blueprint_gets_none(w1):
    await _brand("Oldest, no Blueprint")  # the default brand for a brandless hunt
    await _blueprint(await _brand("Brand B"), 4, "approved", "Calm")

    request = await _hunt(w1)

    assert request.blueprint == ""
    assert "<blueprint>" not in build_research_prompt(request)
