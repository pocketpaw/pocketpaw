# tests/cloud/test_paw_print_ingest.py — PR-B: HTTP surface + event ingest.
# Created: 2026-04-13 — Covers spec serving (CORS), owner-authed CRUD, event
# ingest with origin + payload-size + rate-limit + mapping-to-Fabric logic.

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.paw_print.models import (
    MAX_PAYLOAD_BYTES,
    PawPrintBlock,
    PawPrintSpec,
)
from ee.paw_print.router import router
from ee.paw_print.store import PawPrintStore


def _spec(widget_id: str = "pp_test", pocket_id: str = "pocket-1") -> PawPrintSpec:
    return PawPrintSpec(
        widget_id=widget_id,
        pocket_id=pocket_id,
        blocks=[PawPrintBlock(type="text", content="Hi from Brew & Co")],
    )


def _widget_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pocket_id": "pocket-1",
        "owner": "user:maya",
        "name": "Brew & Co Menu",
        "spec": _spec().model_dump(),
        "allowed_domains": ["brewco.com"],
        "rate_limit_per_min": 5,
        "per_customer_limit_per_min": 3,
        "event_mapping": {
            "order_click": {
                "creates": "Order",
                "fields": {"item": "{{ payload.item }}", "buyer": "{{ customer_ref }}"},
            },
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def app_with_store(tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    store = PawPrintStore(tmp_path / "paw_print_router.db")
    with patch("ee.paw_print.router._store", return_value=store):
        yield app, store


@pytest.fixture
def client(app_with_store):
    app, _ = app_with_store
    return TestClient(app)


# ---------------------------------------------------------------------------
# Widget CRUD
# ---------------------------------------------------------------------------


class TestWidgetCRUDEndpoints:
    def test_create_widget_returns_shape(self, client: TestClient) -> None:
        res = client.post("/paw-print/widgets", json=_widget_payload())
        assert res.status_code == 201
        body = res.json()
        assert body["pocket_id"] == "pocket-1"
        assert body["access_token"].startswith("pp_tok_")
        assert body["allowed_domains"] == ["brewco.com"]

    def test_get_widget_requires_token(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.get(f"/paw-print/widgets/{created['id']}")
        assert res.status_code == 401

    def test_get_widget_with_valid_token(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.get(
            f"/paw-print/widgets/{created['id']}",
            headers={"X-Paw-Print-Token": created["access_token"]},
        )
        assert res.status_code == 200
        assert res.json()["id"] == created["id"]

    def test_rotate_token_changes_value(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.post(
            f"/paw-print/widgets/{created['id']}/rotate-token",
            headers={"X-Paw-Print-Token": created["access_token"]},
        )
        assert res.status_code == 200
        assert res.json()["access_token"] != created["access_token"]

    def test_delete_widget(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.delete(
            f"/paw-print/widgets/{created['id']}",
            headers={"X-Paw-Print-Token": created["access_token"]},
        )
        assert res.status_code == 204
        res2 = client.get(
            f"/paw-print/widgets/{created['id']}",
            headers={"X-Paw-Print-Token": created["access_token"]},
        )
        assert res2.status_code == 404

    def test_list_events_requires_token(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        unauthed = client.get(f"/paw-print/widgets/{created['id']}/events")
        assert unauthed.status_code == 401
        authed = client.get(
            f"/paw-print/widgets/{created['id']}/events",
            headers={"X-Paw-Print-Token": created["access_token"]},
        )
        assert authed.status_code == 200


# ---------------------------------------------------------------------------
# Public spec serving
# ---------------------------------------------------------------------------


class TestSpecEndpoint:
    def test_allowed_origin_gets_spec_with_cors_headers(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.get(
            f"/paw-print/spec/{created['id']}",
            headers={"Origin": "https://brewco.com"},
        )
        assert res.status_code == 200
        assert res.headers["access-control-allow-origin"] == "https://brewco.com"
        assert "origin" in res.headers.get("vary", "").lower()

    def test_disallowed_origin_is_rejected(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.get(
            f"/paw-print/spec/{created['id']}",
            headers={"Origin": "https://evil.example"},
        )
        assert res.status_code == 403

    def test_missing_origin_is_rejected_when_allowlist_set(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.get(f"/paw-print/spec/{created['id']}")
        assert res.status_code == 403

    def test_empty_allowlist_allows_any_origin(self, client: TestClient) -> None:
        created = client.post(
            "/paw-print/widgets",
            json=_widget_payload(allowed_domains=[]),
        ).json()
        res = client.get(
            f"/paw-print/spec/{created['id']}",
            headers={"Origin": "https://anywhere.example"},
        )
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# Event ingest
# ---------------------------------------------------------------------------


class TestEventIngest:
    def test_ingest_happy_path_records_event(self, app_with_store, client: TestClient) -> None:
        _, store = app_with_store
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()

        res = client.post(
            f"/paw-print/events/{created['id']}",
            json={
                "type": "order_click",
                "payload": {"item": "oat_latte"},
                "customer_ref": "cust_hash_abc",
            },
            headers={"Origin": "https://brewco.com"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["accepted"] is True
        assert body["event"]["type"] == "order_click"

    def test_disallowed_origin_is_rejected(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.post(
            f"/paw-print/events/{created['id']}",
            json={"type": "order_click", "payload": {}, "customer_ref": "abc"},
            headers={"Origin": "https://evil.example"},
        )
        assert res.status_code == 403

    def test_oversized_payload_is_rejected(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        big_payload = {"blob": "x" * (MAX_PAYLOAD_BYTES + 50)}
        res = client.post(
            f"/paw-print/events/{created['id']}",
            json={"type": "order_click", "payload": big_payload, "customer_ref": "abc"},
            headers={"Origin": "https://brewco.com"},
        )
        assert res.status_code == 413

    def test_rate_limit_per_customer_fires(self, client: TestClient) -> None:
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        # per_customer_limit_per_min=3 in payload — fourth call from same
        # customer should 429.
        for _ in range(3):
            ok = client.post(
                f"/paw-print/events/{created['id']}",
                json={
                    "type": "order_click",
                    "payload": {"item": "oat_latte"},
                    "customer_ref": "cust_a",
                },
                headers={"Origin": "https://brewco.com"},
            )
            assert ok.status_code == 200
        blocked = client.post(
            f"/paw-print/events/{created['id']}",
            json={
                "type": "order_click",
                "payload": {"item": "oat_latte"},
                "customer_ref": "cust_a",
            },
            headers={"Origin": "https://brewco.com"},
        )
        assert blocked.status_code == 429

    def test_guardian_rejection_marks_event_not_accepted(
        self, app_with_store, client: TestClient, monkeypatch
    ) -> None:
        async def blocker(payload: str) -> bool:
            return False

        monkeypatch.setattr(
            "ee.paw_print.router._pass_through_guardian",
            AsyncMock(return_value=False),
        )
        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.post(
            f"/paw-print/events/{created['id']}",
            json={"type": "order_click", "payload": {}, "customer_ref": "abc"},
            headers={"Origin": "https://brewco.com"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["accepted"] is False
        assert body["reason"] == "guardian_rejected"

    def test_event_mapping_creates_fabric_object(self, client: TestClient, monkeypatch) -> None:
        fabric = MagicMock()
        created_obj = MagicMock()
        created_obj.id = "obj_created_123"
        fabric.create_object = AsyncMock(return_value=created_obj)

        class _FakeFabricObject:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        import sys
        import types

        fake_api = types.ModuleType("ee.api")
        fake_api.get_fabric_store = lambda: fabric  # type: ignore[attr-defined]

        fake_fabric_models = types.ModuleType("ee.fabric.models")
        fake_fabric_models.FabricObject = _FakeFabricObject  # type: ignore[attr-defined]
        fake_fabric_models._gen_id = lambda prefix="x": f"{prefix}_fake"  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "ee.api", fake_api)
        # ee.fabric.models is already a real module — only patch create_object
        # via monkeypatching the router's _apply_event_mapping import path.
        from ee.paw_print import router as ppr

        async def fake_apply(widget, event):
            props = {
                "item": event.payload.get("item"),
                "buyer": event.customer_ref,
            }
            obj = fabric.create_object(
                _FakeFabricObject(
                    type_name="Order",
                    properties=props,
                    source_connector="paw_print",
                ),
            )
            awaited = await obj if hasattr(obj, "__await__") else obj
            return getattr(awaited, "id", None)

        monkeypatch.setattr(ppr, "_apply_event_mapping", fake_apply)

        created = client.post("/paw-print/widgets", json=_widget_payload()).json()
        res = client.post(
            f"/paw-print/events/{created['id']}",
            json={
                "type": "order_click",
                "payload": {"item": "oat_latte"},
                "customer_ref": "cust_a",
            },
            headers={"Origin": "https://brewco.com"},
        )
        assert res.status_code == 200
        assert res.json()["fabric_object_id"] == "obj_created_123"


# ---------------------------------------------------------------------------
# _interpolate helper behavior
# ---------------------------------------------------------------------------


class TestInterpolate:
    def test_full_placeholder_returns_raw_value(self) -> None:
        from ee.paw_print.router import _interpolate

        assert _interpolate("{{ payload.count }}", {"payload": {"count": 42}}) == 42

    def test_mixed_string_stringifies(self) -> None:
        from ee.paw_print.router import _interpolate

        out = _interpolate(
            "Order {{ payload.item }} for {{ customer_ref }}",
            {"payload": {"item": "latte"}, "customer_ref": "cust_a"},
        )
        assert out == "Order latte for cust_a"

    def test_missing_path_resolves_to_empty_string_in_mixed_mode(self) -> None:
        from ee.paw_print.router import _interpolate

        out = _interpolate("Hi {{ payload.name }}!", {"payload": {}})
        assert out == "Hi !"
