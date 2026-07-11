# tests/test_paw_client.py — PawClient, the thin httpx client for the cloud
# REST API (paw-cli C1).
# Created: 2026-07-11 (feat/paw-cli).
# What this pins, all against httpx.MockTransport (no live server, no mocking
# of the seam under test — the real client code runs down to the transport):
#   * requests hit the exact /api/v1 fabric route contracts (path, method,
#     params, JSON body) the EE router mounts.
#   * the api_key rides as an Authorization: Bearer header; absent when unset.
#   * non-2xx responses raise PawAPIError carrying the FastAPI `detail`.
#   * delete_link returns None on an empty body.

from __future__ import annotations

import json

import httpx
import pytest

from pocketpaw.paw.client import PawAPIError, PawClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_client(handler) -> PawClient:
    """PawClient wired to a MockTransport handler."""
    return PawClient(
        "http://testserver",
        api_key="paw_test_key",
        transport=httpx.MockTransport(handler),
    )


def capture(response_json, status_code: int = 200):
    """Return (requests_list, handler) — the handler records every request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, json=response_json)

    return seen, handler


# ---------------------------------------------------------------------------
# Route contracts
# ---------------------------------------------------------------------------


def test_fabric_stats_hits_route_with_bearer():
    seen, handler = capture({"types": 1, "objects": 2, "links": 0})
    with make_client(handler) as client:
        body = client.fabric_stats()

    assert body == {"types": 1, "objects": 2, "links": 0}
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/fabric/stats"
    assert req.headers["Authorization"] == "Bearer paw_test_key"


def test_no_api_key_sends_no_auth_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    with PawClient("http://testserver", transport=httpx.MockTransport(handler)) as client:
        client.list_types()

    assert "Authorization" not in seen[0].headers


def test_list_types_route():
    seen, handler = capture([{"name": "Customer"}])
    with make_client(handler) as client:
        assert client.list_types() == [{"name": "Customer"}]
    assert seen[0].url.path == "/api/v1/fabric/types"


def test_query_posts_fabric_query_body():
    seen, handler = capture({"objects": [], "total": 0})
    with make_client(handler) as client:
        client.query(type_name="Customer", filters={"status": "active"}, limit=5)

    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/fabric/query"
    body = json.loads(req.content)
    assert body == {"limit": 5, "type_name": "Customer", "filters": {"status": "active"}}


def test_list_objects_passes_filter_params():
    seen, handler = capture({"objects": [], "total": 0})
    with make_client(handler) as client:
        client.list_objects(type_name="Order", limit=10, offset=20)

    params = dict(seen[0].url.params)
    assert params == {"type_name": "Order", "limit": "10", "offset": "20"}


def test_list_links_passes_endpoint_filters():
    seen, handler = capture({"links": [], "total": 0})
    with make_client(handler) as client:
        client.list_links(from_id="a", link_type="has_order")

    req = seen[0]
    assert req.url.path == "/api/v1/fabric/links"
    params = dict(req.url.params)
    assert params["from_id"] == "a"
    assert params["link_type"] == "has_order"
    assert "to_id" not in params


def test_create_object_posts_body():
    seen, handler = capture({"id": "obj-1"}, status_code=201)
    with make_client(handler) as client:
        client.create_object("type-1", {"name": "Acme"})

    body = json.loads(seen[0].content)
    assert body["type_id"] == "type-1"
    assert body["properties"] == {"name": "Acme"}


def test_create_link_posts_body():
    seen, handler = capture({"id": "lnk-1"}, status_code=201)
    with make_client(handler) as client:
        client.create_link("a", "b", "has_order")

    req = seen[0]
    assert req.url.path == "/api/v1/fabric/links"
    body = json.loads(req.content)
    assert body == {"from_id": "a", "to_id": "b", "link_type": "has_order", "properties": {}}


def test_delete_link_hits_route_and_returns_none():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    with make_client(handler) as client:
        assert client.delete_link("lnk-1") is None

    req = seen[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/fabric/links/lnk-1"


def test_update_type_patches_schema_route():
    seen, handler = capture({"id": "type-1", "version": 2})
    with make_client(handler) as client:
        client.update_type("type-1", renames={"old": "new"})

    req = seen[0]
    assert req.method == "PATCH"
    assert req.url.path == "/api/v1/fabric/schema/types/type-1"
    assert json.loads(req.content) == {"renames": {"old": "new"}}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_error_response_raises_with_detail():
    _, handler = capture({"detail": "Object not found"}, status_code=404)
    with make_client(handler) as client, pytest.raises(PawAPIError) as exc_info:
        client.get_object("missing")

    assert exc_info.value.status_code == 404
    assert "Object not found" in exc_info.value.detail


def test_non_json_error_body_is_relayed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with make_client(handler) as client, pytest.raises(PawAPIError) as exc_info:
        client.fabric_stats()

    assert exc_info.value.status_code == 502
    assert "bad gateway" in exc_info.value.detail
