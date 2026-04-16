# tests/ee/test_widget_track_endpoint.py — Coverage for POST /widgets/track.
# Created: 2026-04-16 (feat/widget-track-endpoint) — closes the integration
# loop opened by #955 (widget journal projection) + paw-enterprise #74
# (SuggestedWidgetsFeed UI). The UI has been POSTing to /widgets/track for
# weeks; until this endpoint landed every interaction 404'd and dropped on
# the floor. These tests pin the writer contract so a future refactor
# doesn't quietly regress it:
#
#   1. Happy path — valid payload returns 200 + ack, and a
#      widget.interaction.recorded event lands on the journal with
#      matching scope / actor / action_type.
#   2. Validation — missing widget_name, bad actor.kind, empty
#      action_type all 422 before anything hits the journal.
#   3. Defaults — empty metadata (and "no metadata key") default to {}.
#   4. Correlation id round-trip — request carries a UUID; journal event
#      carries the same one.
#   5. Sequential writes — seq increments by one per POST; event ids are
#      distinct.
#   6. Scope fallback — actor.scope_context=[] → event emitted with
#      ["org:*"] default (journal refuses scope=[], and the UI's
#      anonymous-actor path always hands us an empty list).
#   7. Downstream projection — three POSTs flow through to GET /usage
#      and the usage row reflects count=3 / promoting_count=3. This is
#      the end-to-end proof the integration loop is closed.

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from soul_protocol.engine.journal import open_journal

from ee.journal_dep import get_journal, reset_journal_cache
from ee.widget.events import ACTION_WIDGET_INTERACTION_RECORDED
from ee.widget.router import reset_store_cache, router

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/ee/test_widget_journal.py so the caches don't
# leak across tests in the same file or across sibling files.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_caches():
    reset_journal_cache()
    reset_store_cache()
    yield
    reset_journal_cache()
    reset_store_cache()


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "track_journal.db"


