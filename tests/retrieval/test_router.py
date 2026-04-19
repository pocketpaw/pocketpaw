# test_router.py — Retrieval router + credential broker test suite.
# Updated: feat/receive-retrieval-infra (2026-04-19) — moved here from
# soul-protocol/tests/test_engine/test_retrieval.py as part of the 0.3.2
# split. Imports rewritten: concrete router/broker/adapter implementations
# now come from pocketpaw.retrieval; Protocols and exceptions come from
# soul_protocol.spec.retrieval.
# Updated: feat/retrieval-router — add tests for broker fail-closed audit
# emission (journal raising on append -> acquire propagates the error),
# explicit fire-and-forget path when no journal is attached, and a pinned
# scope-overlap policy: wildcard-grant vs specific-requester AND specific-
# grant vs wildcard-requester both match; disjoint scopes don't.
# Created: feat/retrieval-router — Workstream C1 of Org Architecture RFC (#164).
# Covers: scope filtering, parallel/first/sequential strategies, per-source
# timeout, journal event emission on dispatch, broker acquire/use/expire
# flow + scope enforcement, and Protocol conformance for MockAdapter and
# ProjectionAdapter.

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from soul_protocol.engine.journal import Journal, open_journal
from soul_protocol.spec.journal import Actor
from soul_protocol.spec.retrieval import (
    AsyncSourceAdapter,
    CandidateSource,
    CredentialExpiredError,
    CredentialScopeError,
    NoSourcesError,
    RetrievalCandidate,
    RetrievalRequest,
    SourceAdapter,
)

from pocketpaw.retrieval import (
    InMemoryCredentialBroker,
    MockAdapter,
    ProjectionAdapter,
    RetrievalRouter,
)


# -- fixtures -------------------------------------------------------------


def _actor() -> Actor:
    return Actor(kind="agent", id="did:soul:test-agent", scope_context=["org:sales:leads"])


def _candidate(source: str, score: float | None = 0.9) -> RetrievalCandidate:
    return RetrievalCandidate(
        source=source,
        content={"hit": source},
        score=score,
        as_of=datetime.now(UTC),
        cached=False,
    )


def _request(**overrides) -> RetrievalRequest:
    defaults = dict(
        query="who bought the thing",
        actor=_actor(),
        scopes=["org:sales:leads"],
        strategy="parallel",
        timeout_s=2.0,
    )
    defaults.update(overrides)
    return RetrievalRequest(**defaults)


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


# -- router: strategies ---------------------------------------------------


def test_parallel_merges_candidates_from_two_sources() -> None:
    router = RetrievalRouter()
    router.register_source(
        CandidateSource(
            name="soul_memory",
            kind="projection",
            scopes=["org:sales:*"],
            adapter_ref="mock",
        ),
        MockAdapter(candidates=[_candidate("soul_memory", score=0.9)]),
    )
    router.register_source(
        CandidateSource(
            name="kb_articles",
            kind="projection",
            scopes=["org:sales:*"],
            adapter_ref="mock",
        ),
        MockAdapter(candidates=[_candidate("kb_articles", score=0.7)]),
    )

    result = router.dispatch(_request())

    assert {c.source for c in result.candidates} == {"soul_memory", "kb_articles"}
    assert set(result.sources_queried) == {"soul_memory", "kb_articles"}
    # Higher score sorts first.
    assert result.candidates[0].source == "soul_memory"
    assert result.sources_failed == []


def test_first_strategy_returns_first_non_empty() -> None:
    router = RetrievalRouter()
    first_adapter = MockAdapter(candidates=[])
    second_adapter = MockAdapter(candidates=[_candidate("kb_articles", 0.5)])
    third_adapter = MockAdapter(candidates=[_candidate("fabric", 0.9)])
    for name, adapter in [
        ("soul_memory", first_adapter),
        ("kb_articles", second_adapter),
        ("fabric", third_adapter),
    ]:
        router.register_source(
            CandidateSource(
                name=name, kind="projection", scopes=["org:sales:*"], adapter_ref="mock"
            ),
            adapter,
        )

    result = router.dispatch(_request(strategy="first"))

    # Should stop at kb_articles and never call fabric.
    assert [c.source for c in result.candidates] == ["kb_articles"]
    assert result.sources_queried == ["soul_memory", "kb_articles"]
    assert third_adapter.calls == []


