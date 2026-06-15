# pocket_backend.py — Beanie document for per-pocket backend credentials.
# Created: 2026-05-21 (RFC 04 alpha) — A Pocket can be bound to ONE external
#   backend (base URL + auth credential). The credential is stored here, in a
#   SEPARATE collection (`pocket_backend_credentials`) — never inside the
#   `Pocket` document and never inside `rippleSpec`. Keeping it out of the
#   pocket keeps the spec shareable and secret-free.
#
#   The auth token is encrypted at rest: `encrypted_token` / `nonce` / `salt`
#   are produced by `pockets/backend_crypto.py` (AES-GCM + HKDF-SHA256).
#   The plaintext token never touches this document.
#
# Updated: 2026-05-22 (RFC 05 M2a) — added the per-pocket WRITE ALLOWLIST.
#   `AllowedWrite` is one (method, path_pattern) rule; `allowed_writes` is
#   the list of rules a write action must match before the write executor
#   makes the call. The list lives HERE — outside the spec, in the same
#   human-configured store as the auth credential — so a compromised or
#   hallucinated spec cannot widen its own blast radius. The default is an
#   EMPTY list: fail-closed, no write can fire until a human allow-lists it.
# Updated: 2026-05-22 (RFC 05 M2b.1) — added the per-pocket APPROVAL ROUTE.
#   `ApprovalRoute` decides who approves a `requires_instinct` write:
#   `mode="owner"` (the default when `approval_route` is None) routes to
#   the pocket owner; `mode="user"` routes to a named workspace member.
#   It lives HERE alongside the credential — owner-set, outside the spec.
# Updated: 2026-05-22 (RFC 04 M3) — added `webhook_secret`. A pocket source
#   binding may declare a `"webhook"` refresh trigger; the inbound endpoint
#   `POST /pockets/{id}/sources/{source}/refresh` authenticates the caller
#   against this secret. It lives HERE — generated server-side, on the
#   credential row, NEVER in the agent-authored spec — so the spec stays
#   shareable and secret-free. `None` until an owner generates / rotates it.
# Updated: 2026-06-12 (feat/connector-as-pocket-backend) — a pocket's backend
#   can now be an existing CONNECTOR, not only an HTTP base_url. `backend_type`
#   is `"http"` (the default — every pre-existing row reads as http via the
#   field default, so this is fully back-compatible) or `"connector"`. When
#   `"connector"`, `connector_name` names a workspace-bound connector and the
#   `base_url`/`auth_*` fields are unused — source execution routes each source
#   through `connectors_service.execute(...)` instead of an HTTP GET.
# Updated: 2026-06-15 (feat/invoke-tool-v1) — added the per-pocket TOOL
#   ALLOWLIST. `ToolGrant` is one allowed tool name; `allowed_tools` is the
#   list a click-driven `invoke_tool` must match before the tool executor
#   dispatches. Parallel to `allowed_writes`: it lives HERE — outside the
#   spec, in the same human-configured store as the auth credential — so a
#   compromised or hallucinated spec cannot grant itself a tool. The default
#   is an EMPTY list: fail-closed, no tool can fire until a human allow-lists
#   it via `PUT /pockets/{id}/backend/tool-policy`. A grant's `tool` is either
#   a built-in tool name or a connector action `connector:<name>:<action>`.

from __future__ import annotations

from typing import Literal

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class AllowedWrite(BaseModel):
    """One write-allowlist rule: a method + a glob path pattern.

    The write executor matches an action's `(method, path)` against every
    `AllowedWrite` on the pocket's backend config. `method` is matched
    exactly; `path_pattern` is a glob (`fnmatch`) — `/leases/*/renew`
    matches `POST /leases/42/renew`. No rule for a verb → that verb can
    never fire (e.g. omit a `DELETE` entry → no DELETE action can run).
    """

    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path_pattern: str


