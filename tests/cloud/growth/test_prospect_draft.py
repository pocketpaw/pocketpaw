# tests/cloud/growth/test_prospect_draft.py — drafting first-touch copy for ONE
# prospect with the writer agent.
#
# ``POST /growth/prospects/{id}/draft`` writes only for channels the prospect
# can actually be reached on (email needs an address, LinkedIn a profile URL,
# WhatsApp a number and an opt-in), skips a channel that already holds a live
# first-touch draft, reports every skipped channel with its reason, and ignores
# anything the writer returns for a channel it was not given. Drafts land as
# ``draft`` and move the prospect to ``drafted`` without ever regressing it.
#
# The writer is a fake installed on the ``WriterFn`` seam; the seeding and
# production-fn tests drive a fake agent pool. No real run.

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.realtime.events import AgentCreated
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.growth.domain import Prospect
from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
from pocketpaw_ee.cloud.growth.writer import (
    GROWTH_WRITER_PROMPT,
    GROWTH_WRITER_SLUG,
    WrittenDraft,
    agent_write_drafts,
    build_writer_prompt,
    parse_writer_response,
    set_production_writer_fn,
)
from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.draft import Draft as DraftDoc
from pocketpaw_ee.cloud.models.prospect import Prospect as ProspectDoc
from pocketpaw_ee.cloud.models.workspace import Workspace

from tests.cloud.growth.test_router import _build_app

PROSPECTS = "/api/v1/growth/prospects"
EMAIL = WrittenDraft("email", "Your booking line", "Hi Dana, saw you book by phone.")
LINKEDIN = WrittenDraft("linkedin", "", "Hi Dana, quick note about bookings.")
WHATSAPP = WrittenDraft("whatsapp", "", "Hi, quick question about bookings.")


@pytest_asyncio.fixture
async def w1(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


class FakeWriter:
    def __init__(self, *drafts: WrittenDraft, exc: Exception | None = None):
        self.drafts = drafts
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, prospect, icp, channels, instructions):
        self.calls.append(
            {"prospect": prospect, "icp": icp, "channels": channels, "instructions": instructions}
        )
        if self.exc is not None:
            raise self.exc
        return self.drafts


@pytest.fixture
def seam():
    def _install(fn: Any) -> Any:
        set_production_writer_fn(fn)
        return fn

    try:
        yield _install
    finally:
        set_production_writer_fn(None)