def test_sequential_accumulates_up_to_limit() -> None:
    router = RetrievalRouter()
    router.register_source(
        CandidateSource(name="a", kind="projection", scopes=["org:sales:*"], adapter_ref="mock"),
        MockAdapter(candidates=[_candidate("a", 0.9), _candidate("a", 0.8)]),
    )
    never_called = MockAdapter(candidates=[_candidate("b", 0.7)])
    router.register_source(
        CandidateSource(name="b", kind="projection", scopes=["org:sales:*"], adapter_ref="mock"),
        never_called,
    )

    result = router.dispatch(_request(strategy="sequential", limit=2))
    assert len(result.candidates) == 2
    assert never_called.calls == []


def test_scope_filter_skips_unrelated_source() -> None:
    router = RetrievalRouter()
    allowed = MockAdapter(candidates=[_candidate("sales_src", 0.9)])
    forbidden = MockAdapter(candidates=[_candidate("support_src", 0.9)])
    router.register_source(
        CandidateSource(
            name="sales_src", kind="projection", scopes=["org:sales:*"], adapter_ref="mock"
        ),
        allowed,
    )
    router.register_source(
        CandidateSource(
            name="support_src",
            kind="projection",
            scopes=["org:support:*"],
            adapter_ref="mock",
        ),
        forbidden,
    )

    result = router.dispatch(_request(scopes=["org:sales:leads"]))

    assert result.sources_queried == ["sales_src"]
    assert forbidden.calls == []


def test_no_sources_raises() -> None:
    router = RetrievalRouter()
    router.register_source(
        CandidateSource(
            name="ops", kind="projection", scopes=["org:ops:*"], adapter_ref="mock"
        ),
        MockAdapter(),
    )
    with pytest.raises(NoSourcesError):
        router.dispatch(_request(scopes=["org:sales:leads"]))


def test_parallel_source_timeout_is_reported() -> None:
    router = RetrievalRouter()
    fast = MockAdapter(candidates=[_candidate("fast", 0.9)])
    slow = MockAdapter(candidates=[_candidate("slow", 0.9)], delay_s=1.0)
    router.register_source(
        CandidateSource(name="fast", kind="projection", scopes=["org:sales:*"], adapter_ref="m"),
        fast,
    )
    router.register_source(
        CandidateSource(name="slow", kind="projection", scopes=["org:sales:*"], adapter_ref="m"),
        slow,
    )

    result = router.dispatch(_request(strategy="parallel", timeout_s=0.2))

    assert any(name == "slow" for name, _ in result.sources_failed)
    assert {c.source for c in result.candidates} == {"fast"}


def test_first_strategy_timeout_falls_through() -> None:
    router = RetrievalRouter()
    router.register_source(
        CandidateSource(name="slow", kind="projection", scopes=["org:sales:*"], adapter_ref="m"),
        MockAdapter(candidates=[_candidate("slow")], delay_s=1.0),
    )
    router.register_source(
        CandidateSource(name="fast", kind="projection", scopes=["org:sales:*"], adapter_ref="m"),
        MockAdapter(candidates=[_candidate("fast")]),
    )

    result = router.dispatch(_request(strategy="first", timeout_s=0.2))
    assert [c.source for c in result.candidates] == ["fast"]
    assert any(name == "slow" for name, _ in result.sources_failed)


# -- router: journal emission --------------------------------------------


