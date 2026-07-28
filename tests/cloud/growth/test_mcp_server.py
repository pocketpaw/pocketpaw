# tests/cloud/growth/test_mcp_server.py — the agent-facing /growth MCP surface
# (``ee/pocketpaw_ee/agent/mcp_servers/growth.py``).
#
# The security-critical assertion is ``TestTheGateHolds``: NO exposed tool can
# reach a draft status in ``GATE_OWNED_TARGETS`` (``approved`` / ``sent``). It
# is written against the TOOL LIST and the tool SCHEMAS rather than against the
# nine tools that exist today, so adding a tenth tool that takes a ``status``
# argument — or naming one ``growth_send_now`` — fails this file before it can
# ship. The dynamic half drives every tool through the real handlers and asserts
# no draft ever left ``draft``/``proposed``, and that ``service.gate_transition``
# (the executor's seam) is never called from here.
#
# The rest pins the round trip: every tool goes through
# ``ee.cloud.growth.service`` against real (mongomock) Beanie, tenancy comes
# from the chat identity ContextVars and never from an argument, the RBAC tiers
# match the HTTP routes, and a call outside a chat stream errors instead of
# operating on a blank workspace.
#
# Harness: the ``mongo_db`` fixture from tests/cloud/conftest.py, real User docs
# (the RBAC gate loads the doc and reads ``.workspaces``), identity bound via
# ``attach_agent_identity``, and one tmp ``InstinctStore`` behind
# ``pocketpaw.stores.get_instinct_store`` so the propose path files a real
# Action — same seam test_gate.py patches.
#
# Created 2026-07-28 (feat/growth-mcp): new module.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.agent.mcp_servers import growth as growth_mcp  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.growth.domain import GATE_OWNED_TARGETS  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _identity:
    """Bind the workspace/user ContextVars the handlers read, then reset."""

    def __init__(self, workspace: str, user: str) -> None:
        self._ws, self._user = workspace, user
        self._token: Any = None

    def __enter__(self) -> _identity:
        self._token = attach_agent_identity(workspace_id=self._ws, user_id=self._user)
        return self

    def __exit__(self, *_exc: Any) -> None:
        detach_agent_identity(self._token)


async def _seed_user(email: str, workspace: str, role: str = "admin") -> str:
    """Insert a real User doc with a membership the RBAC gate can resolve."""
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

    doc = User(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Growth Operator",
        active_workspace=workspace,
        workspaces=[WorkspaceMembership(workspace=workspace, role=role)],
    )
    await doc.insert()
    return str(doc.id)