async def _create(client: AsyncClient, **fields: Any) -> dict[str, Any]:
    payload = {"domain": "acme.com", "source": "manual", **fields}
    resp = await client.post(PROSPECTS, json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _reachable(client: AsyncClient, **fields: Any) -> dict[str, Any]:
    return await _create(
        client,
        emails=["dana@acme.com"],
        linkedin_url="https://www.linkedin.com/in/dana",
        whatsapp_number="+15550001111",
        opted_in=True,
        **fields,
    )


async def _draft(client: AsyncClient, prospect_id: str, **body: Any):
    return await client.post(f"{PROSPECTS}/{prospect_id}/draft", json=body)


async def _set(prospect_id: str, **fields: Any) -> None:
    doc = await ProspectDoc.get(PydanticObjectId(prospect_id))
    for key, value in fields.items():
        setattr(doc, key, value)
    await doc.save()


async def _stored_drafts(prospect_id: str) -> list[DraftDoc]:
    return await DraftDoc.find({"prospect_id": prospect_id}).to_list()


# ---------------------------------------------------------------------------
# Parser and prompt
# ---------------------------------------------------------------------------


class TestParser:
    def test_reads_drafts_and_keeps_the_first_per_channel(self):
        text = "Sure:\n" + json.dumps(
            {
                "drafts": [
                    {"channel": "Email", "subject": " Hi ", "body": " Body "},
                    {"channel": "email", "subject": "dup", "body": "dup"},
                    {"channel": "linkedin", "body": "Note"},
                    {"channel": "fax", "body": "x"},
                    {"channel": "whatsapp", "body": "   "},
                    "junk",
                ]
            }
        )
        assert parse_writer_response(text) == (
            WrittenDraft("email", "Hi", "Body"),
            WrittenDraft("linkedin", "", "Note"),
        )

    @pytest.mark.parametrize("text", ["", "nope", "{", '{"drafts": "x"}', '{"other": []}'])
    def test_never_raises_on_junk(self, text):
        assert parse_writer_response(text) == ()


class TestPrompt:
    def test_carries_research_channels_and_delimited_operator_notes(self):
        prospect = Prospect(
            id="p",
            workspace_id="w",
            name="Dana",
            company="Acme Dental",
            domain="acme.com",
            source="manual",
            research_brief="old brief",
            research={"summary": "Three chairs.", "hook": "Phone-only booking."},
        )
        text = build_writer_prompt(prospect, None, ["email", "linkedin"], "Mention Austin.")
        for needle in ("acme.com", "Acme Dental", "Dana", "Three chairs.", "Phone-only"):
            assert needle in text
        assert "email, linkedin" in text
        assert "<operator-notes>\nMention Austin.\n</operator-notes>" in text
        assert "old brief" not in text

    def test_falls_back_to_the_brief_without_research(self):
        prospect = Prospect(
            id="p",
            workspace_id="w",
            name="",
            company="",
            domain="a.io",
            source="manual",
            research_brief="The brief.",
        )
        text = build_writer_prompt(prospect, None, ["email"])
        assert "The brief." in text
        assert "operator-notes" not in text


# ---------------------------------------------------------------------------
# The route: errors
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.asyncio
    async def test_503_when_no_writer_is_wired(self, w1):
        p = await _reachable(w1)
        resp = await _draft(w1, p["id"])
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "prospect.writer_unavailable"

    @pytest.mark.asyncio
    async def test_404_for_missing_and_foreign(self, w1, w2, seam):
        fake = seam(FakeWriter(EMAIL))
        p = await _reachable(w1)
        assert (await _draft(w1, "not-an-id")).status_code == 404
        assert (await _draft(w2, p["id"])).status_code == 404
        assert fake.calls == []

    @pytest.mark.parametrize(
        ("reachable", "body"),
        [(False, {}), (True, {"channels": []})],
        ids=["nothing_reachable", "empty_channel_list"],
    )
    @pytest.mark.asyncio
    async def test_422_when_no_channel_is_left(self, w1, seam, reachable, body):
        fake = seam(FakeWriter(EMAIL))
        p = await (_reachable(w1) if reachable else _create(w1))
        resp = await _draft(w1, p["id"], **body)
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "prospect.no_channel"
        assert "research the prospect first" in err["message"]
        assert fake.calls == []

    @pytest.mark.parametrize(
        "fake",
        [
            FakeWriter(exc=ResearchUnavailable("the writer run failed")),
            FakeWriter(exc=RuntimeError("boom")),
            FakeWriter(WrittenDraft("email", "", "no subject")),
        ],
        ids=["run_failed", "unexpected_error", "no_usable_draft"],
    )
    @pytest.mark.asyncio
    async def test_502_and_nothing_stored(self, w1, seam, fake):
        seam(fake)
        p = await _create(w1, emails=["dana@acme.com"])
        resp = await _draft(w1, p["id"])
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "prospect.draft_failed"
        assert await _stored_drafts(p["id"]) == []
        assert (await w1.get(f"{PROSPECTS}/{p['id']}")).json()["status"] == "new"


# ---------------------------------------------------------------------------
# The route: eligibility, skipping, storing
# ---------------------------------------------------------------------------


class TestDrafting:
    @pytest.mark.asyncio
    async def test_drafts_every_reachable_channel(self, w1, seam):
        fake = seam(FakeWriter(EMAIL, LINKEDIN, WHATSAPP))
        p = await _reachable(w1)
        resp = await _draft(w1, p["id"], instructions="Keep it brief.")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skipped"] == []
        by_channel = {d["channel"]: d for d in body["drafts"]}
        assert set(by_channel) == {"email", "linkedin", "whatsapp"}
        assert by_channel["email"]["subject"] == "Your booking line"
        assert by_channel["linkedin"]["subject"] is None
        for d in body["drafts"]:
            assert d["status"] == "draft"
            assert d["variant"] == "first_touch"
            assert d["prospect_id"] == p["id"]
        assert fake.calls[0]["channels"] == ["email", "linkedin", "whatsapp"]
        assert fake.calls[0]["instructions"] == "Keep it brief."
        assert len(await _stored_drafts(p["id"])) == 3
        assert (await w1.get(f"{PROSPECTS}/{p['id']}")).json()["status"] == "drafted"

    @pytest.mark.parametrize(
        ("fields", "written", "skipped"),
        [
            (
                {"emails": ["dana@acme.com"], "whatsapp_number": "+15550001111"},
                "email",
                {
                    "linkedin": "no LinkedIn profile on file",
                    "whatsapp": "the prospect has not opted in to WhatsApp",
                },
            ),
            (
                {"linkedin_url": "https://www.linkedin.com/in/dana"},
                "linkedin",
                {
                    "email": "no email address on file",
                    "whatsapp": "no WhatsApp number on file",
                },
            ),
        ],
        ids=["no_linkedin_no_optin", "no_email_no_number"],
    )
    @pytest.mark.asyncio
    async def test_unreachable_channels_are_skipped_with_reasons(
        self, w1, seam, fields, written, skipped
    ):
        fake = seam(FakeWriter(EMAIL, LINKEDIN))
        p = await _create(w1, **fields)
        body = (await _draft(w1, p["id"])).json()
        assert [d["channel"] for d in body["drafts"]] == [written]
        assert {s["channel"]: s["reason"] for s in body["skipped"]} == skipped
        assert fake.calls[0]["channels"] == [written]

    @pytest.mark.asyncio
    async def test_only_requested_channels_are_written(self, w1, seam):
        fake = seam(FakeWriter(EMAIL, LINKEDIN, WHATSAPP))
        p = await _reachable(w1)
        body = (await _draft(w1, p["id"], channels=["linkedin", "linkedin"])).json()
        assert [d["channel"] for d in body["drafts"]] == ["linkedin"]
        assert body["skipped"] == []
        assert fake.calls[0]["channels"] == ["linkedin"]
        assert [d.channel for d in await _stored_drafts(p["id"])] == ["linkedin"]

    @pytest.mark.asyncio
    async def test_a_draft_for_an_ineligible_channel_is_ignored(self, w1, seam):
        seam(FakeWriter(EMAIL, LINKEDIN, WHATSAPP))
        p = await _create(w1, emails=["dana@acme.com"])
        body = (await _draft(w1, p["id"])).json()
        assert [d["channel"] for d in body["drafts"]] == ["email"]
        assert [d.channel for d in await _stored_drafts(p["id"])] == ["email"]

    @pytest.mark.parametrize(
        ("drafts", "written", "skipped"),
        [
            ((EMAIL,), "email", ("linkedin", "the writer returned no draft for this channel")),
            (
                (WrittenDraft("email", "  ", "Body"), LINKEDIN),
                "linkedin",
                ("email", "the writer returned an email with no subject"),
            ),
        ],
        ids=["channel_left_out", "email_without_subject"],
    )
    @pytest.mark.asyncio
    async def test_unusable_writer_output_is_skipped_with_a_reason(
        self, w1, seam, drafts, written, skipped
    ):
        seam(FakeWriter(*drafts))
        p = await _create(w1, emails=["dana@acme.com"], linkedin_url="https://linkedin.com/in/d")
        body = (await _draft(w1, p["id"])).json()
        assert [d["channel"] for d in body["drafts"]] == [written]
        channel, reason = skipped
        assert body["skipped"] == [
            {"channel": "whatsapp", "reason": "no WhatsApp number on file"},
            {"channel": channel, "reason": reason},
        ]

    @pytest.mark.asyncio
    async def test_writer_output_is_normalized(self, w1, seam):
        seam(
            FakeWriter(
                WrittenDraft("email", "s" * 300, "b" * 12_000),
                WrittenDraft("linkedin", "Subject", "Note"),
            )
        )
        p = await _create(w1, emails=["dana@acme.com"], linkedin_url="https://linkedin.com/in/d")
        by_channel = {d["channel"]: d for d in (await _draft(w1, p["id"])).json()["drafts"]}
        assert len(by_channel["email"]["subject"]) == 200
        assert len(by_channel["email"]["body"]) == 10_000
        assert by_channel["linkedin"]["subject"] is None

    @pytest.mark.asyncio
    async def test_already_drafted_channels_are_skipped(self, w1, seam):
        fake = seam(FakeWriter(EMAIL, LINKEDIN))
        p = await _create(w1, emails=["dana@acme.com"], linkedin_url="https://linkedin.com/in/d")
        existing = await w1.post(
            f"{PROSPECTS}/{p['id']}/drafts",
            json={"channel": "email", "subject": "s", "body": "b"},
        )
        assert existing.status_code in (200, 201), existing.text

        body = (await _draft(w1, p["id"])).json()
        assert [d["channel"] for d in body["drafts"]] == ["linkedin"]
        assert {"channel": "email", "reason": "already drafted"} in body["skipped"]
        assert fake.calls[0]["channels"] == ["linkedin"]

    @pytest.mark.parametrize(
        ("status", "variant", "blocks"),
        [
            ("draft", "first_touch", True),
            ("proposed", "first_touch", True),
            ("approved", "first_touch", True),
            ("sent", "first_touch", True),
            ("rejected", "first_touch", False),
            ("draft", "follow_up", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_which_existing_drafts_count_as_already_drafted(
        self, w1, seam, status, variant, blocks
    ):
        seam(FakeWriter(EMAIL))
        p = await _create(w1, emails=["dana@acme.com"])
        await DraftDoc(
            workspace="w1",
            prospect_id=p["id"],
            channel="email",
            subject="s",
            body="b",
            variant=variant,
            status=status,
        ).insert()
        resp = await _draft(w1, p["id"], channels=["email"])
        assert resp.status_code == (422 if blocks else 200), resp.text
        if blocks:
            assert "already has a first-touch draft" in resp.json()["error"]["message"]

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("new", "drafted"),
            ("qualified", "drafted"),
            ("in_sequence", "in_sequence"),
            ("replied", "replied"),
        ],
    )
    @pytest.mark.asyncio
    async def test_status_moves_to_drafted_and_never_regresses(self, w1, seam, before, after):
        seam(FakeWriter(EMAIL))
        p = await _create(w1, emails=["dana@acme.com"])
        await _set(p["id"], status=before)
        assert (await _draft(w1, p["id"])).status_code == 200
        assert (await w1.get(f"{PROSPECTS}/{p['id']}")).json()["status"] == after


# ---------------------------------------------------------------------------
# Seeding and the production fn
# ---------------------------------------------------------------------------


async def _writer(workspace_id: str) -> Agent | None:
    return await Agent.find_one(Agent.workspace == workspace_id, Agent.slug == GROWTH_WRITER_SLUG)


class _FakePool:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        yield SimpleNamespace(type="message", content=self.answer)


@pytest.mark.usefixtures("mongo_db")
class TestWriterAgent:
    @pytest.mark.asyncio
    async def test_seed_inserts_a_toolless_exclusive_agent(self, recording_bus):
        doc, created = await agents_service.seed_growth_writer_agent("w1", "u1")
        assert created is True
        assert doc.owner == "u1"
        assert doc.config.tool_mode == "exclusive"
        assert list(doc.config.tools) == []
        assert doc.config.system_prompt == GROWTH_WRITER_PROMPT
        assert doc.config.soul_enabled is False
        assert doc.config.trust_level == 1
        assert [e.data["slug"] for e in recording_bus.events if isinstance(e, AgentCreated)] == [
            GROWTH_WRITER_SLUG
        ]

        again, created_again = await agents_service.seed_growth_writer_agent("w1", "u1")
        assert created_again is False and str(again.id) == str(doc.id)

    @pytest.mark.asyncio
    async def test_seed_narrows_a_widened_agent(self):
        await Agent(
            workspace="w1",
            name="Growth writer",
            slug=GROWTH_WRITER_SLUG,
            owner="u1",
            config=AgentConfig(tools=["WebSearch", "Bash"], tool_mode="additive"),
        ).insert()
        await agents_service.seed_growth_writer_agent("w1", "u1")
        stored = await _writer("w1")
        assert stored.config.tool_mode == "exclusive"
        assert list(stored.config.tools) == []

    @pytest.mark.asyncio
    async def test_boot_backfill_seeds_every_workspace_once(self):
        await Workspace(name="a", slug="a", owner="owner-a").insert()
        await Workspace(name="b", slug="b", owner="owner-b").insert()
        assert await agents_service.ensure_growth_writer_agent_all_workspaces() == 2
        assert await agents_service.ensure_growth_writer_agent_all_workspaces() == 0

    @pytest.mark.asyncio
    async def test_agent_write_drafts_seeds_and_runs(self):
        ws = Workspace(name="acme", slug="acme", owner="owner-1")
        await ws.insert()
        workspace_id = str(ws.id)
        pool = _FakePool(
            json.dumps({"drafts": [{"channel": "email", "subject": "S", "body": "B"}]})
        )
        prospect = Prospect(
            id="p1", workspace_id=workspace_id, name="", company="", domain="a.io", source="x"
        )
        with patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool):
            out = await agent_write_drafts(prospect, None, ["email"], "")

        seeded = await _writer(workspace_id)
        assert seeded is not None and seeded.owner == "owner-1"
        assert out == (WrittenDraft("email", "S", "B"),)
        assert pool.calls[0]["agent_id"] == str(seeded.id)
        assert pool.calls[0]["session_key"].startswith(f"growth-writer:{workspace_id}:p1:")
