# DirectREST YAML engine — reads connector YAML definitions and executes REST actions.
# Created: 2026-03-27 — Primary adapter. One YAML per service.
# Updated: 2026-03-28 — Real HTTP execution via httpx (was placeholder).

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorStatus,
    SyncResult,
    TrustLevel,
)


@dataclass
class ConnectorDef:
    """Parsed connector YAML definition."""

    name: str
    display_name: str
    type: str = "generic"
    icon: str = "plug"
    auth: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    sync: dict[str, Any] = field(default_factory=dict)


def parse_connector_yaml(path: Path) -> ConnectorDef:
    """Parse a connector YAML file into a ConnectorDef."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    return ConnectorDef(
        name=raw.get("name", path.stem),
        display_name=raw.get("display_name", raw.get("name", path.stem)),
        type=raw.get("type", "generic"),
        icon=raw.get("icon", "plug"),
        auth=raw.get("auth", {}),
        actions=raw.get("actions", []),
        sync=raw.get("sync", {}),
    )


class DirectRESTAdapter:
    """Connector adapter that reads YAML definitions and executes REST actions.

    Each YAML file defines one service (Stripe, Square, etc.) with:
    - auth config (api_key, oauth, basic, bearer)
    - actions (REST endpoints with params and response schemas)
    - sync config (table mapping, schedule)
    """

    def __init__(self, definition: ConnectorDef) -> None:
        self._def = definition
        self._credentials: dict[str, str] = {}
        self._connected = False

    @property
    def name(self) -> str:
        return self._def.name

    @property
    def display_name(self) -> str:
        return self._def.display_name

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        """Store credentials and mark as connected."""
        # Extract credentials from config
        for cred in self._def.auth.get("credentials", []):
            key = cred["name"]
            if key in config:
                self._credentials[key] = config[key]
            elif cred.get("required", False):
                return ConnectionResult(
                    success=False,
                    connector_name=self.name,
                    status=ConnectorStatus.ERROR,
                    message=f"Missing required credential: {key}",
                )

        self._connected = True
        tables = []
        if self._def.sync.get("table"):
            tables.append(self._def.sync["table"])

        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message=f"Connected to {self.display_name}",
            tables_created=tables,
        )

    async def disconnect(self, pocket_id: str) -> bool:
        self._credentials.clear()
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        """Convert YAML action definitions to ActionSchema list."""
        schemas = []
        for act in self._def.actions:
            params = {}
            for key, val in act.get("params", {}).items():
                params[key] = val
            for key, val in act.get("body", {}).items():
                params[key] = val

            schemas.append(
                ActionSchema(
                    name=act["name"],
                    description=act.get("description", ""),
                    method=act.get("method", "GET"),
                    parameters=params,
                    trust_level=TrustLevel(act.get("trust_level", "confirm")),
                )
            )
        return schemas

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        """Execute a REST action via httpx."""
        if not self._connected:
            return ActionResult(success=False, error="Not connected")

        act_def = None
        for a in self._def.actions:
            if a["name"] == action:
                act_def = a
                break

        if not act_def:
            return ActionResult(success=False, error=f"Unknown action: {action}")

        method = act_def.get("method", "GET").upper()
        url = act_def.get("url", "")

        # Substitute {placeholder} in URL templates — check credentials first, then params
        if url:
            import re

            for placeholder in re.findall(r"\{(\w+)\}", url):
                if placeholder in self._credentials:
                    url = url.replace(f"{{{placeholder}}}", self._credentials[placeholder])
                elif placeholder in params:
                    url = url.replace(f"{{{placeholder}}}", str(params.pop(placeholder)))

        # If no hardcoded URL, build from BASE_URL credential + path param
        if not url and method != "LOCAL":
            base = self._credentials.get("BASE_URL", "")
            path = params.pop("path", "")
            if base and path:
                url = base.rstrip("/") + "/" + path.lstrip("/")

        # LOCAL actions (CSV import etc.) don't make HTTP calls
        if method == "LOCAL":
            return ActionResult(success=True, data={"action": action, "params": params})

        if not url:
            return ActionResult(success=False, error=f"No URL defined for action: {action}")

        # Build auth headers
        headers = self._build_auth_headers()

        # Separate query params from body params
        query_params = {}
        body_data = {}
        param_defs = act_def.get("params", {})
        body_defs = act_def.get("body", {})

        for key, val in params.items():
            if key in body_defs:
                body_data[key] = val
            elif key in param_defs:
                query_params[key] = val
            else:
                # Unknown param — put in query for GET, body for POST
                if method in ("GET", "DELETE"):
                    query_params[key] = val
                else:
                    body_data[key] = val

        try:
            import httpx

            # Detect form-encoded APIs (Stripe, etc.) from URL or content_type hint
            content_type = act_def.get("content_type", "")
            use_form = content_type == "form" or "stripe.com" in url

            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    resp = await client.get(url, params=query_params, headers=headers)
                elif method == "POST":
                    if use_form:
                        resp = await client.post(
                            url, data=body_data, params=query_params, headers=headers
                        )
                    else:
                        resp = await client.post(
                            url, json=body_data, params=query_params, headers=headers
                        )
                elif method == "PUT":
                    if use_form:
                        resp = await client.put(
                            url, data=body_data, params=query_params, headers=headers
                        )
                    else:
                        resp = await client.put(
                            url, json=body_data, params=query_params, headers=headers
                        )
                elif method == "PATCH":
                    if use_form:
                        resp = await client.patch(
                            url, data=body_data, params=query_params, headers=headers
                        )
                    else:
                        resp = await client.patch(
                            url, json=body_data, params=query_params, headers=headers
                        )
                elif method == "DELETE":
                    resp = await client.delete(url, params=query_params, headers=headers)
                else:
                    return ActionResult(success=False, error=f"Unsupported method: {method}")

                resp.raise_for_status()
                data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else resp.text
                )

                # Count records — handle wrapped responses (Stripe: {data: [...]})
                if isinstance(data, list):
                    records = len(data)
                elif isinstance(data, dict) and isinstance(data.get("data"), list):
                    records = len(data["data"])
                else:
                    records = 1

                return ActionResult(success=True, data=data, records_affected=records)

        except httpx.HTTPStatusError as e:
            return ActionResult(
                success=False, error=f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except httpx.RequestError as e:
            return ActionResult(success=False, error=f"Request failed: {e}")
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    def _build_auth_headers(self) -> dict[str, str]:
        """Build auth headers from stored credentials based on auth method."""
        # Start with default headers from connector definition
        headers: dict[str, str] = dict(self._def.auth.get("headers", {}))
        auth_method = self._def.auth.get("method", "none")

        if auth_method == "api_key":
            # Find the first credential and use as Bearer token
            for cred in self._def.auth.get("credentials", []):
                key = cred["name"]
                if key in self._credentials:
                    headers["Authorization"] = f"Bearer {self._credentials[key]}"
                    break
        elif auth_method == "bearer":
            for cred in self._def.auth.get("credentials", []):
                if cred["name"].endswith("TOKEN") or cred["name"].endswith("KEY"):
                    val = self._credentials.get(cred["name"], "")
                    if val:
                        headers["Authorization"] = f"Bearer {val}"
                        break
        elif auth_method == "basic":
            import base64

            username = self._credentials.get("username", "")
            password = self._credentials.get("password", "")
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        return headers

    async def sync(self, pocket_id: str) -> SyncResult:
        """Sync data from the external service into pocket.db."""
        if not self._connected:
            return SyncResult(success=False, connector_name=self.name, error="Not connected")

        if not self._def.sync:
            return SyncResult(success=False, connector_name=self.name, error="No sync config")

        # In production: call the list action, map response to pocket.db table
        return SyncResult(
            success=True,
            connector_name=self.name,
            records_synced=0,
        )

    async def schema(self) -> dict[str, Any]:
        """Return the sync table schema."""
        return {
            "table": self._def.sync.get("table", f"{self.name}_data"),
            "mapping": self._def.sync.get("mapping", {}),
            "schedule": self._def.sync.get("schedule", "manual"),
        }
