# tests/cloud/test_phaseb_e2e.py
# Created: 2026-06-08 — VIP Onboarding Phase B, FINAL chunk: cross-cutting
#   e2e / integration tests over the ASSEMBLED Phase B backend.
#
# Each Phase B piece is unit-tested in isolation (member_ingest, member_day_
# digest, the chat-path gate, the REST kb scope validator, purge). This suite
# proves the WHOLE CHAIN holds together — and, above all, that no member's data
# leaks across ANY door when every door is wired up at once.
#
# What's REAL vs MOCKED (the task's "real assembled modules" rule)
# ----------------------------------------------------------------
# REAL (exercised, not faked): member_ingest.service.ingest_member,
# member_ingest.purge.purge_member_data, member_day_digest.service.
# member_day_digest, member_day_digest.router.get_member_day_digest,
# chat.agent_service._kb_scopes_for_context / _member_briefing_block /
# build_knowledge_context, kb.service.validate_scope_override, kb.router's
# search/ingest/lint endpoints.
# MOCKED (the external boundary ONLY): the per-user Gmail/Calendar reads
# (injected fake readers — no OAuth, no network) and the kb-go subprocess (a
# single in-memory FakeKbStore that the ingest ACCEPT path writes to, the
# chat KB-search + REST router read from, and purge CLEARS — so the kb scope
# store is genuinely shared across every door, just without a binary).
#
# The three acts
# --------------
#   1. HAPPY PATH — ingest member A's mail/calendar into A's user:{A} scope,
#      then prove A's OWN briefing AND A's OWN digest surface A's data, and a
#      KB search in A's solo session finds the ingested text.
#   2. LEAK-ACROSS-ALL-DOORS (the centerpiece) — member B gets NONE of A's
#      data, exercised across EVERY door together: (a) the chat gate, (b) the
#      REST kb router (403), (c) the digest API (B's own, never A's), (d) a
#      shared/multi-member room (no member-private scope for anyone).
#   3. PURGE — after purge_member_data for A, A's data is gone from all paths:
#      the user:{A} KB scope is cleared, and A's briefing + digest go empty.

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.context import RequestContext  # noqa: E402
from pocketpaw_ee.cloud._core.context import ScopeKind as CtxScopeKind
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    ScopeContext,
    ScopeKind,
    _kb_scopes_for_context,
    _member_briefing_block,
    build_knowledge_context,
)
from pocketpaw_ee.cloud.kb import router as kb_router  # noqa: E402
from pocketpaw_ee.cloud.kb import service as kb_service  # noqa: E402
from pocketpaw_ee.cloud.kb.dto import IngestTextRequest, SearchRequest  # noqa: E402
from pocketpaw_ee.cloud.member_day_digest import router as digest_router  # noqa: E402
from pocketpaw_ee.cloud.member_day_digest.dto import MemberDayDigest  # noqa: E402
from pocketpaw_ee.cloud.member_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.member_ingest.purge import purge_member_data  # noqa: E402
from pocketpaw_ee.cloud.shared.errors import Forbidden  # noqa: E402

pytestmark = pytest.mark.asyncio

# The two members exercised across every door. Opaque cloud ids (as the real
# system uses), so they double as kb-go scope ids verbatim.
WS = "w1"
ALICE = "alice-objid"
BOB = "bob-objid"

# A signature that text from Alice's mail/calendar carries, so the leak tests
# can scan any surface for "did Alice's content escape into Bob's view?".
ALICE_SECRET = "PROJECT-NIGHTINGALE-merger"


# ===========================================================================
# The shared in-memory kb-go fake — the ONE mocked seam for the scope store.
#
# Every door that touches kb-go in the assembled chain funnels through this:
#   * ingest ACCEPT  -> store.accept(scope, articles)   (via injected kb_accept)
#   * chat KB search -> store.kb("search", ..., "--scope", S, "--context")
#   * REST kb router -> store.kb("search"/"ingest"/"lint", "--scope", S, ...)
#   * scope-probe    -> store.kb("list", "--scope", S)
#   * purge CLEAR    -> store.clear(scope)              (via injected kb_clear)
# Because it's a single dict keyed on the literal scope string, a cross-scope
# read can only ever return what was written under THAT scope — exactly the
# property the leak tests assert against.
# ===========================================================================