@pytest.fixture
def instinct_store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """One tmp store behind every seam that resolves an instinct store, so
    ``growth_propose_send`` files a real Action instead of touching
    ``~/.pocketpaw/instinct.db``."""
    store = InstinctStore(tmp_path / "growth_mcp.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


@pytest_asyncio.fixture
async def admin_w1(mongo_db: Any) -> str:
    return await _seed_user("admin-w1@growth.test", "w1", role="admin")


@pytest_asyncio.fixture
async def admin_w2(mongo_db: Any) -> str:
    return await _seed_user("admin-w2@growth.test", "w2", role="admin")


@pytest_asyncio.fixture
async def member_w1(mongo_db: Any) -> str:
    return await _seed_user("member-w1@growth.test", "w1", role="member")


def _payload(response: dict) -> dict:
    """Decode a tool response's JSON body. Fails loudly on an error envelope so
    a broken call can't be mistaken for an empty result."""
    assert not response.get("is_error"), response["content"][0]["text"]
    return json.loads(response["content"][0]["text"])


def _error_text(response: dict) -> str:
    assert response.get("is_error"), f"expected an error envelope, got {response}"
    return response["content"][0]["text"]


async def _seed_prospect(user_id: str, **over: Any) -> dict:
    args = {
        "domain": "acme-dental.com",
        "name": "Sam Founder",
        "company": "Acme Dental",
        "research_brief": "Booking flow dead-ends at a contact form.",
        "tier": "a",
        **over,
    }
    with _identity("w1", user_id):
        return _payload(await growth_mcp._upsert_prospect_handler(args))["prospect"]


async def _seed_draft(user_id: str, prospect_id: str, **over: Any) -> dict:
    args = {
        "prospect_id": prospect_id,
        "channel": "email",
        "subject": "A faster path from search to booked",
        "body": "Saw your online booking stops at a contact form — here is a live demo.",
        **over,
    }
    with _identity("w1", user_id):
        return _payload(await growth_mcp._create_draft_handler(args))["draft"]


# ---------------------------------------------------------------------------
# The gate — the assertion this whole module exists for
# ---------------------------------------------------------------------------


class TestTheGateHolds:
    """The agent's reach ends at ``proposed``. Written against the tool list and
    the schemas, so a FUTURE tool addition trips these before it ships."""

    def test_the_exposed_tool_set_is_exactly_this(self):
        """A new tool must be added here deliberately — which forces whoever
        adds it to read the rest of this class."""
        tools = growth_mcp._build_tools()
        assert tools is not None, "the Claude Agent SDK must be installed for this suite"
        assert {t.name for t in tools} == {
            "growth_list_prospects",
            "growth_get_prospect",
            "growth_list_drafts",
            "growth_upsert_prospect",
            "growth_create_draft",
            "growth_update_draft",
            "growth_propose_send",
            "growth_propose_send_batch",
            "growth_linkedin_queue",
        }
        assert set(growth_mcp.GROWTH_TOOL_NAMES) == {t.name for t in tools}
        assert set(growth_mcp.GROWTH_TOOL_IDS) == {
            f"mcp__pocketpaw_growth__{t.name}" for t in tools
        }

    def test_no_tool_name_claims_to_send(self):
        """A tool is named for what it does. Nothing here sends, dispatches or
        approves — the only outbound verbs are PROPOSE verbs."""
        for tool in growth_mcp._build_tools():
            forbidden = ("send", "dispatch", "approve", "deliver", "transition")
            offending = [word for word in forbidden if word in tool.name]
            assert not offending or tool.name.startswith("growth_propose_send"), (
                f"{tool.name} names a verb the agent does not have: {offending}"
            )

    def test_no_write_tool_takes_a_status_argument(self):
        """The legal moves are exposed as named verbs. A ``status`` INPUT on a
        write tool would be a shape of argument that could ask for
        ``approved`` — so the write tools have none at all, and the two reads
        that do have one use it as a filter."""
        reads_that_filter_on_status = {"growth_list_prospects", "growth_list_drafts"}
        for tool in growth_mcp._build_tools():
            props = tool.input_schema.get("properties", {})
            if tool.name in reads_that_filter_on_status:
                continue
            assert "status" not in props, f"{tool.name} accepts a status argument"

    def test_no_tool_schema_offers_a_gate_owned_value(self):
        """No enum on any tool offers ``approved`` or ``sent`` as something to
        SET. The two read filters may name them (reading an approved draft is
        fine — the LinkedIn queue is built on it), so this walks the write
        tools only."""
        writes = {
            "growth_upsert_prospect",
            "growth_create_draft",
            "growth_update_draft",
            "growth_propose_send",
            "growth_propose_send_batch",
        }
        for tool in growth_mcp._build_tools():
            if tool.name not in writes:
                continue
            for prop, spec in tool.input_schema.get("properties", {}).items():
                values = set(spec.get("enum") or ())
                assert not (values & GATE_OWNED_TARGETS), (
                    f"{tool.name}.{prop} offers a gate-owned value: {values & GATE_OWNED_TARGETS}"
                )

    def test_the_module_never_reaches_the_gate_seam(self):
        """``service.gate_transition`` is how the executor and the dispatch
        worker walk the gate-owned edges. It must be unreachable from the agent
        surface — including via ``mark_linkedin_sent``, which walks
        approved→sent."""
        source = Path(growth_mcp.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for seam in ("gate_transition", "mark_linkedin_sent", "service.transition"):
            assert seam not in code, f"the growth MCP module references {seam}"

    def test_no_tool_accepts_a_workspace_id(self):
        """Tenancy comes from the chat stream's identity. A model that could
        name a tenant could read another one."""
        for tool in growth_mcp._build_tools():
            props = tool.input_schema.get("properties", {})
            assert "workspace_id" not in props, f"{tool.name} accepts a workspace_id"
            assert tool.input_schema.get("additionalProperties") is False, (
                f"{tool.name} allows extra properties — a stray key could smuggle one in"
            )

    @pytest.mark.asyncio
    async def test_driving_every_tool_never_reaches_approved_or_sent(
        self, mongo_db, admin_w1, instinct_store, monkeypatch
    ):
        """The dynamic half: run the whole surface end to end and assert the
        drafts stopped at ``proposed``, and that nothing called the gate seam."""
        from pocketpaw_ee.cloud.growth import service as growth_service

        calls: list[tuple] = []

        async def _tripwire(*args: Any, **kwargs: Any):
            calls.append((args, kwargs))
            raise AssertionError("the agent surface reached the gate seam")

        monkeypatch.setattr(growth_service, "gate_transition", _tripwire)
        monkeypatch.setattr(growth_service, "mark_linkedin_sent", _tripwire)

        prospect = await _seed_prospect(admin_w1)
        draft = await _seed_draft(admin_w1, prospect["id"])
        second = await _seed_draft(
            admin_w1, prospect["id"], channel="linkedin", subject=None, body="Connect note."
        )

        with _identity("w1", admin_w1):
            await growth_mcp._list_prospects_handler({})
            await growth_mcp._get_prospect_handler({"prospect_id": prospect["id"]})
            await growth_mcp._list_drafts_handler({})
            await growth_mcp._update_draft_handler({"draft_id": draft["id"], "body": "revised"})
            await growth_mcp._propose_send_handler({"draft_id": draft["id"]})
            await growth_mcp._propose_send_batch_handler({"draft_ids": [second["id"]]})
            await growth_mcp._linkedin_queue_handler({})
            drafts = _payload(await growth_mcp._list_drafts_handler({}))["drafts"]

        assert calls == []
        assert {d["status"] for d in drafts} == {"proposed"}
        assert not {d["status"] for d in drafts} & GATE_OWNED_TARGETS

    @pytest.mark.asyncio
    async def test_a_proposed_draft_can_no_longer_be_edited(
        self, mongo_db, admin_w1, instinct_store
    ):
        """Editing a proposed draft would put copy on the wire that nobody
        approved — the same bypass as sending, wearing an edit's clothes."""
        prospect = await _seed_prospect(admin_w1)
        draft = await _seed_draft(admin_w1, prospect["id"])
        with _identity("w1", admin_w1):
            await growth_mcp._propose_send_handler({"draft_id": draft["id"]})
            response = await growth_mcp._update_draft_handler(
                {"draft_id": draft["id"], "body": "swapped after approval was asked for"}
            )
            assert "draft.not_editable" in _error_text(response)
            fetched = _payload(
                await growth_mcp._get_prospect_handler({"prospect_id": prospect["id"]})
            )
        assert fetched["drafts"][0]["body"] == draft["body"]


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_provider_exposes_the_server_and_its_tool_ids(self):
        from pocketpaw_ee.extensions import CloudGrowthMcpProvider

        provider = CloudGrowthMcpProvider()
        built = provider.build_server()
        assert built is not None
        name, server = built
        assert name == growth_mcp.SERVER_NAME == "pocketpaw_growth"
        assert server is not None
        assert set(provider.tool_ids()) == set(growth_mcp.GROWTH_TOOL_IDS)


# ---------------------------------------------------------------------------
# Round trips — every tool goes through the service
# ---------------------------------------------------------------------------


class TestRoundTrips:
    @pytest.mark.asyncio
    async def test_upsert_then_list_then_get(self, mongo_db, admin_w1):
        prospect = await _seed_prospect(admin_w1)
        assert prospect["domain"] == "acme-dental.com"
        assert prospect["tier"] == "a"

        with _identity("w1", admin_w1):
            listed = _payload(await growth_mcp._list_prospects_handler({"q": "Acme"}))
            fetched = _payload(
                await growth_mcp._get_prospect_handler({"prospect_id": prospect["id"]})
            )

        assert listed["total"] == 1
        assert listed["showing"] == 1
        row = listed["prospects"][0]
        assert row["company"] == "Acme Dental"
        # Compact by design — the brief and contact details are not in the list.
        assert "research_brief" not in row
        assert "emails" not in row

        assert fetched["prospect"]["research_brief"].startswith("Booking flow")
        assert fetched["drafts"] == []

    @pytest.mark.asyncio
    async def test_upsert_normalises_the_domain_and_dedupes(self, mongo_db, admin_w1):
        """The domain is the dedupe key, canonicalised at the DTO boundary — a
        pasted URL and a bare hostname are the same prospect."""
        first = await _seed_prospect(admin_w1, domain="https://www.Acme-Dental.com/about")
        assert first["domain"] == "acme-dental.com"
        second = await _seed_prospect(admin_w1, domain="acme-dental.com", tier="b")
        assert second["id"] == first["id"]

        with _identity("w1", admin_w1):
            listed = _payload(await growth_mcp._list_prospects_handler({}))
        assert listed["total"] == 1
        assert listed["prospects"][0]["tier"] == "b"

    @pytest.mark.asyncio
    async def test_upsert_merges_instead_of_blanking_what_it_was_not_told(self, mongo_db, admin_w1):
        """A re-upsert enriches. Fields the agent leaves out keep their stored
        values, and the lifecycle status — which the agent cannot set — is
        carried forward rather than reset to ``new``."""
        prospect = await _seed_prospect(admin_w1)
        first_draft = await _seed_draft(admin_w1, prospect["id"])
        assert first_draft["status"] == "draft"

        with _identity("w1", admin_w1):
            # Writing a draft moved the prospect to ``drafted``.
            before = _payload(
                await growth_mcp._get_prospect_handler({"prospect_id": prospect["id"]})
            )["prospect"]
            assert before["status"] == "drafted"

            enriched = _payload(
                await growth_mcp._upsert_prospect_handler(
                    {"domain": "acme-dental.com", "emails": ["sam@acme-dental.com"]}
                )
            )["prospect"]

        assert enriched["emails"] == ["sam@acme-dental.com"]
        assert enriched["status"] == "drafted"  # not reset
        assert enriched["name"] == "Sam Founder"  # not blanked
        assert enriched["research_brief"].startswith("Booking flow")
        assert enriched["tier"] == "a"

    @pytest.mark.asyncio
    async def test_create_then_update_then_propose(self, mongo_db, admin_w1, instinct_store):
        prospect = await _seed_prospect(admin_w1)
        draft = await _seed_draft(admin_w1, prospect["id"])
        assert draft["status"] == "draft"

        with _identity("w1", admin_w1):
            edited = _payload(
                await growth_mcp._update_draft_handler(
                    {"draft_id": draft["id"], "body": "Rewrote the opener.", "subject": "New line"}
                )
            )["draft"]
            assert edited["body"] == "Rewrote the opener."
            assert edited["subject"] == "New line"
            assert edited["status"] == "draft"

            proposed = _payload(await growth_mcp._propose_send_handler({"draft_id": draft["id"]}))

        assert proposed["status"] == "proposed"
        assert proposed["proposal_id"]
        assert proposed["draft"]["status"] == "proposed"
        assert "NOTHING has been sent" in proposed["note"]
        # The proposal is a real, durably stored Instinct Action.
        assert await instinct_store.get_action(proposed["proposal_id"]) is not None

    @pytest.mark.asyncio
    async def test_propose_batch_files_one_proposal_per_draft(
        self, mongo_db, admin_w1, instinct_store
    ):
        prospect = await _seed_prospect(admin_w1)
        a = await _seed_draft(admin_w1, prospect["id"])
        b = await _seed_draft(
            admin_w1, prospect["id"], channel="whatsapp", subject=None, body="Hi Sam!"
        )

        with _identity("w1", admin_w1):
            result = _payload(
                await growth_mcp._propose_send_batch_handler({"draft_ids": [a["id"], b["id"]]})
            )
            # A second pass finds them already proposed — partial success, not a
            # second road to approval.
            again = _payload(await growth_mcp._propose_send_batch_handler({"draft_ids": [a["id"]]}))

        assert result["proposed"] == 2
        assert result["failed"] == []
        assert again["proposed"] == 0
        assert again["failed"][0]["code"] == "draft.illegal_transition"
        pending = await instinct_store.pending()
        assert len(pending) == 2

    @pytest.mark.asyncio
    async def test_list_drafts_previews_bodies_and_filters(self, mongo_db, admin_w1):
        prospect = await _seed_prospect(admin_w1)
        await _seed_draft(admin_w1, prospect["id"], body="x" * 400)
        await _seed_draft(
            admin_w1, prospect["id"], channel="linkedin", subject=None, body="Connect note."
        )

        with _identity("w1", admin_w1):
            everything = _payload(await growth_mcp._list_drafts_handler({}))
            linkedin = _payload(await growth_mcp._list_drafts_handler({"channel": "linkedin"}))

        assert everything["showing"] == 2
        long_row = next(r for r in everything["drafts"] if r["channel"] == "email")
        assert "body" not in long_row
        assert len(long_row["body_preview"]) <= 120
        assert linkedin["showing"] == 1

    @pytest.mark.asyncio
    async def test_linkedin_queue_carries_the_prospect_context(
        self, mongo_db, admin_w1, instinct_store
    ):
        prospect = await _seed_prospect(admin_w1, linkedin_url="https://linkedin.com/in/sam")
        draft = await _seed_draft(
            admin_w1, prospect["id"], channel="linkedin", subject=None, body="Connect note."
        )
        with _identity("w1", admin_w1):
            await growth_mcp._propose_send_handler({"draft_id": draft["id"]})
            queue = _payload(await growth_mcp._linkedin_queue_handler({}))

        assert queue["count"] == 1
        row = queue["queue"][0]
        assert row["prospect_company"] == "Acme Dental"
        assert row["linkedin_url"] == "https://linkedin.com/in/sam"
        assert row["draft"]["body"] == "Connect note."
        assert "MANUAL" in row.get("note", "") or "MANUAL" in queue["note"]

    @pytest.mark.asyncio
    async def test_list_reports_the_filter_scoped_total_not_the_page(self, mongo_db, admin_w1):
        for i in range(7):
            await _seed_prospect(admin_w1, domain=f"clinic-{i}.com", company=f"Clinic {i}")

        with _identity("w1", admin_w1):
            page = _payload(await growth_mcp._list_prospects_handler({"limit": 3}))
            rest = _payload(
                await growth_mcp._list_prospects_handler(
                    {"limit": 3, "cursor": page["next_cursor"]}
                )
            )

        assert page["showing"] == 3
        assert page["total"] == 7
        assert page["next_cursor"]
        assert rest["showing"] == 3
        assert {r["id"] for r in page["prospects"]} & {r["id"] for r in rest["prospects"]} == set()

    @pytest.mark.asyncio
    async def test_an_absurd_limit_is_clamped_not_honoured(self, mongo_db, admin_w1):
        """The agent reads to decide, not to recite — a 5,000-row ask is capped
        rather than dumped into the turn."""
        for i in range(3):
            await _seed_prospect(admin_w1, domain=f"clinic-{i}.com")
        with _identity("w1", admin_w1):
            page = _payload(await growth_mcp._list_prospects_handler({"limit": 5000}))
        assert page["showing"] == 3


# ---------------------------------------------------------------------------
# Tenancy + identity
# ---------------------------------------------------------------------------


class TestTenancy:
    @pytest.mark.asyncio
    async def test_another_workspace_sees_none_of_it(self, mongo_db, admin_w1, admin_w2):
        prospect = await _seed_prospect(admin_w1)
        await _seed_draft(admin_w1, prospect["id"])

        with _identity("w2", admin_w2):
            listed = _payload(await growth_mcp._list_prospects_handler({}))
            drafts = _payload(await growth_mcp._list_drafts_handler({}))
            fetched = await growth_mcp._get_prospect_handler({"prospect_id": prospect["id"]})

        assert listed["total"] == 0
        assert listed["prospects"] == []
        assert drafts["drafts"] == []
        # Identical 404 wording — existence never leaks across tenants.
        assert "prospect.not_found" in _error_text(fetched)

    @pytest.mark.asyncio
    async def test_another_workspace_cannot_touch_the_draft(
        self, mongo_db, admin_w1, admin_w2, instinct_store
    ):
        prospect = await _seed_prospect(admin_w1)
        draft = await _seed_draft(admin_w1, prospect["id"])

        with _identity("w2", admin_w2):
            edit = await growth_mcp._update_draft_handler(
                {"draft_id": draft["id"], "body": "tenant leak"}
            )
            propose = await growth_mcp._propose_send_handler({"draft_id": draft["id"]})
            batch = _payload(
                await growth_mcp._propose_send_batch_handler({"draft_ids": [draft["id"]]})
            )

        assert "draft.not_found" in _error_text(edit)
        assert "draft.not_found" in _error_text(propose)
        assert batch["proposed"] == 0
        assert batch["failed"][0]["code"] == "draft.not_found"

        with _identity("w1", admin_w1):
            fetched = _payload(
                await growth_mcp._get_prospect_handler({"prospect_id": prospect["id"]})
            )
        assert fetched["drafts"][0]["body"] == draft["body"]
        assert fetched["drafts"][0]["status"] == "draft"

    @pytest.mark.asyncio
    async def test_a_workspace_id_argument_is_ignored(self, mongo_db, admin_w1, admin_w2):
        """Belt and braces on top of the schema's ``additionalProperties:
        false``: even if a stray ``workspace_id`` reached a handler, the
        workspace still comes from the identity."""
        await _seed_prospect(admin_w1)
        with _identity("w2", admin_w2):
            listed = _payload(await growth_mcp._list_prospects_handler({"workspace_id": "w1"}))
        assert listed["total"] == 0

    @pytest.mark.asyncio
    async def test_outside_a_chat_stream_every_tool_errors(self, mongo_db):
        """No identity ContextVars → an explicit error, never a blank-workspace
        read."""
        handlers = [
            growth_mcp._list_prospects_handler,
            growth_mcp._get_prospect_handler,
            growth_mcp._list_drafts_handler,
            growth_mcp._upsert_prospect_handler,
            growth_mcp._create_draft_handler,
            growth_mcp._update_draft_handler,
            growth_mcp._propose_send_handler,
            growth_mcp._propose_send_batch_handler,
            growth_mcp._linkedin_queue_handler,
        ]
        for handler in handlers:
            response = await handler({})
            assert "requires workspace and user context" in _error_text(response)

    @pytest.mark.asyncio
    async def test_an_unresolvable_user_fails_closed(self, mongo_db):
        with _identity("w1", "not-an-object-id"):
            response = await growth_mcp._list_prospects_handler({})
        assert "could not resolve the calling user" in _error_text(response)


# ---------------------------------------------------------------------------
# RBAC — the same tiers the HTTP routes carry
# ---------------------------------------------------------------------------


class TestRbac:
    @pytest.mark.asyncio
    async def test_a_member_reads_and_writes_but_cannot_propose(
        self, mongo_db, member_w1, instinct_store
    ):
        """``growth.manage`` is ADMIN. It is not decoration: the executor
        re-checks it against the proposer's CURRENT role at approve time, so a
        member-filed proposal could only ever clog the Tray."""
        prospect = await _seed_prospect(member_w1)
        draft = await _seed_draft(member_w1, prospect["id"])

        with _identity("w1", member_w1):
            read = _payload(await growth_mcp._list_prospects_handler({}))
            single = _payload(await growth_mcp._propose_send_handler({"draft_id": draft["id"]}))
            batch = _payload(
                await growth_mcp._propose_send_batch_handler({"draft_ids": [draft["id"]]})
            )

        assert read["total"] == 1
        for denial in (single, batch):
            assert denial["ok"] is False
            assert denial["denied"] is True
            assert denial["code"]

        # And nothing was proposed.
        assert await instinct_store.pending() == []
        with _identity("w1", member_w1):
            drafts = _payload(await growth_mcp._list_drafts_handler({}))
        assert drafts["drafts"][0]["status"] == "draft"


# ---------------------------------------------------------------------------
# Input validation — a malformed call explains itself
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_required_ids_explain_themselves(self, mongo_db, admin_w1):
        with _identity("w1", admin_w1):
            assert "prospect_id" in _error_text(await growth_mcp._get_prospect_handler({}))
            assert "prospect_id" in _error_text(await growth_mcp._create_draft_handler({}))
            assert "draft_id" in _error_text(await growth_mcp._update_draft_handler({}))
            assert "draft_id" in _error_text(await growth_mcp._propose_send_handler({}))
            assert "draft_ids" in _error_text(
                await growth_mcp._propose_send_batch_handler({"draft_ids": []})
            )
            assert "domain" in _error_text(await growth_mcp._upsert_prospect_handler({}))

    @pytest.mark.asyncio
    async def test_a_subject_on_a_non_email_channel_is_refused(self, mongo_db, admin_w1):
        prospect = await _seed_prospect(admin_w1)
        with _identity("w1", admin_w1):
            response = await growth_mcp._create_draft_handler(
                {
                    "prospect_id": prospect["id"],
                    "channel": "whatsapp",
                    "subject": "not allowed here",
                    "body": "Hi Sam!",
                }
            )
        assert response.get("is_error")

    @pytest.mark.asyncio
    async def test_an_empty_body_is_refused(self, mongo_db, admin_w1):
        prospect = await _seed_prospect(admin_w1)
        with _identity("w1", admin_w1):
            response = await growth_mcp._create_draft_handler(
                {"prospect_id": prospect["id"], "channel": "email", "body": "   "}
            )
        assert response.get("is_error")
