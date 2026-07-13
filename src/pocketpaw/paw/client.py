# paw/client.py — PawClient, a thin synchronous httpx client for the PocketPaw
# cloud REST API (paw-cli C1 — the external programmatic surface).
# Created: 2026-07-11 (feat/paw-cli) — wraps the EXISTING /api/v1/fabric route
# contracts only (ee/pocketpaw_ee/fabric/router.py); no invented endpoints.
# Auth is a bearer token (a `paw_...` API key or a login JWT — both ride the
# same Authorization header). A custom httpx transport can be injected so tests
# and embedders stub the wire without patching.

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT = 30.0

_API_PREFIX = "/api/v1"


class PawAPIError(Exception):
    """A non-2xx response from the PocketPaw API.

    Carries ``status_code`` and the response ``detail`` (the FastAPI error
    body's ``detail`` field when present, else the raw text) so callers and
    the CLI can render the server's actual reason.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class PawClient:
    """Thin synchronous client for the PocketPaw cloud REST API.

    Mirrors the existing router contracts (fabric ontology today) — each
    method maps 1:1 onto a mounted ``/api/v1`` route and returns the decoded
    JSON body. Workspace scoping is server-side (the token's active
    workspace); the client never sends a workspace id.

    Args:
        base_url: server origin, e.g. ``https://cloud.example.com``.
        api_key: bearer credential (``paw_...`` API key or JWT). Optional so
            unauthenticated endpoints (``/openapi.json``) still work.
        timeout: per-request timeout in seconds.
        transport: optional ``httpx.BaseTransport`` for tests/embedders
            (e.g. ``httpx.MockTransport``).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PawClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one request against the API prefix and decode the JSON body.

        Raises :class:`PawAPIError` on any non-2xx status, with the FastAPI
        ``detail`` extracted when the body is JSON.
        """
        resp = self._client.request(method, f"{_API_PREFIX}{path}", **kwargs)
        if resp.is_error:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001 — non-JSON error body
                detail = resp.text
            raise PawAPIError(resp.status_code, str(detail))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- meta ----------------------------------------------------------------

    def openapi(self) -> dict[str, Any]:
        """Fetch the live OpenAPI schema (unauthenticated; contract anchor)."""
        resp = self._client.get("/openapi.json")
        if resp.is_error:
            raise PawAPIError(resp.status_code, resp.text)
        return resp.json()

    # -- fabric: reads --------------------------------------------------------

    def fabric_stats(self) -> dict[str, Any]:
        """GET /fabric/stats — ontology counts for the token's workspace."""
        return self._request("GET", "/fabric/stats")

    def list_types(self) -> list[dict[str, Any]]:
        """GET /fabric/types — object types visible to the workspace."""
        return self._request("GET", "/fabric/types")

    def get_schema(self) -> dict[str, Any]:
        """GET /fabric/schema — object types + declared link types."""
        return self._request("GET", "/fabric/schema")

    def list_objects(
        self,
        *,
        type_id: str | None = None,
        type_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /fabric/objects — paged object listing with type filters."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if type_id:
            params["type_id"] = type_id
        if type_name:
            params["type_name"] = type_name
        return self._request("GET", "/fabric/objects", params=params)

    def get_object(self, obj_id: str) -> dict[str, Any]:
        """GET /fabric/objects/{id} — one object (404 -> PawAPIError)."""
        return self._request("GET", f"/fabric/objects/{obj_id}")

    def query(
        self,
        *,
        type_name: str | None = None,
        linked_to: str | None = None,
        link_type: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """POST /fabric/query — run a FabricQuery (same body the router takes)."""
        body: dict[str, Any] = {"limit": limit}
        if type_name:
            body["type_name"] = type_name
        if linked_to:
            body["linked_to"] = linked_to
        if link_type:
            body["link_type"] = link_type
        if filters:
            body["filters"] = filters
        return self._request("POST", "/fabric/query", json=body)

    def list_links(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        link_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /fabric/links — paged link listing with endpoint/type filters."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if from_id:
            params["from_id"] = from_id
        if to_id:
            params["to_id"] = to_id
        if link_type:
            params["link_type"] = link_type
        return self._request("GET", "/fabric/links", params=params)

    # -- fabric: writes -------------------------------------------------------

    def create_object(
        self,
        type_id: str,
        properties: dict[str, Any] | None = None,
        *,
        source_connector: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /fabric/objects — create one object of an existing type."""
        return self._request(
            "POST",
            "/fabric/objects",
            json={
                "type_id": type_id,
                "properties": properties or {},
                "source_connector": source_connector,
                "source_id": source_id,
            },
        )

    def create_link(
        self,
        from_id: str,
        to_id: str,
        link_type: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /fabric/links — link two objects."""
        return self._request(
            "POST",
            "/fabric/links",
            json={
                "from_id": from_id,
                "to_id": to_id,
                "link_type": link_type,
                "properties": properties or {},
            },
        )

    def delete_link(self, link_id: str) -> None:
        """DELETE /fabric/links/{id} — remove one link (workspace-scoped)."""
        self._request("DELETE", f"/fabric/links/{link_id}")

    def update_type(
        self,
        type_id: str,
        *,
        properties: list[dict[str, Any]] | None = None,
        renames: dict[str, str] | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /fabric/schema/types/{id} — version + migrate a type (ADMIN).

        Rename and additive changes only — a dropped property is deferred
        server-side (its orphaned key stays on existing objects).
        """
        body: dict[str, Any] = {}
        if properties is not None:
            body["properties"] = properties
        if renames is not None:
            body["renames"] = renames
        if description is not None:
            body["description"] = description
        if icon is not None:
            body["icon"] = icon
        if color is not None:
            body["color"] = color
        return self._request("PATCH", f"/fabric/schema/types/{type_id}", json=body)


__all__ = ["DEFAULT_BASE_URL", "PawAPIError", "PawClient"]