class FakeKbStore:
    """An in-memory stand-in for the kb-go binary, keyed on scope string."""

    def __init__(self) -> None:
        # scope -> list of article dicts ({title, content, summary, ...}).
        self.scopes: dict[str, list[dict[str, Any]]] = {}

    # -- write paths -------------------------------------------------------

    async def accept(self, scope: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """The keyless ``kb accept`` seam the ingest worker writes through."""
        self.scopes.setdefault(scope, []).extend(dict(a) for a in articles)
        return {"accepted": len(articles)}

    async def clear(self, scope: str) -> dict[str, Any]:
        """The keyless ``kb clear`` seam the purge path clears through."""
        removed = len(self.scopes.pop(scope, []))
        return {"cleared": removed}

    # -- the _kb subprocess seam (sync, called as _kb(*args, input_text=)) --

    def kb(self, *args: str, input_text: str | None = None, timeout: int = 120) -> Any:
        """Mimics ``agents.knowledge._kb`` over the in-memory store.

        Honours the ``--scope`` flag (every door passes it) and dispatches on
        the verb. ``search``/``list`` return rows from THAT scope only; the
        ``--context`` form returns the formatted string the chat path expects.
        ``ingest`` writes a row to the scope (the REST write door).
        """
        if not args:
            return []
        verb = args[0]
        scope = _flag(args, "--scope")
        rows = self.scopes.get(scope, [])

        if verb == "search":
            query = args[1] if len(args) > 1 else ""
            hits = [r for r in rows if _matches(r, query)] or rows
            if "--context" in args:
                # The chat path consumes a single formatted string.
                if not hits:
                    return ""
                return "\n\n".join(
                    f"{h.get('title', '')}\n{h.get('content', '')}".strip() for h in hits
                )
            return [
                {"title": h.get("title", ""), "summary": h.get("summary", h.get("content", ""))}
                for h in hits
            ]
        if verb == "list":
            return [{"title": r.get("title", "")} for r in rows]
        if verb == "ingest":
            source = _flag(args, "--source") or "manual"
            self.scopes.setdefault(scope, []).append(
                {"title": source, "content": input_text or "", "summary": (input_text or "")[:200]}
            )
            return {"ingested": 1, "scope": scope}
        if verb == "lint":
            return []
        if verb == "stats":
            return {"articles": len(rows)}
        return []


def _flag(args: tuple[str, ...] | list[str], name: str) -> str:
    """Return the value following ``name`` in an argv-style list, or ''."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return ""


def _matches(row: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    hay = f"{row.get('title', '')} {row.get('content', '')} {row.get('summary', '')}".lower()
    return any(tok in hay for tok in query.lower().split())


# ===========================================================================
# Fake per-user Gmail/Calendar readers — the EXTERNAL boundary (no OAuth).
#
# Keyed by member so each member resolves THEIR OWN data, mirroring how the
# real per-user clients resolve per-member OAuth-token buckets. The leak tests
# rely on this: a reader built for Bob can only ever return Bob's data.
# ===========================================================================


class FakeGmailReader:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self._messages = messages or []
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        self.queries.append(query)
        return list(self._messages[:max_results])


class FakeCalendarReader:
    def __init__(self, events: list[dict] | None = None) -> None:
        self._events = events or []

    async def list_events(self, time_min=None, time_max=None, max_results=10, **_kw):
        return list(self._events[:max_results])


def _alice_mail() -> list[dict]:
    return [
        {
            "id": "m-alice-1",
            "subject": f"Re: {ALICE_SECRET}",
            "from": "ceo@acme.test",
            "date": "Wed, 04 Jun 2026 10:00:00 +0000",
            "snippet": f"Confidential: {ALICE_SECRET} closes Friday.",
        }
    ]


def _alice_events() -> list[dict]:
    return [
        {
            "id": "e-alice-1",
            "summary": f"{ALICE_SECRET} sync",
            "start": "2026-06-10T09:00:00Z",
            "end": "2026-06-10T10:00:00Z",
            "location": "War Room",
            "description": f"Final terms for {ALICE_SECRET}.",
            "attendees": ["ceo@acme.test"],
        }
    ]


def _bob_mail() -> list[dict]:
    return [
        {
            "id": "m-bob-1",
            "subject": "Standup notes",
            "from": "teammate@acme.test",
            "date": "Wed, 04 Jun 2026 11:00:00 +0000",
            "snippet": "Bob's own mundane standup notes.",
        }
    ]


def _bob_events() -> list[dict]:
    return [
        {
            "id": "e-bob-1",
            "summary": "Bob 1:1",
            "start": "2026-06-11T15:00:00Z",
            "end": "2026-06-11T15:30:00Z",
            "location": "",
            "description": "Bob's own 1:1.",
            "attendees": [],
        }
    ]


# Per-member reader registries so a digest/ingest for member X structurally
# only ever sees X's data — the in-test analogue of per-user OAuth buckets.
_GMAIL = {ALICE: _alice_mail(), BOB: _bob_mail()}
_CAL = {ALICE: _alice_events(), BOB: _bob_events()}


def _readers_for(member_id: str) -> tuple[FakeGmailReader, FakeCalendarReader]:
    return FakeGmailReader(_GMAIL.get(member_id, [])), FakeCalendarReader(_CAL.get(member_id, []))


def _digest_fn(store_unused: Any = None):
    """A digest entry point that reads the per-member fake readers LIVE.

    This is the real ``member_day_digest`` service with its external Gmail/
    Calendar boundary replaced — it is NOT a KB read (the digest is a live
    pull by design), so a member's digest is keyed purely on member_id.
    """

    async def _fn(workspace_id: str, member_id: str) -> MemberDayDigest:
        gmail, cal = _readers_for(member_id)
        from pocketpaw_ee.cloud.member_day_digest.service import member_day_digest

        return await member_day_digest(
            workspace_id, member_id, gmail_reader=gmail, calendar_reader=cal
        )

    return _fn


# ===========================================================================
# RequestContext / ScopeContext builders (mirror the real resolvers).
# ===========================================================================


def _req_ctx(user_id: str, workspace_id: str | None = WS) -> RequestContext:
    """An authed RequestContext as ``request_context`` would build it — the
    shape the digest REST router consumes."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req-e2e",
        scope=CtxScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _solo_ctx(member_id: str) -> ScopeContext:
    """A member's OWN solo session — ``members == [member_id]`` (the shape
    ``_resolve_session`` always builds for a single-user session)."""
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id=f"s-{member_id}",
        workspace_id=WS,
        user_id=member_id,
        members=[member_id],
        target_agent_id="a1",
    )


