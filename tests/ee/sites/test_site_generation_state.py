# tests/ee/sites/test_site_generation_state.py — the server-side "is this site still
# being generated" signal (``Site.generation``) as a client reads it.
#
# The bug: the agent creates a site with a create tool, then keeps working in the same
# run for minutes. A reload during that window had no server-side way to know the site
# was mid-build, so the builder showed the bare template. These tests pin the contract:
# only the create tools stamp ``generating`` (with the chat session), the read paths
# self-heal it to ``waiting`` / ``ready`` once the creating run is over, run
# finalization settles it too, a published site never reports it, and the list read
# costs nothing extra unless a row is actually generating.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.message import Message, MessageAskUser
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import generation as site_generation
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import SiteResponse, SiteStatusResponse

WS = "ws1"
SESSION = "sess-1"


@pytest.fixture(autouse=True)
def _no_pocket_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _empty(_ws: str, _ids: list[str]) -> dict[str, str]:
        return {}

    monkeypatch.setattr(pockets_service, "patterns_for_pockets", _empty)
    monkeypatch.setattr(pockets_service, "engines_for_pockets", _empty)


async def _site(pocket_id: str = "pk1", **fields: Any) -> Site:
    doc = Site(
        id=sites_service._live_object_id(WS, pocket_id),
        workspace=WS,
        pocket_id=pocket_id,
        owner="u1",
        name="S",
        **fields,
    )
    await doc.insert()
    return doc


async def _run(
    status: str,
    *,
    run_id: str = "run-1",
    assistant_message_id: str | None = None,
    age_s: int = 0,
):
    doc = ChatRunDoc(
        run_id=run_id,
        workspace=WS,
        context_type="session",
        scope_id=SESSION,
        session_key=f"cloud:session:{SESSION}:agent-1",
        user_id="u1",
        agent_id="agent-1",
        client_message_id=f"cm-{run_id}",
        user_message_id=f"um-{run_id}",
        status=status,
        assistant_message_id=assistant_message_id,
        createdAt=datetime.now(UTC) - timedelta(seconds=age_s),
    )
    await doc.insert()
    return doc


async def _reply(*, ask: bool, answered: bool = False) -> str:
    msg = Message(
        context_type="session",
        session_key=f"cloud:session:{SESSION}:agent-1",
        role="assistant",
        sender_type="agent",
        content="Which colour?",
        ask_user=MessageAskUser(id="q1", question="Which colour?", answered=answered)
        if ask
        else None,
    )
    await msg.insert()
    return str(msg.id)


async def _status(pocket_id: str = "pk1") -> SiteStatusResponse:
    return await sites_service.pocket_status(workspace_id=WS, pocket_id=pocket_id)


class TestTheWireContract:
    @pytest.mark.parametrize("model", [SiteResponse, SiteStatusResponse])
    def test_both_reads_declare_the_fields_with_none_defaults(self, model: Any) -> None:
        assert model.model_fields["generation"].default is None
        assert model.model_fields["generation_session_id"].default is None

    @pytest.mark.parametrize("model", [SiteResponse, SiteStatusResponse])
    def test_the_run_id_stays_internal(self, model: Any) -> None:
        assert "generation_run_id" not in model.model_fields