def test_dispatch_emits_retrieval_query_event(journal: Journal) -> None:
    router = RetrievalRouter(journal=journal)
    router.register_source(
        CandidateSource(
            name="soul_memory",
            kind="projection",
            scopes=["org:sales:*"],
            adapter_ref="mock",
        ),
        MockAdapter(candidates=[_candidate("soul_memory", 0.9)]),
    )
    correlation = uuid4()
    router.dispatch(_request(correlation_id=correlation))

    events = journal.query(action="retrieval.query")
    assert len(events) == 1
    ev = events[0]
    assert ev.action == "retrieval.query"
    assert ev.actor.id == "did:soul:test-agent"
    assert ev.scope == ["org:sales:leads"]
    assert ev.correlation_id == correlation
    assert isinstance(ev.payload, dict)
    assert ev.payload["query"] == "who bought the thing"
    assert ev.payload["sources_queried"] == ["soul_memory"]


# -- broker ---------------------------------------------------------------


def test_broker_acquire_returns_scoped_credential() -> None:
    broker = InMemoryCredentialBroker(ttl_s=60)
    cred = broker.acquire("drive", ["org:sales:leads"])
    assert cred.source == "drive"
    assert cred.scopes == ["org:sales:leads"]
    assert cred.token
    assert cred.expires_at > cred.acquired_at
    assert (cred.expires_at - cred.acquired_at).total_seconds() == pytest.approx(60, abs=1)


def test_broker_refuses_credential_used_by_wrong_scope() -> None:
    broker = InMemoryCredentialBroker()
    cred = broker.acquire("drive", ["org:sales:*"])
    broker.ensure_usable(cred, ["org:sales:leads"])  # overlap allowed
    with pytest.raises(CredentialScopeError):
        broker.ensure_usable(cred, ["org:support:tickets"])


def test_broker_credential_expires_after_ttl() -> None:
    broker = InMemoryCredentialBroker(ttl_s=0.05)
    cred = broker.acquire("drive", ["org:sales:leads"])
    time.sleep(0.1)
    with pytest.raises(CredentialExpiredError):
        broker.ensure_usable(cred, ["org:sales:leads"])


def test_broker_emits_journal_events(journal: Journal) -> None:
    actor = Actor(kind="system", id="system:credential-broker")
    broker = InMemoryCredentialBroker(ttl_s=60, journal=journal, broker_actor=actor)
    cred = broker.acquire("drive", ["org:sales:leads"])
    broker.ensure_usable(cred, ["org:sales:leads"])
    broker.mark_used(cred)
    broker.revoke(cred.id)

    actions = [e.action for e in journal.query()]
    assert "credential.acquired" in actions
    assert "credential.used" in actions
    assert "credential.expired" in actions


def test_broker_expiry_emits_event(journal: Journal) -> None:
    broker = InMemoryCredentialBroker(ttl_s=0.05, journal=journal)
    cred = broker.acquire("drive", ["org:sales:leads"])
    time.sleep(0.1)
    with pytest.raises(CredentialExpiredError):
        broker.ensure_usable(cred, ["org:sales:leads"])
    actions = [e.action for e in journal.query()]
    assert actions.count("credential.expired") == 1


# -- adapter conformance --------------------------------------------------


def test_mock_and_projection_are_source_adapters() -> None:
    mock = MockAdapter()
    proj = ProjectionAdapter(lambda req: [])
    assert isinstance(mock, SourceAdapter)
    assert isinstance(proj, SourceAdapter)


def test_projection_adapter_calls_underlying_fn() -> None:
    calls: list[RetrievalRequest] = []

    def fn(req: RetrievalRequest) -> list[RetrievalCandidate]:
        calls.append(req)
        return [_candidate("proj", 0.5)]

    adapter = ProjectionAdapter(fn)
    out = adapter.query(_request(), credential=None)
    assert [c.source for c in out] == ["proj"]
    assert len(calls) == 1


# -- broker: fail-closed audit emission -----------------------------------