def _shared_room_ctx(caller: str, members: list[str]) -> ScopeContext:
    """A shared/multi-member room (group)."""
    return ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g-shared",
        workspace_id=WS,
        user_id=caller,
        members=list(members),
        target_agent_id="a-shared",
    )


def _patch_kb(store: FakeKbStore):
    """Point the chat-path KB search, the REST kb router, and the scope-probe
    at the shared in-memory store.

    All three resolve the kb-go subprocess through ``agents.knowledge._kb``
    (the chat path and the scope-probe import it lazily inside their bodies;
    the router binds it at module import), so patching it there is the single
    seam that makes the kb store shared and binary-free across every door.
    """
    return patch("pocketpaw_ee.cloud.agents.knowledge._kb", store.kb)


def _patch_router_kb(store: FakeKbStore):
    """The REST kb router binds ``_kb`` at import time, so it needs its own
    patch target (the module-level name in kb.router)."""
    return patch.object(kb_router, "_kb", store.kb)


async def _seed_ingest(store: FakeKbStore, member_id: str) -> dict:
    """Run the REAL ingest worker for one member against the shared store and
    the member's fake readers — the upstream of the whole chain."""
    gmail, cal = _readers_for(member_id)
    return await ingest_service.ingest_member(
        workspace_id=WS,
        member_id=member_id,
        gmail_reader=gmail,
        calendar_reader=cal,
        kb_accept=store.accept,
    )


