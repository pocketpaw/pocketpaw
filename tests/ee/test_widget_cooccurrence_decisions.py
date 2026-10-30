# tests/ee/test_widget_cooccurrence_decisions.py — Cluster B Sub-PR #2.
# Created: 2026-04-19 — Coverage for the new accept/dismiss writer endpoints
# that close the loop on paw-enterprise #74's SuggestedWidgetsFeed.
# Mirrors the style + fixture shape of tests/ee/test_widget_track_endpoint.py
# so the two writers read as one story; a future refactor that breaks one
# surfaces in both suites.
#
# What this pins:
#   1. Happy path — valid payload returns 200 + ack, and the right event
#      action lands on the journal for each route.
#   2. Validation — missing signature / actor / widget names all 422.
#   3. Scope fallback — empty actor.scope_context falls back to ["org:*"],
#      matching the /widgets/track contract.
#   4. Route separation — accept emits ``widget.cooccurrence.accepted``,
#      dismiss emits ``widget.cooccurrence.dismissed``; neither leaks
#      into the other's stream.
#   5. Signature echo — the response journal event carries the exact
#      signature the UI passed in, not a recomputed one. Ensures the
#      feed's accept/dismiss state stays stable even if the tokenisation
#      rule changes between surface and decision.

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from soul_protocol.engine.journal import open_journal

from pocketpaw.journal_dep import get_journal, reset_journal_cache
from pocketpaw.widget.events import (
    ACTION_WIDGET_COOCCURRENCE_ACCEPTED,
    ACTION_WIDGET_COOCCURRENCE_DISMISSED,
)
from pocketpaw.widget.router import reset_store_cache, router


@pytest.fixture(autouse=True)
def _isolate_caches():
    reset_journal_cache()
    reset_store_cache()
    yield
    reset_journal_cache()
    reset_store_cache()


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "coocc_journal.db"


@pytest.fixture
def app(journal_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    _journal = open_journal(journal_path)
    a.dependency_overrides[get_journal] = lambda: _journal
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _valid_payload(**overrides) -> dict:
    payload = {
        "signature": "leads::pipeline",
        "widget_a": "leads_table",
        "widget_b": "pipeline_chart",
        "actor": {
            "kind": "user",
            "id": "user:priya",
            "scope_context": ["org:sales:*"],
        },
        "pocket_id": "pocket-1",
        "reason": "",
        "correlation_id": None,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. Happy path.
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_accept_returns_ack_and_emits_event(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        res = client.post(
            "/widgets/cooccurrence/accept",
            json=_valid_payload(),
        )
        assert res.status_code == 200, res.text

        body = res.json()
        assert body["ok"] is True
        assert isinstance(body["event_id"], str)
        UUID(body["event_id"])

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_COOCCURRENCE_ACCEPTED)
        assert len(events) == 1
        ev = events[0]
        assert str(ev.id) == body["event_id"]
        assert ev.actor.id == "user:priya"
        assert list(ev.scope) == ["org:sales:*"]
        assert ev.payload["signature"] == "leads::pipeline"
        assert ev.payload["widget_a"] == "leads_table"
        assert ev.payload["widget_b"] == "pipeline_chart"
        assert ev.payload["pocket_id"] == "pocket-1"

    def test_dismiss_returns_ack_and_emits_event(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        res = client.post(
            "/widgets/cooccurrence/dismiss",
            json=_valid_payload(reason="not relevant to this pocket"),
        )
        assert res.status_code == 200, res.text

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_COOCCURRENCE_DISMISSED)
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["reason"] == "not relevant to this pocket"


# ---------------------------------------------------------------------------
# 2. Validation.
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "route", ["/widgets/cooccurrence/accept", "/widgets/cooccurrence/dismiss"]
    )
    def test_missing_signature_returns_422(
        self,
        client: TestClient,
        route: str,
    ) -> None:
        payload = _valid_payload()
        del payload["signature"]
        res = client.post(route, json=payload)
        assert res.status_code == 422

    @pytest.mark.parametrize(
        "route", ["/widgets/cooccurrence/accept", "/widgets/cooccurrence/dismiss"]
    )
    def test_empty_signature_returns_422(
        self,
        client: TestClient,
        route: str,
    ) -> None:
        res = client.post(route, json=_valid_payload(signature=""))
        assert res.status_code == 422

    @pytest.mark.parametrize(
        "route", ["/widgets/cooccurrence/accept", "/widgets/cooccurrence/dismiss"]
    )
    def test_missing_actor_returns_422(
        self,
        client: TestClient,
        route: str,
    ) -> None:
        payload = _valid_payload()
        del payload["actor"]
        res = client.post(route, json=payload)
        assert res.status_code == 422

    @pytest.mark.parametrize(
        "route", ["/widgets/cooccurrence/accept", "/widgets/cooccurrence/dismiss"]
    )
    def test_empty_widget_name_returns_422(
        self,
        client: TestClient,
        route: str,
    ) -> None:
        res = client.post(route, json=_valid_payload(widget_a=""))
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# 3. Scope fallback.
# ---------------------------------------------------------------------------


class TestScopeFallback:
    def test_empty_scope_context_falls_back_to_org_wildcard(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        payload = _valid_payload(
            actor={
                "kind": "user",
                "id": "anon:abc123",
                "scope_context": [],
            },
        )
        res = client.post("/widgets/cooccurrence/accept", json=payload)
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        events = journal.query(action=ACTION_WIDGET_COOCCURRENCE_ACCEPTED)
        assert list(events[-1].scope) == ["org:*"]


# ---------------------------------------------------------------------------
# 4. Route separation.
# ---------------------------------------------------------------------------


class TestRouteSeparation:
    def test_accept_does_not_leak_into_dismiss_stream(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        r1 = client.post("/widgets/cooccurrence/accept", json=_valid_payload())
        r2 = client.post("/widgets/cooccurrence/dismiss", json=_valid_payload())
        assert r1.status_code == 200
        assert r2.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        accepted = journal.query(action=ACTION_WIDGET_COOCCURRENCE_ACCEPTED)
        dismissed = journal.query(action=ACTION_WIDGET_COOCCURRENCE_DISMISSED)

        assert len(accepted) == 1
        assert len(dismissed) == 1
        # Both events landed on the journal but under distinct action names —
        # the projection (and the read side) can key off the action to
        # figure out which bucket the signature lives in.
        assert str(accepted[0].id) != str(dismissed[0].id)


# ---------------------------------------------------------------------------
# 5. Signature echo.
# ---------------------------------------------------------------------------


class TestSignatureEcho:
    def test_response_event_carries_the_exact_signature_from_the_request(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """The read side surfaces a signature the write side has to echo
        verbatim. If the router recomputes it (for instance, via the
        ``cooccurrence_signature`` helper) the accept/dismiss bucket
        could diverge from the read view — a dismissed pair would
        re-surface as a new suggestion because the recomputed signature
        doesn't match the stored dismissal.
        """

        raw_signature = "deliberately::ordered::bag"
        res = client.post(
            "/widgets/cooccurrence/accept",
            json=_valid_payload(signature=raw_signature),
        )
        assert res.status_code == 200

        journal = app.dependency_overrides[get_journal]()
        ev = journal.query(action=ACTION_WIDGET_COOCCURRENCE_ACCEPTED)[-1]
        assert ev.payload["signature"] == raw_signature