class _FailingJournal:
    """Stub journal whose append always raises. Lets us assert fail-closed
    behavior without needing a real SQLite error path."""

    def __init__(self) -> None:
        self.appends: int = 0

    def append(self, _entry) -> None:
        self.appends += 1
        raise RuntimeError("simulated journal-down condition")


def test_broker_acquire_fails_closed_when_journal_append_raises() -> None:
    """If the journal is configured and append raises, acquire propagates
    the error rather than silently issuing a credential whose acquisition
    never made it into the audit trail. The credential must NOT remain
    trackable by the broker either — we don't hand back an orphaned token.
    """
    bad_journal = _FailingJournal()
    broker = InMemoryCredentialBroker(journal=bad_journal)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="simulated journal-down"):
        broker.acquire("drive", ["org:sales:leads"])
    # The append was attempted exactly once.
    assert bad_journal.appends == 1


def test_broker_without_journal_is_fire_and_forget() -> None:
    """No journal configured = explicit opt-out. acquire/use/revoke all
    succeed silently. This is the path tests and ephemeral scripts use."""
    broker = InMemoryCredentialBroker()  # journal=None
    cred = broker.acquire("drive", ["org:sales:leads"])
    broker.mark_used(cred)
    broker.revoke(cred.id)


# -- broker: scope overlap policy pin -------------------------------------


def test_scopes_overlap_wildcard_grant_matches_specific_requester() -> None:
    """A credential issued for `org:sales:*` is usable by a requester
    operating at `org:sales:leads`. Pinning this direction so a future
    refactor can't break fanout."""
    from soul_protocol.engine.journal import scopes_overlap

    assert scopes_overlap(["org:sales:*"], ["org:sales:leads"])


def test_scopes_overlap_specific_grant_matches_wildcard_requester() -> None:
    """A credential issued for a specific scope `org:sales:leads` is
    usable by a requester presenting `org:sales:*` — the requester is
    asserting it operates anywhere in the subtree, and a specific
    credential covers a specific point in that subtree. Pinning this
    direction so a future tightening can't break routers that fan out
    over a broad scope."""
    from soul_protocol.engine.journal import scopes_overlap

    assert scopes_overlap(["org:sales:leads"], ["org:sales:*"])


def test_scopes_overlap_disjoint_scopes_do_not_match() -> None:
    """A credential for one subtree must not be usable in another."""
    from soul_protocol.engine.journal import scopes_overlap

    assert not scopes_overlap(["org:sales:*"], ["org:support:*"])
    assert not scopes_overlap(["org:sales:leads"], ["org:support:tickets"])


def test_broker_enforces_scope_asymmetry_on_ensure_usable() -> None:
    """Integration-level pin: the broker honors the same asymmetric
    policy. A credential granted broadly is usable by a specific
    requester, and vice versa — both should succeed."""
    broker = InMemoryCredentialBroker(ttl_s=60)

    cred_wide = broker.acquire("drive", ["org:sales:*"])
    broker.ensure_usable(cred_wide, ["org:sales:leads"])

    cred_narrow = broker.acquire("drive", ["org:sales:leads"])
    broker.ensure_usable(cred_narrow, ["org:sales:*"])


def test_dataref_source_triggers_broker(journal: Journal) -> None:
    broker = InMemoryCredentialBroker(ttl_s=60, journal=journal)
    router = RetrievalRouter(journal=journal, broker=broker)
    mock = MockAdapter(candidates=[_candidate("drive", 0.9)])
    mock.supports_dataref = True
    router.register_source(
        CandidateSource(
            name="drive", kind="dataref", scopes=["org:sales:*"], adapter_ref="drive"
        ),
        mock,
    )
    router.dispatch(_request(strategy="sequential"))

    actions = [e.action for e in journal.query()]
    assert "credential.acquired" in actions
    assert "credential.used" in actions
    assert "retrieval.query" in actions
    # MockAdapter saw a real credential.
    assert mock.calls
    _, cred = mock.calls[0]
    assert cred is not None
    assert cred.source == "drive"