# ===========================================================================
# ACT 1 — HAPPY PATH. Ingest A → A's briefing, A's digest, A's KB search all
# surface A's data.
# ===========================================================================


async def test_happy_ingest_lands_in_alice_scope(mongo_db):  # noqa: ARG001 — Beanie init
    """The assembled upstream: ingest writes A's mail+calendar into user:{A}
    and ONLY user:{A}, via the keyless accept seam."""
    store = FakeKbStore()
    result = await _seed_ingest(store, ALICE)

    assert result["status"] == "ok"
    assert result["scope"] == f"user:{ALICE}"
    # The store now carries Alice's data under exactly her scope.
    assert set(store.scopes) == {f"user:{ALICE}"}
    arts = store.scopes[f"user:{ALICE}"]
    assert len(arts) == 2  # 1 mail + 1 event
    assert any(ALICE_SECRET in a["content"] for a in arts)


async def test_happy_alice_briefing_surfaces_her_day(mongo_db):  # noqa: ARG001
    """A's OWN solo session: the proactive <your-day> briefing carries her
    real calendar + mail (the live digest pull, keyed on her id)."""
    block = await _member_briefing_block(_solo_ctx(ALICE), digest_fn=_digest_fn())

    assert block
    assert "<your-day>" in block
    assert f"{ALICE_SECRET} sync" in block  # her calendar event
    assert "Unread mail:" in block


async def test_happy_alice_digest_api_returns_her_data(mongo_db):  # noqa: ARG001
    """A's OWN digest via the REST door: events + unread mail, keyed on the
    authenticated principal."""
    with patch.object(digest_router, "member_day_digest", _digest_fn()):
        digest = await digest_router.get_member_day_digest(ctx=_req_ctx(ALICE))

    assert isinstance(digest, MemberDayDigest)
    assert digest.member_id == ALICE
    assert digest.empty is False
    assert any(ALICE_SECRET in e.summary for e in digest.events)
    assert digest.unread_mail_count == 1


async def test_happy_alice_kb_search_finds_ingested_text(mongo_db):  # noqa: ARG001
    """The full chain end-to-end: after ingest, a KB search in A's OWN solo
    session retrieves the ingested mail/calendar text. Proves the gate ADMITS
    user:{A} for A and the search reads the scope ingest wrote."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)

    # The gate emits user:{A} at the head for A's solo session.
    scopes = _kb_scopes_for_context(_solo_ctx(ALICE))
    assert scopes[0] == f"user:{ALICE}"

    with _patch_kb(store):
        kb_block = await build_knowledge_context(
            _solo_ctx(ALICE),
            user_message=ALICE_SECRET,
        )
    # The ingested confidential text is retrievable in A's own session.
    assert ALICE_SECRET in kb_block
    assert f"### user:{ALICE}" in kb_block


# ===========================================================================
# ACT 2 — LEAK-ACROSS-ALL-DOORS (THE CENTERPIECE).
#
# Member A has ingested + has a busy day. Member B gets NONE of it, across
# EVERY door exercised together in one suite.
# ===========================================================================


async def test_leak_door_a_chat_gate_no_alice_scope_for_bob(mongo_db):  # noqa: ARG001
    """DOOR (a) — the chat KB-scope gate. B's solo session yields user:{B}
    and NEVER user:{A}; the gate keys on members==[user_id] exactly."""
    scopes_b = _kb_scopes_for_context(_solo_ctx(BOB))
    assert f"user:{BOB}" in scopes_b
    assert f"user:{ALICE}" not in scopes_b
    assert not any(s.startswith(f"user:{ALICE}") for s in scopes_b)


async def test_leak_door_a_chat_search_returns_only_bobs_scope(mongo_db):  # noqa: ARG001
    """DOOR (a), deeper — even with both members' data in the SAME store, a
    KB search in B's session can only read user:{B}; A's secret never appears
    because the gate never hands B the user:{A} scope to search."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)  # Alice's secret is in the store...
    await _seed_ingest(store, BOB)  # ...alongside Bob's own mundane data.

    with _patch_kb(store):
        kb_block = await build_knowledge_context(
            _solo_ctx(BOB),
            user_message=ALICE_SECRET,  # Bob even SEARCHES for Alice's secret
        )
    # ...and still gets nothing of Alice's: his scope doesn't contain it.
    assert ALICE_SECRET not in kb_block
    assert f"### user:{ALICE}" not in kb_block


