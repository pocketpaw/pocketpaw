# DirectREST YAML engine — reads connector YAML definitions and executes REST actions.
# Created: 2026-03-27 — Primary adapter. One YAML per service.
# Updated: 2026-03-28 — Real HTTP execution via httpx (was placeholder).
# Updated: 2026-06-07 (M3 connector→skill auto-authoring) — ConnectorDef grows an
#   optional ``surface_profile`` block (skill / allow_tools / deny_tools) parsed
#   from the YAML. It is the per-connector mapping source for deriving a pocket's
#   PocketSurfaceProfile when the connector is bound to a pocket. Backward-compat:
#   connectors with no block parse with ``surface_profile=None``.
# Updated: 2026-06-08 — Added `senses: list[str]` to ConnectorDef + parse-time
#   validation via senses.validate_sense_id (Sense tier chunk 1). Unknown paw.*
#   ids fail loudly; missing `senses:` key is backward compatible ([]).
# Updated: 2026-06-11 — Cookie/session auth + a persistent HTTP client.
#   _build_auth_headers gains two additive methods: `cookie` (emits a Cookie:
#   header from a declared credential, name set via auth.credential) and
#   `header` (emits an arbitrary header named by auth.header from a credential —
#   the escape hatch for APIs whose key is not a Bearer token, fixing the
#   api_key-always-Bearer trap without touching api_key). execute() now reuses a
#   lazily-built httpx.AsyncClient per adapter instance (connection pooling +
#   cookie jar) instead of opening a fresh client per call; disconnect() closes
#   it. Existing auth methods, timeouts, and error mapping are unchanged.
# Updated: 2026-06-28 (AW-1 connector egress guard) — execute() now routes its
#   outbound HTTP through the SSRF egress guard when the
#   POCKETPAW_CONNECTOR_EGRESS_GUARD flag is on. Before each request it calls
#   ``assert_egress_allowed(url, allowed_hosts)`` (https-only, no
#   userinfo/fragment, host must be on the allow-list, DNS pre-resolve +
#   internal-range reject) and dials the result through a pinned-IP client
#   (``PinnedTransport``, ``follow_redirects=False``) so the connection cannot
#   be re-resolved to an internal address between check and connect (DNS-rebind
#   TOCTOU). A rejection returns a clean ActionResult error. The flag defaults
#   OFF (safe rollout) — with it off the pooled client path is byte-for-byte
#   unchanged. The guard primitive lives in the OSS module
#   ``security.url_validators`` (the OSS->EE import boundary forbids importing
#   the EE ``_http_guard``); the EE guard re-exports it to stay canonical.
# Updated: 2026-06-28 (AW-2 multi-host allow-list + concern fixes) —
#   * ConnectorDef grows ``allowed_hosts: list[str]`` parsed from a top-level
#     ``allowed_hosts:`` YAML key (explicit operator additions).
#   * The effective per-call allow-list is now built from THREE sources, not
#     just the request host: every action's declared base-URL host (resolved
#     through the SAME ``{template}`` substitution execute() applies, so
#     ``{FRESHDESK_DOMAIN}.freshdesk.com`` and ``{CONFLUENCE_BASE_URL}`` resolve
#     to the real runtime host), the auth-endpoint host (``auth.auth_url`` /
#     ``auth.token_url`` — some connectors authenticate on a different host),
#     and the explicit ``allowed_hosts``. Hosts are normalized (lowercase,
#     IPv6 brackets stripped). The RESOLVED request host is checked against this
#     set — never a template string — so a templated/dynamic base URL is vetted
#     by its real host at call time. IP-literal and IPv6 base URLs flow through
#     unchanged (urlsplit handles the brackets; the resolved-IP internal check
#     still applies).
#   * Concern fix 1 (fail-CLOSED on config error): ``_egress_guard_enabled``
#     no longer swallows a settings-load error into "guard off". A settings
#     failure now logs at error level and FAILS CLOSED (returns True → the
#     guard runs) so a malformed settings load cannot silently re-open the
#     SSRF bypass. The dev escape ``POCKETPAW_ALLOW_INTERNAL_URLS`` still lets
#     localhost connectors through when the guard runs.
#   * Concern fix 2 (preserve the cookie jar): pinned clients are now CACHED
#     per resolved host (``_pinned_clients``) instead of built fresh per
#     request, so the persistent cookie jar survives across calls — session /
#     cookie-auth connectors keep working under the guard. The cache is closed
#     alongside the pooled client in disconnect().

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