# ---------- primitive #4: RetrievalRequest.point_in_time --------------


def test_point_in_time_round_trips(journal: Journal) -> None:
    """Unit: the field is tz-aware UTC and survives model validation."""
    pit = datetime.now(UTC)
    req = RetrievalRequest(
        query="q",
        actor=_actor(),
        scopes=["org:s"],
        point_in_time=pit,
    )
    assert req.point_in_time == pit


def test_point_in_time_naive_raises() -> None:
    """Unit: naive datetimes rejected at validation time."""
    from pydantic import ValidationError

    naive = datetime.now()  # no tzinfo
    with pytest.raises(ValidationError):
        RetrievalRequest(
            query="q",
            actor=_actor(),
            scopes=["org:s"],
            point_in_time=naive,
        )


def test_point_in_time_defaults_to_none() -> None:
    """Unit: pre-0.3.2 callers keep working — field is optional."""
    req = RetrievalRequest(query="q", actor=_actor(), scopes=["org:s"])
    assert req.point_in_time is None


def test_point_in_time_emitted_on_journal_event(
    journal: Journal,
) -> None:
    """E2E: when point_in_time is set, the retrieval.query event records it."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    mock = MockAdapter(candidates=[_candidate("drive", 0.9)])
    router.register_source(
        CandidateSource(
            name="drive", kind="projection", scopes=["org:sales:*"], adapter_ref="drive"
        ),
        mock,
    )
    pit = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    router.dispatch(_request(point_in_time=pit))

    events = journal.query(action="retrieval.query")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["point_in_time"] == pit.isoformat()


def test_point_in_time_absent_omitted_from_payload(
    journal: Journal,
) -> None:
    """E2E: when point_in_time is None (default), it's not in the payload
    — avoids stuffing the payload with null fields for 99% of retrievals."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    mock = MockAdapter(candidates=[_candidate("kb", 0.7)])
    router.register_source(
        CandidateSource(
            name="kb", kind="projection", scopes=["org:sales:*"], adapter_ref="kb"
        ),
        mock,
    )
    router.dispatch(_request())

    events = journal.query(action="retrieval.query")
    assert "point_in_time" not in events[0].payload


def test_point_in_time_not_supported_error_exists() -> None:
    """Smoke: the sentinel exception for adapters that can't honor
    time-travel is exported and usable."""
    from soul_protocol.spec.retrieval import PointInTimeNotSupported

    exc = PointInTimeNotSupported("drive doesn't support AT")
    assert isinstance(exc, Exception)
    assert "drive" in str(exc)


def test_point_in_time_realworld_drive_adapter_pattern(
    journal: Journal,
) -> None:
    """Real-world sim: replays how pocketpaw's connectors/drive/source.py
    should look after 0.3.2. Pre-0.3.2 it used @at=<iso>|<query> string
    prefixes. Post-0.3.2 the adapter reads request.point_in_time directly.
    """
    from pocketpaw.retrieval import InMemoryCredentialBroker

    class DriveLikeAdapter:
        """Mock adapter that honors point_in_time."""

        supports_dataref = True

        def __init__(self) -> None:
            self.resolved_at: datetime | None = None

        def query(self, request, credential):
            # Real driver would pass point_in_time to the Drive API's
            # `revisions.get` endpoint. Here we just record it.
            self.resolved_at = request.point_in_time
            return [
                RetrievalCandidate(
                    source="drive",
                    content={"hit": "doc123", "revision": "r42"},
                    as_of=request.point_in_time or datetime.now(UTC),
                )
            ]

    adapter = DriveLikeAdapter()
    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    router.register_source(
        CandidateSource(
            name="drive",
            kind="dataref",
            scopes=["org:sales:*"],
            adapter_ref="drive",
        ),
        adapter,  # type: ignore[arg-type]
    )

    pit = datetime(2024, 6, 1, 9, 0, 0, tzinfo=UTC)
    router.dispatch(_request(point_in_time=pit))

    assert adapter.resolved_at == pit