@pytest.fixture
def app(journal_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    # Single journal instance for the app lifetime — the warmed store
    # cache in the router keys off id(journal), so a fresh journal per
    # request would defeat the cache and double-apply events.
    _journal = open_journal(journal_path)
    a.dependency_overrides[get_journal] = lambda: _journal
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _valid_payload(**overrides) -> dict:
    payload = {
        "widget_name": "metrics_chart",
        "actor": {
            "kind": "user",
            "id": "user:priya",
            "scope_context": ["org:sales:leads"],
        },
        "pocket_id": "pocket-1",
        "surface": "dashboard",
        "action_type": "open",
        "metadata": {"clicks": 3},
        "correlation_id": None,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. Happy path.
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_post_returns_ack_and_emits_event(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        res = client.post("/widgets/track", json=_valid_payload())
        assert res.status_code == 200, res.text

        body = res.json()
        assert body["ok"] is True
        assert isinstance(body["event_id"], str)
        # Seq is 0-indexed on an empty journal, so the first write is 0.
        assert body["seq"] == 0

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert len(events) == 1
        ev = events[0]
        assert str(ev.id) == body["event_id"]
        assert ev.actor.kind == "user"
        assert ev.actor.id == "user:priya"
        assert list(ev.scope) == ["org:sales:leads"]
        assert ev.payload["widget_name"] == "metrics_chart"
        assert ev.payload["action_type"] == "open"
        assert ev.payload["surface"] == "dashboard"
        assert ev.payload["pocket_id"] == "pocket-1"
        assert ev.payload["metadata"] == {"clicks": 3}


# ---------------------------------------------------------------------------
# 2. Validation.
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_widget_name_returns_422(self, client: TestClient) -> None:
        payload = _valid_payload()
        del payload["widget_name"]
        res = client.post("/widgets/track", json=payload)
        assert res.status_code == 422

    def test_empty_widget_name_returns_422(self, client: TestClient) -> None:
        res = client.post("/widgets/track", json=_valid_payload(widget_name=""))
        assert res.status_code == 422

    def test_unknown_actor_kind_returns_422(self, client: TestClient) -> None:
        payload = _valid_payload(actor={"kind": "alien", "id": "x"})
        res = client.post("/widgets/track", json=payload)
        assert res.status_code == 422

    def test_empty_actor_id_returns_422(self, client: TestClient) -> None:
        payload = _valid_payload(actor={"kind": "user", "id": ""})
        res = client.post("/widgets/track", json=payload)
        assert res.status_code == 422

    def test_empty_action_type_returns_422(self, client: TestClient) -> None:
        res = client.post("/widgets/track", json=_valid_payload(action_type=""))
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# 3. Metadata defaults.
# ---------------------------------------------------------------------------


class TestMetadataDefault:
    def test_omitted_metadata_defaults_to_empty_dict(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        payload = _valid_payload()
        del payload["metadata"]
        res = client.post("/widgets/track", json=payload)
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert events[-1].payload["metadata"] == {}

    def test_explicit_empty_metadata_stays_empty(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        res = client.post("/widgets/track", json=_valid_payload(metadata={}))
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert events[-1].payload["metadata"] == {}


# ---------------------------------------------------------------------------
# 4. Correlation id round-trip.
# ---------------------------------------------------------------------------


class TestCorrelationId:
    def test_correlation_id_propagates_to_event(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        cid = uuid4()
        res = client.post(
            "/widgets/track",
            json=_valid_payload(correlation_id=str(cid)),
        )
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert events[-1].correlation_id == cid

    def test_correlation_id_accepts_uuid_string(
        self,
        client: TestClient,
    ) -> None:
        """The UI generates `wi_<uuid>` strings; Pydantic only accepts
        bare UUIDs. Confirm malformed correlation ids 422 rather than
        quietly dropping.
        """

        res = client.post(
            "/widgets/track",
            json=_valid_payload(correlation_id="wi_not-a-uuid"),
        )
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# 5. Sequential writes — seq increments.
# ---------------------------------------------------------------------------


class TestSequentialWrites:
    def test_three_posts_yield_three_distinct_event_ids_and_seqs(
        self,
        client: TestClient,
    ) -> None:
        seqs = []
        ids = []
        for i in range(3):
            res = client.post(
                "/widgets/track",
                json=_valid_payload(metadata={"i": i}),
            )
            assert res.status_code == 200
            body = res.json()
            seqs.append(body["seq"])
            ids.append(body["event_id"])

        # Seqs are strictly monotonic.
        assert seqs == [0, 1, 2]
        # Event ids are all distinct.
        assert len(set(ids)) == 3
        # Every id parses as a UUID.
        for eid in ids:
            UUID(eid)


# ---------------------------------------------------------------------------
# 6. Scope fallback.
# ---------------------------------------------------------------------------


class TestScopeFallback:
    def test_empty_scope_context_falls_back_to_org_wildcard(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """The UI's anonymous-actor helper passes scope_context=[].
        The journal refuses scope=[] — the writer must substitute a
        concrete wildcard so the emit succeeds.
        """

        payload = _valid_payload(
            actor={
                "kind": "user",
                "id": "anon:abc123",
                "scope_context": [],
            },
        )
        res = client.post("/widgets/track", json=payload)
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert list(events[-1].scope) == ["org:*"]

    def test_missing_scope_context_falls_back_to_org_wildcard(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """scope_context is optional on Actor — when the client omits
        the key entirely, same fallback applies.
        """

        payload = _valid_payload(actor={"kind": "user", "id": "anon:abc123"})
        res = client.post("/widgets/track", json=payload)
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert list(events[-1].scope) == ["org:*"]


# ---------------------------------------------------------------------------
# 7. Downstream projection — end-to-end integration proof.
# ---------------------------------------------------------------------------


class TestDownstreamProjection:
    def test_three_posts_reflect_in_usage_endpoint(
        self,
        client: TestClient,
    ) -> None:
        """POST three ``open`` interactions for the same widget, then
        GET /widgets/usage — the row should carry count=3 and
        promoting_count=3. This is the end-to-end contract the UI in
        paw-enterprise #74 depends on; if it breaks the widget feed's
        ranking silently regresses.
        """

        for _ in range(3):
            res = client.post(
                "/widgets/track",
                json=_valid_payload(action_type="open"),
            )
            assert res.status_code == 200

        res = client.get("/widgets/usage?window_days=30")
        assert res.status_code == 200
        body = res.json()

        rows = [r for r in body["entries"] if r["widget_name"] == "metrics_chart"]
        assert len(rows) == 1
        assert rows[0]["count"] == 3
        assert rows[0]["promoting_count"] == 3

    def test_mixed_actions_split_count_and_promoting(
        self,
        client: TestClient,
    ) -> None:
        """Two ``open`` (promoting) + one ``dismiss`` (non-promoting)
        should roll up as count=3 / promoting_count=2. Matches #941's
        promote-vs-dismiss split.
        """

        for action in ("open", "open", "dismiss"):
            res = client.post(
                "/widgets/track",
                json=_valid_payload(action_type=action),
            )
            assert res.status_code == 200

        res = client.get("/widgets/usage?window_days=30")
        assert res.status_code == 200
        rows = [
            r
            for r in res.json()["entries"]
            if r["widget_name"] == "metrics_chart"
        ]
        assert len(rows) == 1
        assert rows[0]["count"] == 3
        assert rows[0]["promoting_count"] == 2