@dataclass(frozen=True)
class ConnectorSurfaceProfile:
    """The surface-profile contribution a connector makes to a pocket.

    Parsed from the OPTIONAL ``surface_profile:`` block on a connector YAML.
    When a connector is bound to a pocket (scope=pocket), the cloud derivation
    helper folds every enabled connector's ``ConnectorSurfaceProfile`` into the
    pocket's ``PocketSurfaceProfile`` (skill_names + tool allow/deny). A
    connector with no block contributes nothing (``ConnectorDef.surface_profile``
    is ``None``).

    Fields (all optional):
      * ``skill`` — a single skill name to load for rooms with this connector.
      * ``allow_tools`` — tool-id glob/patterns to add to the SDK allowlist.
      * ``deny_tools`` — tool-id glob/patterns to deny.
    """

    skill: str | None = None
    allow_tools: tuple[str, ...] = field(default_factory=tuple)
    deny_tools: tuple[str, ...] = field(default_factory=tuple)


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
    # M3 — optional connector→skill/tool mapping. ``None`` when the YAML has
    # no ``surface_profile:`` block (the common case / backward-compat).
    surface_profile: ConnectorSurfaceProfile | None = None
    # Sense tier — the provider-agnostic capabilities this connector fills
    # (e.g. ["paw.email.v1"]). Empty when the YAML has no ``senses:`` key.
    senses: list[str] = field(default_factory=list)
    # AW-2 — explicit egress allow-list additions from a top-level
    # ``allowed_hosts:`` YAML key. These are ADDED on top of the auto-seeded
    # hosts (declared base-URL host + auth-endpoint host); they never narrow
    # the list. Empty when the YAML has no ``allowed_hosts:`` key. The effective
    # per-call allow-list is computed in DirectRESTAdapter._effective_allowed_hosts.
    allowed_hosts: list[str] = field(default_factory=list)


def _parse_surface_profile(raw: Any) -> ConnectorSurfaceProfile | None:
    """Parse the optional ``surface_profile:`` YAML block.

    Returns ``None`` when the block is absent or not a mapping, so connectors
    without the block stay byte-identical to pre-M3 behavior. Tolerates a bare
    string or a list for ``skill`` only by ignoring non-string values; the
    canonical shape is a mapping with ``skill`` / ``allow_tools`` / ``deny_tools``.
    """
    if not isinstance(raw, dict):
        return None
    skill = raw.get("skill")
    skill = skill if isinstance(skill, str) and skill else None
    allow = raw.get("allow_tools") or []
    deny = raw.get("deny_tools") or []
    allow_tuple = tuple(str(t) for t in allow if t) if isinstance(allow, list) else ()
    deny_tuple = tuple(str(t) for t in deny if t) if isinstance(deny, list) else ()
    if skill is None and not allow_tuple and not deny_tuple:
        return None
    return ConnectorSurfaceProfile(skill=skill, allow_tools=allow_tuple, deny_tools=deny_tuple)