# ---------- primitive #3: DataRef in spec/retrieval.py -------------


def test_dataref_round_trips_through_json() -> None:
    """Unit: the retrieval DataRef serializes + deserializes cleanly."""
    from soul_protocol.spec.retrieval import DataRef

    ref = DataRef(
        source="drive",
        id="doc123",
        scopes=["org:sales:*"],
        revision_id="r42",
        extra={"mime": "application/pdf"},
    )
    data = ref.model_dump()
    assert data["kind"] == "dataref"
    assert data["source"] == "drive"

    restored = DataRef.model_validate(data)
    assert restored == ref


def test_dataref_defaults() -> None:
    """Unit: scopes/revision_id/extra have sensible defaults."""
    from soul_protocol.spec.retrieval import DataRef

    ref = DataRef(source="slack", id="msg-9999")
    assert ref.kind == "dataref"
    assert ref.scopes == []
    assert ref.revision_id is None
    assert ref.extra == {}


def test_dataref_empty_source_rejected() -> None:
    """Unit: source must be non-empty (min_length=1)."""
    from pydantic import ValidationError
    from soul_protocol.spec.retrieval import DataRef

    with pytest.raises(ValidationError):
        DataRef(source="", id="x")


def test_candidate_content_as_dataref() -> None:
    """Smoke: RetrievalCandidate.content accepts a typed DataRef."""
    from soul_protocol.spec.retrieval import DataRef

    ref = DataRef(source="drive", id="doc1")
    cand = RetrievalCandidate(
        source="drive",
        content=ref,
        as_of=datetime.now(UTC),
    )
    assert isinstance(cand.content, DataRef)
    assert cand.content.source == "drive"


def test_candidate_content_as_dict_still_works() -> None:
    """Smoke: pre-0.3.2 callers returning dict content keep working."""
    cand = RetrievalCandidate(
        source="kb",
        content={"hit": "xyz", "score": 0.7},
        as_of=datetime.now(UTC),
    )
    assert isinstance(cand.content, dict)


def test_candidate_dataref_round_trips_through_json() -> None:
    """E2E: a candidate with a DataRef content round-trips cleanly."""
    from soul_protocol.spec.retrieval import DataRef

    cand = RetrievalCandidate(
        source="drive",
        content=DataRef(source="drive", id="doc1", revision_id="r1"),
        as_of=datetime.now(UTC),
    )
    data = cand.model_dump(mode="json")
    restored = RetrievalCandidate.model_validate(data)
    assert isinstance(restored.content, DataRef)
    assert restored.content.id == "doc1"
    assert restored.content.revision_id == "r1"


def test_candidate_plain_dict_round_trips_as_dict() -> None:
    """E2E: opaque-dict content round-trips as a plain dict, NOT coerced
    into a DataRef even if the dict happens to have a 'source' key."""
    cand = RetrievalCandidate(
        source="kb",
        content={"source": "kb", "hit": "article-42"},
        as_of=datetime.now(UTC),
    )
    data = cand.model_dump(mode="json")
    restored = RetrievalCandidate.model_validate(data)
    # Pydantic's tagged-union resolution prefers DataRef when `kind` is
    # present; a plain dict without `kind` stays a dict.
    assert isinstance(restored.content, dict) or (
        hasattr(restored.content, "kind") and restored.content.kind == "dataref"
    )