class ToolGrant(BaseModel):
    """One tool-allowlist entry (feat/invoke-tool-v1). Two forms:

    * a built-in / registry tool name, e.g. ``"web_fetch"``
    * a connector action, ``"connector:<name>:<action>"``, e.g.
      ``"connector:github:list_issues"``

    The tool executor matches an invoked tool name EXACTLY against the
    ``tool`` field of every grant. No grant for a name → that name can
    never fire (fail-closed). A connector grant additionally tells the
    executor to route through ``connectors.service.execute`` and which
    ``(connector, action)`` to call.

    A ``connector:`` grant resolves the connector by name (via the bind
    check) independent of the pocket's ``backend_type`` — the grant names
    the connector explicitly, so a pocket with an http ``base_url`` backend
    can still hold connector grants.
    """

    tool: str = Field(min_length=1)


class ApprovalRoute(BaseModel):
    """Who approves a pocket's `requires_instinct` writes (RFC 05 M2b.1).

    `mode="owner"` routes a gated write to the pocket owner — the same as
    leaving `approval_route` unset. `mode="user"` routes to the named
    `user_id`; the service validates that id is a current workspace
    member when the route is set, so a stale `user_id` here is trusted.
    """

    mode: Literal["owner", "user"] = "owner"
    user_id: str | None = None


class PocketBackendCredential(TimestampedDocument):
    """Per-pocket backend binding: an HTTP base URL or a bound connector.

    One row per pocket. ``backend_type`` selects the shape:

    * ``"http"`` (the default, and every legacy row) — ``base_url`` +
      optional encrypted auth credential. The run-sources executor decrypts
      the token at call time and fetches each source with an SSRF-guarded GET.
    * ``"connector"`` — ``connector_name`` names a workspace-bound connector.
      ``base_url`` / ``auth_*`` are unused; source execution routes each
      source's ``action``/``params`` through ``connectors_service.execute``
      (the same read-first path senses use).

    Every read path returns only the non-secret summary (``backend_type`` /
    ``connector_name`` / ``base_url`` / ``auth_type`` / ``configured`` /
    ``allowed_writes`` / ``allowed_tools`` / ``approval_route``) — the secret
    never leaves this collection.
    """

    pocket_id: Indexed(str)  # type: ignore[valid-type]
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    # "http" (base_url + auth) | "connector" (bound connector action surface).
    # Defaults to "http" so every row written before this feature reads as an
    # http backend — fully back-compatible.
    backend_type: Literal["http", "connector"] = "http"
    # For backend_type == "connector": the workspace-bound connector this
    # pocket fetches through. None for http backends.
    connector_name: str | None = None
    # The HTTP base URL. Empty string for a connector backend (base_url is
    # unused there). Kept required-with-default so legacy http rows are
    # unaffected and the field is never None.
    base_url: str = ""
    # bearer | api_key | basic | none
    auth_type: str = "none"
    # Custom header name for the api_key auth type. Defaults to "X-Api-Key".
    auth_header: str | None = None
    # Encrypted token material. All three are None when auth_type == "none".
    encrypted_token: bytes | None = None
    nonce: bytes | None = None
    salt: bytes | None = None
    # RFC 05 M2a write allowlist. EMPTY by default — fail-closed: a pocket
    # with no policy can fire no write actions. A human widens it via
    # `PUT /pockets/{id}/backend/write-policy`.
    allowed_writes: list[AllowedWrite] = Field(default_factory=list)
    # feat/invoke-tool-v1 tool allowlist. EMPTY by default — fail-closed: a
    # pocket with no policy can fire no `invoke_tool` calls. A human widens it
    # via `PUT /pockets/{id}/backend/tool-policy`. Legacy rows read as `[]`
    # via this field default, so the fail-closed posture holds with no
    # migration.
    allowed_tools: list[ToolGrant] = Field(default_factory=list)
    # RFC 05 M2b.1 approval route. None means the default — `requires_instinct`
    # writes route to the pocket owner. An owner sets a named approver via
    # `PUT /pockets/{id}/backend/approval-route`.
    approval_route: ApprovalRoute | None = None
    # RFC 04 M3 webhook secret. The shared secret an inbound
    # `POST /pockets/{id}/sources/{source}/refresh` must present to trigger
    # a `"webhook"`-refresh source. None until an owner generates one via
    # `POST /pockets/{id}/backend/webhook/rotate`. Stored in plaintext
    # (it IS the credential the caller echoes back, like an API key) — a
    # rotate replaces it, invalidating the previous value.
    webhook_secret: str | None = None

    class Settings:
        name = "pocket_backend_credentials"