def parse_connector_yaml(path: Path) -> ConnectorDef:
    """Parse a connector YAML file into a ConnectorDef."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    name = raw.get("name", path.stem)

    # Validate any declared senses at parse time. An unknown paw.* id must fail
    # loudly with a message naming the connector + the bad id — this is the
    # "no fragmentation of the core" rule (Sense tier chunk 1).
    from pocketpaw.senses import SenseValidationError, validate_sense_id

    senses = raw.get("senses", [])
    for sense_id in senses:
        try:
            validate_sense_id(sense_id)
        except SenseValidationError as e:
            raise SenseValidationError(
                f"connector {name!r} declares invalid sense {sense_id!r}: {e}"
            ) from e

    # AW-2 — explicit egress allow-list additions. Tolerate a missing key (the
    # common case) and a non-list value (ignored) so connectors without the key
    # parse byte-identically. Each entry is coerced to a stripped string.
    raw_allowed = raw.get("allowed_hosts", [])
    allowed_hosts = (
        [str(h).strip() for h in raw_allowed if str(h).strip()]
        if isinstance(raw_allowed, list)
        else []
    )

    return ConnectorDef(
        name=name,
        display_name=raw.get("display_name", raw.get("name", path.stem)),
        type=raw.get("type", "generic"),
        icon=raw.get("icon", "plug"),
        auth=raw.get("auth", {}),
        actions=raw.get("actions", []),
        sync=raw.get("sync", {}),
        surface_profile=_parse_surface_profile(raw.get("surface_profile")),
        senses=senses,
        allowed_hosts=allowed_hosts,
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
        # Persistent HTTP client — built lazily on first execute() and reused
        # across calls so connections pool and any Set-Cookie response is kept
        # in the client's cookie jar for the next request. ``Any`` avoids a
        # module-level httpx import (it stays inside execute()/_get_client).
        self._client: Any | None = None
        # AW-2 concern fix 2 — per-resolved-host pinned clients. Building a
        # fresh PinnedTransport client per request (AW-1) dropped the cookie
        # jar, breaking session/cookie-auth connectors under the guard. Caching
        # one pinned client per resolved host preserves that host's cookie jar
        # across calls while keeping the IP pin (and DNS-rebind safety) intact.
        # Closed alongside ``_client`` in disconnect().
        self._pinned_clients: dict[str, Any] = {}
        # AW-2 — per-workspace egress allow-list additions, populated by
        # connect() from the config blob (WorkspaceConnector.allowed_hosts).
        # Layered on top of the YAML-declared hosts in _effective_allowed_hosts.
        self._ws_allowed_hosts: list[str] = []

    async def _get_client(self) -> Any:
        """Return the per-adapter httpx.AsyncClient, building it on first use."""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @staticmethod
    def _egress_guard_enabled() -> bool:
        """Whether to route this request through the SSRF egress guard.

        Returns the ``connector_egress_guard`` settings flag (default False).

        AW-2 concern fix 1 — FAIL CLOSED, not open. AW-1 swallowed any
        ``get_settings()`` error into ``False`` (guard off), so a malformed
        settings load would silently re-open the SSRF bypass the guard exists to
        close. Here a settings-load failure is logged at error level and FAILS
        CLOSED (returns True → the guard runs). The cost of a false-closed is a
        blocked connector call with a clear error; the cost of a false-open is a
        silent SSRF hole, so closed is the safe default. The dev escape
        ``POCKETPAW_ALLOW_INTERNAL_URLS`` still lets localhost through when the
        guard runs, so local development is not broken by failing closed.
        """
        try:
            from pocketpaw.config import get_settings

            return bool(get_settings().connector_egress_guard)
        except Exception:  # noqa: BLE001 — a config-load failure must not silently disable the guard
            import logging

            logging.getLogger(__name__).error(
                "connector egress guard: settings load failed; FAILING CLOSED "
                "(routing through the guard) so the SSRF bypass cannot silently re-open",
                exc_info=True,
            )
            return True

    def _substitute_host_template(self, value: str) -> str:
        """Apply the SAME ``{placeholder}`` substitution execute() applies to a
        declared URL, so a templated base URL resolves to its real runtime host.

        Mirrors execute()'s loop: a ``{placeholder}`` is filled from a stored
        credential first, then nothing else (params are per-call and not known
        when seeding the static allow-list — the request host itself covers the
        param-templated case at check time). An unresolved placeholder is left
        as-is; its host is dropped by the caller (it can't be a real hostname).
        """
        import re

        out = value
        for placeholder in re.findall(r"\{(\w+)\}", out):
            if placeholder in self._credentials:
                out = out.replace(f"{{{placeholder}}}", self._credentials[placeholder])
        return out

    def _effective_allowed_hosts(self) -> set[str]:
        """Build the connector's effective egress allow-list (AW-2).

        Auto-seeded from THREE sources, normalized (lowercase, IPv6 brackets
        stripped), with empty/unresolved entries dropped:

        1. Every action's declared base-URL host, taken AFTER the same
           ``{template}`` credential substitution execute() performs — so a
           templated host (``{FRESHDESK_DOMAIN}.freshdesk.com``,
           ``{CONFLUENCE_BASE_URL}``) contributes its real runtime host, never a
           template string. The ``BASE_URL`` credential's host (the
           build-from-base path) is included too.
        2. The auth-endpoint host — ``auth.auth_url`` / ``auth.token_url`` — for
           connectors that authenticate on a different host than the API.
        3. Explicit operator additions — from ``allowed_hosts:`` in the YAML
           (all workspaces) and the per-workspace ``WorkspaceConnector``
           ``allowed_hosts`` (captured by connect()). These never narrow.

        Used to check the RESOLVED request host at call time; a host outside
        this set is rejected by ``assert_egress_allowed``.
        """
        from urllib.parse import urlsplit

        def _host_of(url: str) -> str | None:
            url = self._substitute_host_template(url).strip()
            if not url or "{" in url:  # unresolved template — not a real host
                return None
            # A bare ``host/path`` (no scheme) lands the host in ``.path``;
            # prepend a scheme so urlsplit populates ``.hostname``.
            parts = urlsplit(url if "://" in url else f"https://{url}")
            host = parts.hostname
            return host.lower().strip("[]") if host else None

        hosts: set[str] = set()

        # 1. Declared action base-URL hosts + the BASE_URL credential host.
        for act in self._def.actions:
            url = act.get("url", "")
            if url:
                h = _host_of(str(url))
                if h:
                    hosts.add(h)
        base_cred = self._credentials.get("BASE_URL", "")
        if base_cred:
            h = _host_of(base_cred)
            if h:
                hosts.add(h)

        # 2. Auth-endpoint host (auth host may differ from the API host).
        auth = self._def.auth or {}
        for key in ("auth_url", "token_url", "authorization_url", "token_endpoint"):
            endpoint = auth.get(key)
            if endpoint:
                h = _host_of(str(endpoint))
                if h:
                    hosts.add(h)

        # 3. Explicit operator additions — from the connector YAML
        # (``allowed_hosts:``) and from the per-workspace WorkspaceConnector row
        # (folded into config by the cloud layer, captured in connect()).
        for h_raw in (*self._def.allowed_hosts, *self._ws_allowed_hosts):
            h = _host_of(str(h_raw))
            if h:
                hosts.add(h)

        return hosts

    async def _egress_client(self, url: str) -> Any:
        """Validate ``url`` against the egress policy and return a pinned client.

        Closes the connector SSRF bypass (AW-1) and adds the multi-host
        allow-list (AW-2): enforces https-only, no userinfo/fragment, the
        RESOLVED request host must be on the connector's effective allow-list
        (declared base-URL host(s) + auth-endpoint host + explicit
        ``allowed_hosts``), DNS pre-resolve + internal-range reject, then dials
        the vetted/pinned IP via a ``PinnedTransport`` client with redirects
        disabled — so the connection cannot be re-resolved to an internal
        address between the check and the connect (DNS-rebind TOCTOU).

        The pinned client is CACHED per resolved host (AW-2 concern fix 2) so
        the cookie jar survives across calls — session/cookie-auth connectors
        keep working under the guard. The cache is closed in disconnect(); the
        caller MUST NOT close the returned client.

        Raises ``EgressError`` (a ``ValueError``) when the URL is disallowed.
        """
        import httpx

        from pocketpaw.security.url_validators import (
            PinnedTransport,
            assert_egress_allowed,
        )

        # Check the RESOLVED request host against the connector's effective
        # allow-list. ``url`` already had its {templates} substituted by
        # execute(), so this is a concrete host, never a template string.
        allowed = self._effective_allowed_hosts()
        target = await assert_egress_allowed(url, allowed)

        # Cache one pinned client per resolved host so the cookie jar persists
        # across calls (concern fix 2). Key on the host so the cookie jar is
        # reused even if DNS later returns a different IP. ``assert_egress_allowed``
        # above STILL re-resolves and re-checks the host on every call, so the
        # security boundary (host on the allow-list + currently resolving to a
        # non-internal IP) holds per request even though the cached transport
        # keeps dialing the first call's pinned IP. That stale-pin is an
        # availability edge (a host migrating IPs mid-session), not a security
        # one — and re-pinning per call would discard the cookie jar, the exact
        # regression this cache fixes. Rebuild only if the cached client closed.
        cached = self._pinned_clients.get(target.host)
        if cached is not None and not cached.is_closed:
            return cached
        transport = PinnedTransport(target.pinned_ip)
        client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=False,
            transport=transport,
        )
        self._pinned_clients[target.host] = client
        return client

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

        # AW-2 — per-workspace egress allow-list additions. The cloud layer
        # folds WorkspaceConnector.allowed_hosts into the config blob under
        # ``allowed_hosts``; capture it so _effective_allowed_hosts() can layer
        # these operator additions on top of the YAML-declared hosts. Tolerate a
        # non-list value (ignored) so a malformed config can't break connect().
        ws_allowed = config.get("allowed_hosts", [])
        self._ws_allowed_hosts = (
            [str(h).strip() for h in ws_allowed if str(h).strip()]
            if isinstance(ws_allowed, list)
            else []
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
        # Close the pooled HTTP client (and drop the cookie jar) on disconnect.
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        # AW-2 — close every cached pinned client (one per resolved host) and
        # drop their cookie jars too. They live across calls now, so disconnect
        # is the single place they are torn down.
        for client in self._pinned_clients.values():
            await client.aclose()
        self._pinned_clients.clear()
        return True

    async def actions(self) -> list[ActionSchema]:
        """Convert YAML action definitions to ActionSchema list.

        Phase 1 PR-2 reads optional ``execution_mode`` (default ``cloud``)
        and ``requires_binary`` keys from the YAML so CLI connectors
        rewritten as YAML can declare local-mode actions without a
        Python adapter rewrite.
        """
        from pocketpaw.connectors.protocol import ExecutionMode

        schemas = []
        for act in self._def.actions:
            params = {}
            for key, val in act.get("params", {}).items():
                params[key] = val
            for key, val in act.get("body", {}).items():
                params[key] = val

            mode_raw = act.get("execution_mode", "cloud")
            try:
                mode = ExecutionMode(mode_raw)
            except ValueError:
                mode = ExecutionMode.CLOUD

            schemas.append(
                ActionSchema(
                    name=act["name"],
                    description=act.get("description", ""),
                    method=act.get("method", "GET"),
                    parameters=params,
                    trust_level=TrustLevel(act.get("trust_level", "confirm")),
                    execution_mode=mode,
                    requires_binary=act.get("requires_binary"),
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

        # AW-1/AW-2 egress guard: when enabled, validate the resolved request
        # host against the connector's effective allow-list, pin the vetted IP,
        # and use a per-resolved-host CACHED client (so the cookie jar persists
        # across calls); when off, reuse the pooled adapter client exactly as
        # before. The guarded client is cached on the adapter and closed in
        # disconnect() — NOT per request — so it is not torn down here.
        guarded = self._egress_guard_enabled()

        try:
            import httpx

            from pocketpaw.security.url_validators import EgressError

            # Detect form-encoded APIs (Stripe, etc.) from URL or content_type hint
            content_type = act_def.get("content_type", "")
            use_form = content_type == "form" or "stripe.com" in url

            if guarded:
                try:
                    client = await self._egress_client(url)
                except EgressError as e:
                    return ActionResult(success=False, error=f"Blocked by egress guard: {e}")
            else:
                # Reuse the per-adapter client: pooled connections + persistent
                # cookie jar across calls. Closed in disconnect().
                client = await self._get_client()
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
        elif auth_method == "cookie":
            # Session/cookie auth — emit a `Cookie:` header from a declared
            # credential. The credential name comes from `auth.credential`,
            # falling back to the first declared credential. The stored value
            # is sent verbatim (e.g. "sessionid=abc" or a raw token).
            cred_name = self._def.auth.get("credential")
            if not cred_name:
                creds = self._def.auth.get("credentials", [])
                cred_name = creds[0]["name"] if creds else None
            if cred_name:
                value = self._credentials.get(cred_name, "")
                if value:
                    headers["Cookie"] = value
        elif auth_method == "header":
            # Custom-header auth — emit an arbitrary header (name from
            # `auth.header`, value from the declared credential). This is the
            # escape hatch for APIs whose key is NOT a Bearer token, so it
            # avoids the api_key-always-Bearer trap without changing api_key.
            header_name = self._def.auth.get("header")
            cred_name = self._def.auth.get("credential")
            if not cred_name:
                creds = self._def.auth.get("credentials", [])
                cred_name = creds[0]["name"] if creds else None
            if header_name and cred_name:
                value = self._credentials.get(cred_name, "")
                if value:
                    headers[header_name] = value

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

    # --- Phase 1 PR-2 protocol additions -------------------------------------

    async def widgets(self) -> list[Any]:
        """YAML connectors don't ship default home widgets in Phase 1.

        Native connectors (Gmail, Calendar, …) override this in PR-3 onwards.
        Returning ``Any`` instead of ``list[WidgetRecipe]`` here avoids a
        forward-import — the protocol module declares the type, this
        method just satisfies the protocol with an empty list.
        """
        return []

    async def health(self, scope: Any | None = None) -> Any:
        """Lightweight health snapshot.

        Phase 1 default: returns ``ConnectorHealth(ok=connected,
        status=CONNECTED|DISCONNECTED)`` based on whether ``connect()``
        has been called. Avoids an HTTP probe so this stays cheap; an
        adapter that wants real probing overrides this method.
        """
        from pocketpaw.connectors.protocol import ConnectorHealth, ConnectorStatus

        return ConnectorHealth(
            ok=self._connected,
            status=ConnectorStatus.CONNECTED if self._connected else ConnectorStatus.DISCONNECTED,
            message="connected" if self._connected else "not connected",
        )