def test_dataref_realworld_drive_candidate_pattern() -> None:
    """Real-world sim: the pocketpaw Drive adapter pre-0.3.2 returned
    candidates with content={"source": "drive", "id": ...,
    "revision_id": ..., "extra": {...}} as a dict. Post-0.3.2 it can
    return a typed DataRef directly — and the router sees consistent
    shape across every Zero-Copy adapter.
    """
    from soul_protocol.spec.retrieval import DataRef

    def drive_adapter_returns() -> RetrievalCandidate:
        # What the adapter writes:
        return RetrievalCandidate(
            source="drive",
            content=DataRef(
                source="drive",
                id="1abc-file-xyz",
                scopes=["org:eng:docs"],
                revision_id="rev_0042",
                extra={
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2024-06-01T09:00:00Z",
                },
            ),
            score=0.82,
            as_of=datetime.now(UTC),
        )

    cand = drive_adapter_returns()
    ref = cand.content
    assert isinstance(ref, DataRef)
    assert ref.source == "drive"
    assert ref.id == "1abc-file-xyz"
    assert ref.revision_id == "rev_0042"
    assert ref.extra["mimeType"].startswith("application/")


# ---------- primitive #5: async SourceAdapter.aquery + adispatch ------


class AsyncMockAdapter:
    """Async-native adapter for testing adispatch's aquery path."""

    supports_dataref = False

    def __init__(self, candidates: list[RetrievalCandidate]) -> None:
        self._candidates = candidates
        self.sync_calls = 0
        self.async_calls = 0

    def query(self, request, credential):
        self.sync_calls += 1
        return list(self._candidates)

    async def aquery(self, request, credential):
        self.async_calls += 1
        return list(self._candidates)


class SyncOnlyAdapter:
    """Pure sync adapter — no aquery defined at all."""

    supports_dataref = False

    def __init__(self, candidates: list[RetrievalCandidate]) -> None:
        self._candidates = candidates
        self.call_count = 0

    def query(self, request, credential):
        self.call_count += 1
        return list(self._candidates)


@pytest.mark.asyncio
async def test_adispatch_prefers_aquery_when_available(journal: Journal) -> None:
    """Unit: an adapter with aquery has its async path taken."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    adapter = AsyncMockAdapter([_candidate("drive", 0.9)])
    router.register_source(
        CandidateSource(
            name="drive", kind="projection", scopes=["org:sales:*"], adapter_ref="drive"
        ),
        adapter,  # type: ignore[arg-type]
    )

    result = await router.adispatch(_request())
    assert len(result.candidates) == 1
    assert adapter.async_calls == 1
    assert adapter.sync_calls == 0


@pytest.mark.asyncio
async def test_adispatch_falls_back_to_thread_for_sync_only(journal: Journal) -> None:
    """Unit: sync-only adapter still works under adispatch — threaded."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    adapter = SyncOnlyAdapter([_candidate("kb", 0.5)])
    router.register_source(
        CandidateSource(
            name="kb", kind="projection", scopes=["org:sales:*"], adapter_ref="kb"
        ),
        adapter,  # type: ignore[arg-type]
    )

    result = await router.adispatch(_request())
    assert len(result.candidates) == 1
    assert adapter.call_count == 1


@pytest.mark.asyncio
async def test_adispatch_sequential_strategy_delegates_to_sync(journal: Journal) -> None:
    """Smoke: non-parallel strategies run on the sync path via to_thread.
    Still gets a RetrievalResult back."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    router.register_source(
        CandidateSource(
            name="kb", kind="projection", scopes=["org:sales:*"], adapter_ref="kb"
        ),
        MockAdapter(candidates=[_candidate("kb", 0.5)]),
    )

    result = await router.adispatch(_request(strategy="sequential"))
    assert len(result.candidates) == 1


@pytest.mark.asyncio
async def test_adispatch_timeout_fires_per_source(journal: Journal) -> None:
    """E2E: a slow adapter hits the per-source timeout; others still succeed."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    class SlowAsyncAdapter:
        supports_dataref = False

        async def query(self, request, credential):
            return []

        async def aquery(self, request, credential):
            await asyncio.sleep(5.0)  # way beyond test timeout
            return [_candidate("slow")]

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    router.register_source(
        CandidateSource(
            name="slow", kind="projection", scopes=["org:sales:*"], adapter_ref="slow"
        ),
        SlowAsyncAdapter(),  # type: ignore[arg-type]
    )
    router.register_source(
        CandidateSource(
            name="fast", kind="projection", scopes=["org:sales:*"], adapter_ref="fast"
        ),
        AsyncMockAdapter([_candidate("fast", 0.9)]),  # type: ignore[arg-type]
    )

    result = await router.adispatch(_request(timeout_s=0.2))

    # Fast returned; slow timed out.
    candidate_sources = {c.source for c in result.candidates}
    assert "fast" in candidate_sources
    assert any(name == "slow" and "timed out" in reason for name, reason in result.sources_failed)