class TestTheCreateToolStampsIt:
    async def test_the_shared_mint_stamps_generating_with_the_chat_session(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create
        from pocketpaw_ee.cloud.chat import agent_service

        monkeypatch.setattr(agent_service, "current_session_mongo_id", lambda: SESSION)
        await sites_create._mint_draft_site(WS, "u1", "pk1", "Bakery")

        doc = await Site.find_one({"workspace": WS, "pocket_id": "pk1"})
        assert doc is not None
        assert doc.generation == "generating"
        assert doc.generation_session_id == SESSION

    async def test_no_chat_session_means_no_stamp(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a session there is no run to settle it, so it would stick forever."""
        from pocketpaw_ee.agent.mcp_servers import sites_create
        from pocketpaw_ee.cloud.chat import agent_service

        monkeypatch.setattr(agent_service, "current_session_mongo_id", lambda: None)
        await sites_create._mint_draft_site(WS, "u1", "pk1", "Bakery")

        doc = await Site.find_one({"workspace": WS, "pocket_id": "pk1"})
        assert doc is not None
        assert doc.generation is None

    async def test_a_session_lookup_failure_never_fails_the_create(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create
        from pocketpaw_ee.cloud.chat import agent_service

        def _boom() -> str:
            raise RuntimeError("no context")

        monkeypatch.setattr(agent_service, "current_session_mongo_id", _boom)
        await sites_create._mint_draft_site(WS, "u1", "pk1", "Bakery")

        doc = await Site.find_one({"workspace": WS, "pocket_id": "pk1"})
        assert doc is not None
        assert doc.generation is None

    async def test_other_draft_mints_never_stamp_it(self, beanie_test_db) -> None:
        """Import, remix and the pockets path call ``create_draft_site`` without the
        generation arguments; those drafts are not being generated by a run."""
        doc = await sites_service.create_draft_site(workspace_id=WS, user_id="u1", pocket_id="pk1")
        assert doc.generation is None
        assert doc.generation_session_id is None

    async def test_a_repeat_create_never_restamps_an_existing_site(self, beanie_test_db) -> None:
        await _site(deployed=True, url="https://x.test")
        doc = await sites_service.create_draft_site(
            workspace_id=WS, user_id="u1", pocket_id="pk1", generation_session_id=SESSION
        )
        assert doc.generation is None

    def test_refine_and_edit_tools_never_call_the_mint(self) -> None:
        """Only create handlers mint a draft; the edit lane must never stamp the flag."""
        import inspect

        from pocketpaw_ee.agent.mcp_servers import sites_create, sites_project

        for module in (sites_create, sites_project):
            for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction):
                if "_mint_draft_site" in inspect.getsource(fn) and name != "_mint_draft_site":
                    assert "create" in name or "start_site" in name, name


class TestTheStatusRead:
    async def test_generating_while_the_creating_run_is_active(self, beanie_test_db) -> None:
        await _site(generation="generating", generation_session_id=SESSION)
        await _run("running")

        got = await _status()
        assert got.generation == "generating"
        assert got.generation_session_id == SESSION

    async def test_ready_once_the_run_is_over(self, beanie_test_db) -> None:
        await _site(generation="generating", generation_session_id=SESSION)
        await _run("completed", assistant_message_id=await _reply(ask=False))

        got = await _status()
        assert got.generation == "ready"
        stored = await Site.find_one({"pocket_id": "pk1"})
        assert stored is not None and stored.generation == "ready"  # written back

    async def test_waiting_when_the_last_reply_holds_an_unanswered_ask(
        self, beanie_test_db
    ) -> None:
        await _site(generation="generating", generation_session_id=SESSION)
        await _run("completed", assistant_message_id=await _reply(ask=True))

        got = await _status()
        assert got.generation == "waiting"

    async def test_an_answered_ask_is_ready(self, beanie_test_db) -> None:
        await _site(generation="generating", generation_session_id=SESSION)
        await _run("completed", assistant_message_id=await _reply(ask=True, answered=True))

        assert (await _status()).generation == "ready"

    async def test_a_swept_run_never_leaves_it_stuck(self, beanie_test_db) -> None:
        await _site(generation="generating", generation_session_id=SESSION)
        await _run("interrupted")

        assert (await _status()).generation == "ready"

    async def test_waiting_goes_back_to_generating_when_a_new_run_starts(
        self, beanie_test_db
    ) -> None:
        await _site(generation="waiting", generation_session_id=SESSION)
        await _run(
            "completed", run_id="run-1", assistant_message_id=await _reply(ask=True), age_s=60
        )
        await _run("queued", run_id="run-2")

        assert (await _status()).generation == "generating"

    async def test_a_published_site_never_reports_generation(self, beanie_test_db) -> None:
        await _site(
            generation="generating",
            generation_session_id=SESSION,
            deployed=True,
            url="https://x.test",
        )
        await _run("running")

        got = await _status()
        assert got.generation is None

    async def test_a_site_without_the_field_reads_none(self, beanie_test_db) -> None:
        await _site()
        got = await _status()
        assert got.generation is None
        assert got.generation_session_id is None


class TestTheListRead:
    async def test_rows_resolve_like_the_status_read(self, beanie_test_db) -> None:
        await _site("pk1", generation="generating", generation_session_id=SESSION)
        await _site("pk2")
        await _site("pk3", generation="ready", generation_session_id="old")
        await _run("running")

        rows = {r.pocket_id: r for r in await sites_service.list_for_workspace(WS)}
        assert rows["pk1"].generation == "generating"
        assert rows["pk1"].generation_session_id == SESSION
        assert rows["pk2"].generation is None
        assert rows["pk3"].generation == "ready"

    async def test_no_extra_query_when_nothing_is_generating(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _site("pk1")
        await _site("pk2", generation="ready", generation_session_id=SESSION)
        calls: list[Any] = []

        async def _spy(*args: Any, **kwargs: Any) -> dict:
            calls.append((args, kwargs))
            return {}

        monkeypatch.setattr(site_generation, "_latest_session_runs", _spy)
        await sites_service.list_for_workspace(WS)
        assert calls == []

    async def test_generating_rows_are_batched_into_one_lookup(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _site("pk1", generation="generating", generation_session_id="s1")
        await _site("pk2", generation="waiting", generation_session_id="s2")
        calls: list[Any] = []
        real = site_generation._latest_session_runs

        async def _spy(workspace_id: str, session_ids: list[str]) -> dict:
            calls.append(sorted(session_ids))
            return await real(workspace_id, session_ids)

        monkeypatch.setattr(site_generation, "_latest_session_runs", _spy)
        await sites_service.list_for_workspace(WS)
        assert calls == [["s1", "s2"]]


class TestRunFinalizationSettlesIt:
    async def test_mark_completed_settles_the_site(self, beanie_test_db) -> None:
        from pocketpaw_ee.cloud.chat.runs import service as runs_service

        await _site(generation="generating", generation_session_id=SESSION)
        await _run("running")
        msg_id = await _reply(ask=True)

        await runs_service.mark_completed("run-1", assistant_message_id=msg_id, partial_text="hi")

        stored = await Site.find_one({"pocket_id": "pk1"})
        assert stored is not None and stored.generation == "waiting"

    async def test_mark_terminal_settles_the_site(self, beanie_test_db) -> None:
        from pocketpaw_ee.cloud.chat.runs import service as runs_service

        await _site(generation="generating", generation_session_id=SESSION)
        await _run("running")

        await runs_service.mark_terminal("run-1", status="failed", error="x")

        stored = await Site.find_one({"pocket_id": "pk1"})
        assert stored is not None and stored.generation == "ready"

    async def test_a_settle_failure_never_fails_finalization(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pocketpaw_ee.cloud.chat.runs import service as runs_service

        async def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(site_generation, "settle_for_session", _boom)
        await _run("running")
        await runs_service.mark_completed("run-1", assistant_message_id=None, partial_text="")
        doc = await ChatRunDoc.find_one({"run_id": "run-1"})
        assert doc is not None and doc.status == "completed"


class TestTheSweeperSettlesIt:
    async def test_a_swept_run_settles_the_site(self, beanie_test_db) -> None:
        from pocketpaw_ee.cloud.chat.runs import sweeper

        await _site(generation="generating", generation_session_id=SESSION)
        await _run("running", age_s=3600)

        assert await sweeper.sweep_stale_runs(older_than_minutes=10) == 1
        stored = await Site.find_one({"pocket_id": "pk1"})
        assert stored is not None and stored.generation == "ready"