async def test_leak_door_b_rest_kb_search_foreign_user_scope_403(mongo_db):  # noqa: ARG001
    """DOOR (b) — the REST kb router. B POSTs /api/v1/kb/search with
    {"scope":"user:{A}"} → Forbidden, and kb-go is NEVER reached."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)  # Alice's private KB exists...

    # B's allowlist: B's own workspace only (no visible pockets/agents here).
    with (
        patch.object(
            kb_service,
            "_candidate_scopes",
            _async_return([f"workspace:{WS}"]),
        ),
        _patch_router_kb(store),
    ):
        with pytest.raises(Forbidden) as exc:
            await kb_router.search_kb(
                SearchRequest(query=ALICE_SECRET, scope=f"user:{ALICE}"),
                workspace_id=WS,
                user_id=BOB,
            )
    assert exc.value.code == "kb.scope_forbidden"
    # Belt: the store was never touched — denial happens before kb-go.
    assert store.scopes.get(f"user:{ALICE}")  # Alice's data still intact
    # And nothing of Alice's was returned to Bob (no result object at all).


async def test_leak_door_b_rest_kb_ingest_poison_foreign_scope_403(mongo_db):  # noqa: ARG001
    """DOOR (b), WRITE side — B cannot POISON A's private KB via the REST
    ingest door either. The foreign user: scope is hard-denied before write."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)
    alice_before = list(store.scopes[f"user:{ALICE}"])

    with (
        patch.object(kb_service, "_candidate_scopes", _async_return([f"workspace:{WS}"])),
        _patch_router_kb(store),
    ):
        with pytest.raises(Forbidden) as exc:
            await kb_router.ingest_text(
                IngestTextRequest(text="malware", source="evil", scope=f"user:{ALICE}"),
                workspace_id=WS,
                user_id=BOB,
            )
    assert exc.value.code == "kb.scope_forbidden"
    # Alice's scope is byte-identical — Bob's poison never landed.
    assert store.scopes[f"user:{ALICE}"] == alice_before


async def test_leak_door_c_digest_api_gives_bob_his_own_never_alices(mongo_db):  # noqa: ARG001
    """DOOR (c) — the digest REST API. B's GET returns B's OWN digest (his
    mundane data), and structurally never A's: the router has no member_id
    param, so member_id == ctx.user_id always."""
    with patch.object(digest_router, "member_day_digest", _digest_fn()):
        digest_b = await digest_router.get_member_day_digest(ctx=_req_ctx(BOB))

    assert digest_b.member_id == BOB
    # Bob sees his own day...
    assert any("Bob" in e.summary for e in digest_b.events)
    # ...and NONE of Alice's confidential content leaks into it.
    blob = digest_b.model_dump_json()
    assert ALICE_SECRET not in blob
    assert f"user:{ALICE}" not in blob


async def test_leak_door_c_digest_endpoint_has_no_member_id_param(mongo_db):  # noqa: ARG001
    """DOOR (c), structural — the endpoint exposes NO member-id / override
    parameter, so B cannot even ASK for A's digest. The contract is on the
    signature itself, asserted alongside the runtime leak test above."""
    params = set(inspect.signature(digest_router.get_member_day_digest).parameters)
    forbidden = {"member_id", "user_id", "member", "for_member", "principal", "target"}
    assert params.isdisjoint(forbidden), f"endpoint leaks an id override: {params & forbidden}"