@pytest.mark.asyncio
async def test_adispatch_scope_filtering_still_applies(journal: Journal) -> None:
    """E2E: scope overlap is enforced just like sync dispatch."""
    from pocketpaw.retrieval import InMemoryCredentialBroker

    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    router.register_source(
        CandidateSource(
            name="eng", kind="projection", scopes=["org:eng:*"], adapter_ref="eng"
        ),
        AsyncMockAdapter([_candidate("eng")]),  # type: ignore[arg-type]
    )

    # Request with a scope that doesn't overlap any registered source.
    with pytest.raises(NoSourcesError):
        await router.adispatch(_request(scopes=["org:hr:leads"]))


def test_async_source_adapter_protocol_conformance() -> None:
    """Smoke: AsyncSourceAdapter runtime_checkable correctly identifies
    sync-only vs async-capable adapters."""
    # AsyncSourceAdapter is now imported at module top from soul_protocol.spec.retrieval

    async_adapter = AsyncMockAdapter([])
    sync_only = SyncOnlyAdapter([])

    assert isinstance(async_adapter, AsyncSourceAdapter)
    assert not isinstance(sync_only, AsyncSourceAdapter)


@pytest.mark.asyncio
async def test_adispatch_realworld_async_sdk_adapter_pattern(
    journal: Journal,
) -> None:
    """Real-world sim: modern SaaS SDKs (Salesforce REST, Slack bolt,
    Gmail API) are async-first. Pre-0.3.2, adapters bridged through
    ``asyncio.run`` inside a sync ``query`` — lost the event loop context,
    created new threads, made timeouts unreliable. Post-0.3.2, the adapter
    implements ``aquery`` directly and adispatch awaits it cooperatively.
    """
    from pocketpaw.retrieval import InMemoryCredentialBroker

    class FakeSalesforceAdapter:
        """Adapter simulating an async-native Salesforce client."""

        supports_dataref = True

        def __init__(self) -> None:
            self.aquery_reached = False

        def query(self, request, credential):
            # Sync bridge exists for callers that only have sync dispatch.
            # It would be something like `asyncio.run(self.aquery(...))`.
            raise NotImplementedError("use aquery")

        async def aquery(self, request, credential):
            # Simulates awaiting the SDK (httpx.AsyncClient, etc).
            await asyncio.sleep(0.001)
            self.aquery_reached = True
            return [
                RetrievalCandidate(
                    source="salesforce",
                    content={"Id": "001xyz", "Name": "Acme Corp"},
                    score=0.88,
                    as_of=datetime.now(UTC),
                )
            ]

    adapter = FakeSalesforceAdapter()
    router = RetrievalRouter(journal=journal, broker=InMemoryCredentialBroker())
    router.register_source(
        CandidateSource(
            name="salesforce",
            kind="dataref",
            scopes=["org:sales:*"],
            adapter_ref="salesforce",
        ),
        adapter,  # type: ignore[arg-type]
    )

    result = await router.adispatch(_request())

    assert adapter.aquery_reached is True
    assert len(result.candidates) == 1
    assert result.candidates[0].source == "salesforce"