async def test_leak_door_d_shared_room_no_member_private_scope(mongo_db):  # noqa: ARG001
    """DOOR (d) — a shared/multi-member room. NO member-private user: scope is
    emitted for ANYONE (not the caller's, not any participant's), and the
    briefing is suppressed too — one member's mail/calendar never bleeds into
    a context another member can see."""
    members = [ALICE, BOB]
    ctx = _shared_room_ctx(caller=ALICE, members=members)

    # The scope gate: no user: tier at all in a shared room.
    scopes = _kb_scopes_for_context(ctx)
    assert not any(s.startswith("user:") for s in scopes), scopes
    assert f"workspace:{WS}" in scopes  # the shared workspace scope survives

    # The briefing: suppressed entirely, even though Alice IS the caller and
    # HAS a rich day — and the digest is never even pulled.
    pulled: list[str] = []

    async def _spy(workspace_id: str, member_id: str) -> MemberDayDigest:
        pulled.append(member_id)
        return await _digest_fn()(workspace_id, member_id)

    block = await _member_briefing_block(ctx, digest_fn=_spy)
    assert block == ""
    assert pulled == []  # no private mail/calendar pull in a shared room


async def test_leak_all_doors_one_pass(mongo_db):  # noqa: ARG001
    """THE ASSEMBLED LEAK GUARANTEE, all doors in a single pass.

    With Alice ingested + a busy day, walk every Phase B door AS BOB and prove
    none of them yields a byte of Alice's data:
        (a) chat gate          -> no user:{A} scope, search returns nothing
        (b) REST kb router      -> 403 on user:{A}, store untouched
        (c) digest API          -> Bob's own digest, no ALICE_SECRET
        (d) shared room         -> no user: scope for anyone
    """
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)
    await _seed_ingest(store, BOB)

    # (a) chat gate + search as Bob
    with _patch_kb(store):
        bob_kb = await build_knowledge_context(_solo_ctx(BOB), user_message=ALICE_SECRET)
    assert ALICE_SECRET not in bob_kb

    # (b) REST kb door as Bob -> 403, Alice's data intact
    alice_before = list(store.scopes[f"user:{ALICE}"])
    with (
        patch.object(kb_service, "_candidate_scopes", _async_return([f"workspace:{WS}"])),
        _patch_router_kb(store),
    ):
        with pytest.raises(Forbidden):
            await kb_router.search_kb(
                SearchRequest(query=ALICE_SECRET, scope=f"user:{ALICE}"),
                workspace_id=WS,
                user_id=BOB,
            )
    assert store.scopes[f"user:{ALICE}"] == alice_before

    # (c) digest API as Bob -> his own, never Alice's
    with patch.object(digest_router, "member_day_digest", _digest_fn()):
        bob_digest = await digest_router.get_member_day_digest(ctx=_req_ctx(BOB))
    assert bob_digest.member_id == BOB
    assert ALICE_SECRET not in bob_digest.model_dump_json()

    # (d) shared room -> no member-private scope for anyone
    shared = _kb_scopes_for_context(_shared_room_ctx(caller=BOB, members=[ALICE, BOB]))
    assert not any(s.startswith("user:") for s in shared)


# ===========================================================================
# ACT 3 — PURGE. After purge_member_data for A, A's data is gone everywhere.
# ===========================================================================


async def test_purge_clears_alice_scope_and_emits(mongo_db, recording_bus):  # noqa: ARG001
    """The REAL purge path against the shared store: A's user:{A} KB scope is
    cleared and a MemberDataPurged event is emitted."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)
    assert store.scopes.get(f"user:{ALICE}")  # present before

    # A no-op token store so the OAuth-token deletes don't need an on-disk one.
    class _NoTokens:
        def delete(self, service: str, member_id: str) -> bool:  # noqa: ARG002
            return False

    result = await purge_member_data(
        workspace_id=WS,
        member_id=ALICE,
        kb_clear=store.clear,
        token_store=_NoTokens(),
    )

    assert result["status"] == "ok"
    assert result["scope"] == f"user:{ALICE}"
    assert result["kb_cleared"] is True
    # The scope is gone from the store entirely.
    assert f"user:{ALICE}" not in store.scopes
    # A purge event was emitted for downstream consumers.
    assert any(type(e).__name__ == "MemberDataPurged" for e in recording_bus.events)


async def test_purge_then_kb_search_finds_nothing(mongo_db):  # noqa: ARG001
    """After purge, a KB search in A's OWN solo session finds nothing — the
    scope the gate admits is now empty. The chain's read door reflects the
    purge with no extra wiring."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)

    class _NoTokens:
        def delete(self, *_a, **_k) -> bool:
            return False

    await purge_member_data(
        workspace_id=WS, member_id=ALICE, kb_clear=store.clear, token_store=_NoTokens()
    )

    with _patch_kb(store):
        kb_block = await build_knowledge_context(_solo_ctx(ALICE), user_message=ALICE_SECRET)
    assert ALICE_SECRET not in kb_block


async def test_purge_then_briefing_and_digest_empty(mongo_db):  # noqa: ARG001
    """After purge AND disconnect, A's briefing + digest go empty.

    The digest is a LIVE pull, so emptiness comes from the disconnected
    accounts (both per-user reads raise "not authenticated"), not from the KB
    clear — this models the real offboard: purge wipes the KB, account
    disconnection empties the live digest. Together: no block, empty digest."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)

    class _NoTokens:
        def delete(self, *_a, **_k) -> bool:
            return False

    await purge_member_data(
        workspace_id=WS, member_id=ALICE, kb_clear=store.clear, token_store=_NoTokens()
    )

    # Post-disconnect digest: both sources raise "not authenticated".
    class _Disconnected:
        async def search(self, *_a, **_k):
            raise RuntimeError("Gmail not authenticated. Complete OAuth flow first")

        async def list_events(self, *_a, **_k):
            raise RuntimeError("Google Calendar not authenticated")

    async def _disconnected_digest(workspace_id: str, member_id: str) -> MemberDayDigest:
        from pocketpaw_ee.cloud.member_day_digest.service import member_day_digest

        return await member_day_digest(
            workspace_id,
            member_id,
            gmail_reader=_Disconnected(),
            calendar_reader=_Disconnected(),
        )

    # Briefing: empty (gated solo session, but the digest is empty → no block).
    block = await _member_briefing_block(_solo_ctx(ALICE), digest_fn=_disconnected_digest)
    assert block == ""

    # Digest API: empty digest, never an error.
    with patch.object(digest_router, "member_day_digest", _disconnected_digest):
        digest = await digest_router.get_member_day_digest(ctx=_req_ctx(ALICE))
    assert digest.empty is True
    assert digest.events == []
    assert digest.unread_mail_count == 0


async def test_purge_is_isolated_to_alice_bob_untouched(mongo_db):  # noqa: ARG001
    """Purging A leaves B's scope fully intact — the purge target is a pure
    function of A's opaque id, so it can never reach B's data."""
    store = FakeKbStore()
    await _seed_ingest(store, ALICE)
    await _seed_ingest(store, BOB)
    bob_before = list(store.scopes[f"user:{BOB}"])

    class _NoTokens:
        def delete(self, *_a, **_k) -> bool:
            return False

    await purge_member_data(
        workspace_id=WS, member_id=ALICE, kb_clear=store.clear, token_store=_NoTokens()
    )

    assert f"user:{ALICE}" not in store.scopes  # Alice gone
    assert store.scopes[f"user:{BOB}"] == bob_before  # Bob untouched


# ===========================================================================
# Helpers
# ===========================================================================


def _async_return(value: Any):
    """A coroutine function that returns ``value`` — for patching the async
    ``_candidate_scopes`` with a fixed allowlist."""

    async def _fn(*_a: Any, **_k: Any) -> Any:
        return value

    return _fn
