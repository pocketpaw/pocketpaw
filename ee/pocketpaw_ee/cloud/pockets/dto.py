"""Pockets domain — request/response schemas.

Changes: Added agents, rippleSpec (aliased), and widgets fields to CreatePocketRequest
so the frontend can pass the full pocket spec on creation instead of requiring
separate follow-up calls.

Updated: 2026-05-16 — added optional ``project_id`` (aliased as
``projectId`` on the wire) to CreatePocketRequest / UpdatePocketRequest /
PocketResponse so pockets can be grouped under a Mission Control Project.

Updated: 2026-05-21 — documented the ``type="native"`` widget contract on
``AddWidgetRequest`` (home-as-pocket foundation). A native widget is an
ordinary widget entry whose ``type`` is ``"native"`` and whose ``name`` is
the key the frontend uses to look up a built-in Svelte component — it
carries no ``rippleSpec`` to render or validate.

Updated: 2026-05-21 — added ``HomePocketResponse`` so ``GET /pockets/home``
has a real OpenAPI schema instead of an empty ``dict``.

Updated: 2026-05-22 (#1174) — ``AddWidgetRequest`` and ``_widget_to_wire``
carry the optional ``spec`` field: a per-tile rippleSpec subtree the home
grid renders. Populated by the home agent's ``add_widget`` MCP tool.

Updated: 2026-05-21 (RFC 04 alpha) — added PocketBackendConfigRequest /
PocketBackendConfigResponse / RunSourcesRequest for the per-pocket backend
binding + read-only source-run endpoints.
Updated: 2026-05-21 (PR #1177 security pass) — PocketBackendConfigRequest
.base_url now requires min_length=1; RunSourcesRequest.source coerces an
empty string to None; documented that `auth_token` for `basic` is the
`user:pass` credential (base64-encoded server-side).
Updated: 2026-05-22 (RFC 05 M2a) — added RunActionRequest /
RunActionResponse for the write-action run endpoint, plus AllowedWriteDTO
and SetWritePolicyRequest for the per-pocket write-allowlist endpoint.
Updated: 2026-05-22 (RFC 05 M2b.1) — RunActionResponse gained
``proposed_action_id`` (set when a ``requires_instinct`` write is parked
into an Instinct Action instead of fired). Added ApprovalRouteDTO and
SetApprovalRouteRequest plus an optional ``approval_route`` field on
PocketBackendConfigResponse — the per-pocket approver routing for gated
writes.
Updated: 2026-05-22 (security-review fix for PR #1183, SHOULD-FIX 2) —
RunActionResponse is now ``extra="forbid"`` so an executor-internal key
(``_park``, ``outcome``) that the router fails to strip raises on
construction instead of leaking the resolved write path/params onto the
wire.
Updated: 2026-05-24 (#1206 part a) — added RunToolRequest /
RunToolResponse for the new ``POST /pockets/{id}/tools/run`` wire (the
click-driven sibling of ``sources/run`` and ``actions/run``). The
endpoint runs a named server-side tool with the resolved args from the
``invoke_tool`` ripple action verb; the allowlist is intentionally empty
in part (a) so the wire is locked down before any tool can fire (parts b
and c add the home-grid plumbing + prompt guidance).
Updated: 2026-05-28 (feat/wave-3b-action-pipeline) — added
DispatchBulkRequest / BulkDispatchResponse / BulkExecutionResultDTO /
BulkBlockedRowDTO for the new
``POST /pockets/{id}/actions/{action}/dispatch-bulk`` endpoint. The
request carries ``rows`` (per-row dicts); the response surfaces the
RFC 03 v2 bucketing — ``executions`` / ``blocked`` /
``batch_approval_id`` — plus the consolidated ``approval_row_ids``.
Updated: 2026-05-28 (feat/wave-3e-template-slug) — added the optional
``template_slug`` (aliased ``templateSlug``) field on
``CreatePocketRequest`` / ``UpdatePocketRequest`` / ``PocketResponse``.
When supplied on create, the service loads the named bundled template,
compiles it, and merges the runtime-shaped dict into the pocket's
``rippleSpec`` (compile-on-install). Legacy callers that omit it see
the same shape they always have.
Updated: 2026-06-03 (feat/sites-landing-brain) — added the optional
``pattern`` field on ``CreatePocketRequest`` / ``PocketResponse`` (and
threaded onto the wire dict). Records the create-pocket layout pattern
(``"landing"`` for marketing sites). Single-word key, so the wire form
is the same snake_case ``"pattern"`` on both DTOs and the legacy wire
dict. Legacy callers that omit it read back ``None``.
Updated: 2026-06-04 (feat/sites-svelte-engine) — added the Paw Sites
"Svelte track" fields ``engine`` (``"ripple"`` default | ``"svelte"``)
and ``source`` (the SvelteKit source map, or ``None``) on
``CreatePocketRequest`` / ``PocketResponse`` and threaded onto the wire
dict. Both are single-word snake_case keys (no camelCase split), matching
the read-back form. Defaults keep legacy callers reading ``engine="ripple"``,
``source=None``.
Updated: 2026-06-05 (feat/entity-pocket-profile-field, entity-rooms
chunk ②) — added the optional ``surface_profile`` field (aliased
``surfaceProfile`` on the wire) on ``CreatePocketRequest`` /
``PocketResponse`` and threaded it onto the wire dict. Reuses the
``PocketSurfaceProfile`` sub-model (all sub-fields optional, JSON-friendly
lists) so the create/read DTOs share one shape with the persisted model.
Defaults to ``None`` → legacy callers read back ``None`` with no migration.
Updated: 2026-06-06 (feat/entity-pocket-profile-field) — added the
``surface_profile`` field (aliased ``surfaceProfile``) to
``UpdatePocketRequest`` so an existing pocket's override can be SET / CHANGED
/ CLEARED (the auto-authoring write path). Three-way partial semantics live
in ``pockets_service.update`` and key off ``model_fields_set``: present +
non-null sets/replaces, explicit ``null`` clears, absent leaves it unchanged.
Updated: 2026-06-07 (feat/entity-pocket-profile-field) — import
``PocketSurfaceProfile`` from ``surface.domain`` (its new home) instead of
``models.pocket``. The OSS-EE boundary contract forbids ``pockets.dto`` from
importing ``models.*``; the class is a plain value object, not a Beanie doc,
so sharing it via the leaf domain module keeps one shape without crossing the
boundary.
Updated: 2026-06-12 (feat/connector-as-pocket-backend) — a pocket backend
can be an existing CONNECTOR, not only an http base_url.
PocketBackendConfigRequest gained ``backend_type`` ("http" default |
"connector") + ``connector_name`` and made ``base_url`` optional (a
connector backend needs none; the service still requires a valid URL for an
http backend). PocketBackendConfigResponse echoes ``backend_type`` /
``connector_name`` back (never the token).
Updated: 2026-06-13 (fix/ripple-normalizer-path-drop, native gated actions
step 1) — ``RunActionRequest.path`` is now OPTIONAL (``str | None``, default
``None``). The read-time ripple normalizer rewrites an inline write ``api``
handler into a PATHLESS ``call_binding`` handler (it has no request context to
carry a path), so the client fires ``/actions/run`` with only ``{action,
params}``. The path is author-time data the server already persists on the
``rippleSpec.actions`` binding (it must, to read the HTTP method), so the route
resolves it from there when the client omits it. A required ``path`` 422'd
every relative-URL api button (caught live on the Nerve demo's "Score next 20").
Client-sent ``path`` (a row-scoped binding whose stored path holds an unresolved
``{item.id}`` template, resolved client-side) still wins — the binding path is
only the fallback. The server still reads the verb from the binding, so a
compromised client cannot pick method OR a path the allowlist would reject.
Updated: 2026-06-19 (feat/typed-ripplespec-phase1) — ``UpdatePocketRequest``
gains ``reset_state: bool = False``. It is the escape hatch for the clobber-fix:
by default a partial ``ripple_spec`` body that omits instance-owned regions
(``state`` / ``selections``) now PRESERVES them (the service does a layer-safe
merge), so a frontend canvas-only PATCH no longer wipes instance data. A caller
that genuinely wants to clear instance state sends ``reset_state: true`` to
restore the old wholesale write. Wire-level ``ripple_spec`` stays ``dict | None``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from pocketpaw_ee.cloud.surface.domain import PocketSurfaceProfile


class CreatePocketRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    type: str = "custom"
    icon: str = ""
    color: str = ""
    visibility: str = Field(default="workspace", pattern="^(private|workspace|public)$")
    session_id: str | None = Field(default=None, alias="sessionId")
    agents: list[str] = Field(default_factory=list)  # Agent IDs to assign
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")
    widgets: list[dict] = Field(default_factory=list)  # Initial widget definitions
    project_id: str | None = Field(default=None, alias="projectId")
    # RFC 03 v2 (Wave 3e) — the bundled-template slug this pocket is
    # instantiated from. When set, the service loads + compiles the
    # template at create time and merges the result into ``rippleSpec``.
    # Omitting it preserves the pre-Wave-3e behaviour.
    template_slug: str | None = Field(default=None, alias="templateSlug")
    # The create-pocket layout pattern (``"dashboard"`` | ``"viewer"`` |
    # ``"app"`` | ``"landing"`` | ...). The marketing-site brain stamps
    # ``"landing"`` so the published site renders as a landing page.
    # Optional; omitting it persists ``None`` (legacy behaviour).
    pattern: str | None = None
    # Paw Sites generation track (``"ripple"`` default | ``"svelte"``) and,
    # for svelte sites, the hand-written SvelteKit source map
    # ``{relative_path: file_contents}``. Omitting them persists the ripple
    # defaults (``engine="ripple"``, ``source=None``).
    engine: str = "ripple"
    source: dict[str, str] | None = None
    # Optional per-entity surface-profile override. Consumed by the
    # entity-aware resolve_profile (entity-rooms chunk ①); None = use the
    # surface-kind default. Reuses the persisted ``PocketSurfaceProfile``
    # sub-model (all fields optional, JSON-friendly lists). Wire alias
    # ``surfaceProfile``.
    surface_profile: PocketSurfaceProfile | None = Field(default=None, alias="surfaceProfile")

    model_config = {"populate_by_name": True}


class UpdatePocketRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None
    icon: str | None = None
    color: str | None = None
    visibility: str | None = None
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")
    project_id: str | None = Field(default=None, alias="projectId")
    # When provided, the service treats this as "switch / set the template
    # for this pocket" — re-loads + recompiles + merges into rippleSpec.
    # Pass the same slug to force a recompile (template content edited
    # out-of-band). ``None`` (the default) means "leave it alone."
    template_slug: str | None = Field(default=None, alias="templateSlug")
    # Optional per-entity surface-profile override (the auto-authoring
    # foundation). Three-way partial semantics, distinguished by
    # ``model_fields_set`` in the service: present + non-null SETs/REPLACEs,
    # explicit ``null`` CLEARS (un-profiles the pocket), and absent (not in
    # the partial update) leaves the existing value untouched. Reuses the
    # persisted ``PocketSurfaceProfile`` sub-model (all sub-fields optional,
    # JSON-friendly lists). Wire alias ``surfaceProfile``.
    surface_profile: PocketSurfaceProfile | None = Field(default=None, alias="surfaceProfile")
    # Escape hatch for the clobber-fix (2026-06-13 bug). By DEFAULT a
    # ``ripple_spec`` body that omits instance-owned regions (``state`` /
    # ``selections``) PRESERVES them — the service does a layer-safe merge so a
    # frontend canvas-only PATCH no longer wipes instance data. A caller that
    # INTENDS to clear instance state (e.g. "reset this pocket to a clean
    # slate") must opt in by sending ``reset_state: true``, which restores the
    # old wholesale write of the incoming spec. Wire-level ``ripple_spec`` stays
    # ``dict | None`` — this is the only new field on the wire.
    reset_state: bool = Field(
        default=False,
        description="When true, an incoming ripple_spec replaces the pocket "
        "spec wholesale, clearing instance-owned state/selections it omits. "
        "Default false preserves existing instance state on a partial update.",
    )

    model_config = {"populate_by_name": True}


class AddWidgetRequest(BaseModel):
    """Body for POST /pockets/{id}/widgets.

    ``type`` is free-form. Two kinds of widget ride this schema:

    * ordinary Ripple-spec widgets — ``spec`` carries the rippleSpec
      subtree the home grid renders for this tile (e.g. a ``chart`` node
      with a real ``data`` series);
    * native widgets — ``type="native"`` and ``name`` is the key the
      frontend uses to resolve a built-in Svelte component. Native
      widgets have no rippleSpec, so manifest validation (which only
      walks rippleSpec trees) never touches them. ``icon``/``color`` are
      kept for the tile chrome.
    """

    name: str = Field(min_length=1, max_length=100)
    type: str = "custom"
    icon: str = ""
    color: str = ""
    span: str = "col-span-1"
    data_source_type: str = "static"
    config: dict = Field(default_factory=dict)
    props: dict = Field(default_factory=dict)
    # Optional per-tile rippleSpec subtree. The home grid renders the tile
    # from ``spec`` when present; native widgets leave it ``None``.
    spec: dict | None = None
    assigned_agent: str | None = None


class UpdateWidgetRequest(BaseModel):
    name: str | None = None
    type: str | None = None
    icon: str | None = None
    config: dict | None = None
    props: dict | None = None
    data: Any = None
    assigned_agent: str | None = None


class ReorderWidgetsRequest(BaseModel):
    widget_ids: list[str]  # Ordered list of widget IDs


class ShareLinkRequest(BaseModel):
    access: str = Field(default="view", pattern="^(view|comment|edit)$")


class AddCollaboratorRequest(BaseModel):
    user_id: str
    access: str = Field(default="edit", pattern="^(view|comment|edit)$")


class MergeSpecRequest(BaseModel):
    """Body for ``POST /pockets/{id}/spec/merge``.

    Carries EXACTLY ONE of:

    * ``replace`` — a full rippleSpec dict that wholesale-replaces the
      pocket's current spec.
    * ``merge`` — a partial rippleSpec dict that is applied via
      ``_merge.merge_ripple_spec`` against the current spec.

    The ``model_validator`` below enforces the exactly-one rule at
    parse time so the router never has to hand-roll an ``isinstance``
    check on a free-form ``dict`` body (the original MVP shape that
    PR #1222 R1 flagged). A body with both keys or neither raises a
    422 before the request reaches the service layer.

    PR #1222 R1 follow-up: introduced to replace the prior
    ``body: dict`` route signature.
    """

    replace: dict | None = None
    merge: dict | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> MergeSpecRequest:
        if (self.replace is None) == (self.merge is None):
            raise ValueError(
                "Body must carry exactly one of 'replace' or 'merge' (got both or neither).",
            )
        return self


class PocketResponse(BaseModel):
    id: str
    workspace: str
    name: str
    description: str
    type: str
    icon: str
    color: str
    owner: str
    visibility: str
    team: list[Any]
    agents: list[Any]
    widgets: list[dict]
    ripple_spec: dict | None = None
    share_link_token: str | None = None
    share_link_access: str = "view"
    shared_with: list[str]
    project_id: str | None = None
    # RFC 03 v2 (Wave 3e) — the bundled-template slug, or ``None`` for
    # legacy pockets / cold-generated rippleSpecs.
    template_slug: str | None = None
    # The create-pocket layout pattern (``"landing"`` for marketing
    # sites), or ``None`` for legacy pockets.
    pattern: str | None = None
    # Paw Sites generation track (``"ripple"`` | ``"svelte"``) and the
    # svelte source map (or ``None`` for ripple pockets).
    engine: str = "ripple"
    source: dict[str, str] | None = None
    # Optional per-entity surface-profile override. Consumed by the
    # entity-aware resolve_profile (entity-rooms chunk ①); None = use the
    # surface-kind default. Wire alias ``surfaceProfile``.
    surface_profile: PocketSurfaceProfile | None = Field(default=None, alias="surfaceProfile")
    created_at: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}


class HomePocketResponse(BaseModel):
    """Response for ``GET /pockets/home``.

    ``pocket`` is the full pocket wire dict (camelCase keys, ``_id``,
    ``rippleSpec`` — the legacy shape ``pocket_to_wire_dict`` emits), kept
    as a free-form ``dict`` because it is not the snake_case
    ``PocketResponse`` shape and the client builds against the wire dict
    verbatim. ``created`` is ``True`` only when this call provisioned a
    brand-new home pocket — the client gates one-time widget seeding /
    localStorage migration on it.
    """

    pocket_id: str
    pocket: dict[str, Any]
    created: bool


# ---------------------------------------------------------------------------
# Pocket backend binding + source-run (RFC 04 alpha)
# ---------------------------------------------------------------------------


class AllowedWriteDTO(BaseModel):
    """One write-allowlist rule on the wire — a (method, path_pattern) pair.

    Mirrors ``models.pocket_backend.AllowedWrite``. ``path_pattern`` is a
    glob: ``/leases/*/renew`` allows ``POST /leases/42/renew``. RFC 05 M2a.
    """

    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path_pattern: str = Field(min_length=1)


class PocketBackendConfigRequest(BaseModel):
    """Body for ``PUT /pockets/{id}/backend`` — bind a pocket to one backend.

    ``backend_type`` selects the shape:

    * ``"http"`` (the default) — ``base_url`` + optional auth. ``auth_token``
      carries the secret only on the way IN; it is encrypted server-side and
      never returned. Its meaning depends on ``auth_type``:
        - ``bearer`` — the bearer token (``Authorization: Bearer <token>``).
        - ``api_key`` — the API key value, sent in the ``auth_header`` header.
        - ``basic`` — the raw ``user:pass`` credential. The server
          base64-encodes it — do NOT pre-encode.
        - ``none`` — unused.
      ``auth_header`` names the custom header for the ``api_key`` auth type
      (defaults to ``X-Api-Key`` when omitted).
    * ``"connector"`` — ``connector_name`` names a workspace-bound connector.
      ``base_url``/``auth_*`` are unused (and not required); the service
      validates the connector is enabled for the workspace.

    ``base_url`` is OPTIONAL here (defaults to ``""``) so a connector backend
    needn't send one; the service still requires a valid URL for an http
    backend.
    """

    backend_type: Literal["http", "connector"] = "http"
    connector_name: str | None = None
    base_url: str = ""
    auth_type: Literal["bearer", "api_key", "basic", "none"] = "none"
    auth_token: str = ""
    auth_header: str | None = None


class ApprovalRouteDTO(BaseModel):
    """Who approves a pocket's ``requires_instinct`` writes (RFC 05 M2b.1).

    ``mode="owner"`` (the default) routes every gated write to the pocket
    owner. ``mode="user"`` routes to a named workspace member —
    ``user_id`` is then required and is validated as a current workspace
    member when the route is set.
    """

    mode: Literal["owner", "user"] = "owner"
    user_id: str | None = None

    @field_validator("user_id")
    @classmethod
    def _empty_user_is_none(cls, v: str | None) -> str | None:
        return v or None


class PocketBackendConfigResponse(BaseModel):
    """Backend binding as returned to clients — never carries the token.

    ``backend_type`` is ``"http"`` (the default / legacy) or ``"connector"``;
    ``connector_name`` names the bound connector when ``backend_type`` is
    ``"connector"`` (``None`` for http). For an http backend ``base_url`` /
    ``auth_type`` describe the endpoint; for a connector backend ``base_url``
    is ``""`` and ``auth_type`` is ``"none"``.

    ``allowed_writes`` is the per-pocket write allowlist (RFC 05 M2a) —
    an owner/editor-facing non-secret. Empty by default (fail-closed: no
    write fires until a human allow-lists it).

    ``approval_route`` is the per-pocket approver routing for
    ``requires_instinct`` writes (RFC 05 M2b.1). ``None`` means the
    default — the pocket owner approves.
    """

    backend_type: str = "http"
    connector_name: str | None = None
    base_url: str
    auth_type: str
    configured: bool
    allowed_writes: list[AllowedWriteDTO] = Field(default_factory=list)
    approval_route: ApprovalRouteDTO | None = None


class RunSourcesRequest(BaseModel):
    """Body for ``POST /pockets/{id}/sources/run``.

    ``trigger`` selects sources by refresh policy (``pocket_open`` runs the
    on-open set; ``manual`` runs the refresh-button set). ``source`` runs a
    single named source regardless of policy. Both omitted runs every
    source declared in the spec.

    An empty-string ``source`` is coerced to ``None`` — it would otherwise
    select zero sources (no source key is named "") and silently no-op.
    """

    trigger: Literal["pocket_open", "manual"] | None = None
    source: str | None = None

    @field_validator("source")
    @classmethod
    def _empty_source_is_none(cls, v: str | None) -> str | None:
        return v or None


# ---------------------------------------------------------------------------
# Pocket write actions + write policy (RFC 05 M2a)
# ---------------------------------------------------------------------------


class RunActionRequest(BaseModel):
    """Body for ``POST /pockets/{id}/actions/run``.

    The client sends the action's NAME (``action``) plus the *resolved*
    ``params`` — Ripple's ``{...}`` expression resolver runs client-side at
    click time. The server loads the named action from the persisted
    ``rippleSpec.actions`` block to read the HTTP ``method`` — the client
    never picks the verb.

    ``path`` is OPTIONAL. The read-time ripple normalizer rewrites an inline
    write ``api`` handler into a PATHLESS ``call_binding`` handler, so the
    normal client fires with no ``path`` and the route resolves it from the
    persisted binding's ``path`` (which the spec already carries — see
    :class:`~pocketpaw_ee.cloud.pockets.action_executor.ActionBinding`). A
    client MAY still send a ``path`` — a row-scoped binding whose stored path
    holds an unresolved ``{item.id}`` template gets resolved client-side, and
    that resolved value takes precedence over the binding's templated one.
    Either way the server reads the verb from the binding and the executor
    matches the final ``(method, path)`` against the owner's allowlist, so a
    client cannot pick a path the allowlist would reject.

    ``idempotency_key`` is optional: when omitted the server generates one
    so a write retried after a timeout cannot double-submit.
    """

    action: str = Field(min_length=1)
    path: str | None = Field(default=None, min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class RunActionResponse(BaseModel):
    """Result of a write-action run.

    On a fired write ``ok`` is true and ``status`` / ``response`` carry the
    backend's HTTP status + parsed JSON body. On failure ``ok`` is false
    and ``error`` / ``code`` describe the rejection. ``on_success`` /
    ``on_error`` are the reconcile handler lists the client runs after.

    On a PARKED write (RFC 05 M2b.1) — a ``requires_instinct`` action —
    ``ok`` is true, ``code`` is ``"instinct_pending"``, and
    ``proposed_action_id`` carries the id of the Instinct Action the
    write was routed into. No backend call was made; the client shows a
    "waiting for approval" state and does NOT run the reconcile handlers.

    All optional fields keep one model usable for every outcome.

    ``extra="forbid"`` (security-review fix for PR #1183, SHOULD-FIX 2):
    the executor result dict carries internal-only keys (``_park`` —
    the resolved write path/params — and ``outcome``) that the router
    strips before constructing this response. ``forbid`` makes that
    strip mandatory: if the strip ever misses a key, model construction
    raises instead of leaking the resolved write onto the wire.
    """

    model_config = {"extra": "forbid"}

    ok: bool
    action: str
    status: int | None = None
    response: Any = None
    error: str | None = None
    code: str | None = None
    proposed_action_id: str | None = None
    on_success: list[dict] = Field(default_factory=list)
    on_error: list[dict] = Field(default_factory=list)


class SetWritePolicyRequest(BaseModel):
    """Body for ``PUT /pockets/{id}/backend/write-policy``.

    Replaces the pocket's whole write allowlist. An empty list is valid
    and meaningful — it revokes every write (fail-closed).
    """

    allowed_writes: list[AllowedWriteDTO] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pocket tool invocations (#1206 part a — invoke_tool wire)
# ---------------------------------------------------------------------------


class RunToolRequest(BaseModel):
    """Body for ``POST /pockets/{id}/tools/run`` (#1206 part a).

    The client sends the tool's NAME (``tool``) plus the *resolved*
    ``args`` — Ripple's ``{state.x}`` / ``{item.id}`` expression resolver
    runs client-side at click time, so the server sees plain values, not
    expressions. Sibling to ``RunSourcesRequest`` (read-only fetch) and
    ``RunActionRequest`` (named write binding); ``invoke_tool`` runs a
    named server-side tool (WebFetch, Composio, etc.) and re-hydrates the
    UI from the result.

    The allowlist enforcement lives in the executor: an empty allowlist
    fails closed with ``code="not_allowed"``. The wire-level allowlist is
    intentionally empty in part (a) so nothing fires until the captain
    explicitly enables tools per pocket; the home-grid plumbing that
    actually POSTs here lands in part (b).
    """

    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class RunToolResponse(BaseModel):
    """Result of an ``invoke_tool`` run.

    Mirrors :class:`RunActionResponse` so a single client-side reconcile
    handler shape services both write actions and tool invocations. On a
    fired tool ``ok`` is true and ``status`` / ``response`` carry the
    tool's HTTP-shaped result. On rejection ``ok`` is false and
    ``error`` / ``code`` describe the reason — ``not_allowed`` when the
    tool isn't on the pocket's allowlist, ``unknown_tool`` when no
    registry entry matches.

    ``on_success`` / ``on_error`` are the reconcile handler lists the
    client runs after — the same shape ``call_binding`` returns, so the
    home grid's ``onEvent`` plumbing handles both with one branch.

    ``extra="forbid"`` matches :class:`RunActionResponse`: any
    executor-internal key the route fails to strip raises on
    construction instead of leaking onto the wire.
    """

    model_config = {"extra": "forbid"}

    ok: bool
    tool: str
    status: int | None = None
    response: Any = None
    error: str | None = None
    code: str | None = None
    on_success: list[dict] = Field(default_factory=list)
    on_error: list[dict] = Field(default_factory=list)


class SetApprovalRouteRequest(BaseModel):
    """Body for ``PUT /pockets/{id}/backend/approval-route`` (RFC 05 M2b.1).

    Sets who approves the pocket's ``requires_instinct`` writes.
    ``route=None`` (or an omitted body) clears the route back to the
    default — the pocket owner. ``mode="user"`` requires a ``user_id``
    that the service validates as a current workspace member.
    """

    route: ApprovalRouteDTO | None = None


# ---------------------------------------------------------------------------
# Bulk action dispatch (RFC 03 v2 / Wave 3b)
# ---------------------------------------------------------------------------


class DispatchBulkRequest(BaseModel):
    """Body for ``POST /pockets/{id}/actions/{action}/dispatch-bulk``.

    ``rows`` is the per-row payloads the operator selected. Each entry
    is a free-form dict — the OSS planner threads it through
    ``resolve_instinct`` per-row, so the keys the template's CEL rules
    reference must be present.

    ``pocket_id`` and ``action_name`` are mirrored from the URL onto
    the body to make the service-level call shape symmetric with
    other entries in this module (rule 5 — every service takes a
    typed ``body``). The router fills them in from the path
    parameters; internal callers (jobs, MCP tools) pass them directly.
    """

    model_config = {"extra": "forbid"}

    pocket_id: str = Field(min_length=1)
    action_name: str = Field(min_length=1)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class BulkExecutionResultDTO(BaseModel):
    """One row's execution slot on the wire.

    ``response`` is the executor's full result dict for that row — the
    same shape ``RunActionResponse`` carries for single-row calls,
    serialized as a plain dict so the wire surface stays narrow.
    """

    model_config = {"extra": "forbid"}

    row_id: str
    verdict: str
    response: dict[str, Any]


class BulkBlockedRowDTO(BaseModel):
    """One row that the Instinct composer blocked.

    Block reasons + the offending rule's ``when`` expression travel
    on the wire so the operator can see WHY a row didn't run, without
    leaking the full ``InstinctDecision`` audit blob.
    """

    model_config = {"extra": "forbid"}

    row_id: str
    reason: str
    rule_when: str


class BulkDispatchResponse(BaseModel):
    """Wire response for ``POST /pockets/{id}/actions/{action}/dispatch-bulk``.

    Mirrors the ``BulkDispatchResult`` library shape. ``batch_approval_id``
    is set when ANY row escalated to approval — exactly ONE id covers
    every approval-needing row in the batch (RFC mandate).
    """

    model_config = {"extra": "forbid"}

    pocket_id: str
    action_name: str
    total_rows: int
    executions: list[BulkExecutionResultDTO] = Field(default_factory=list)
    blocked: list[BulkBlockedRowDTO] = Field(default_factory=list)
    batch_approval_id: str | None = None
    approval_row_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain → wire mappers (Phase 8)
# ---------------------------------------------------------------------------


def pocket_to_wire_dict(p) -> dict:
    """Convert a domain ``Pocket`` (from ``ee.cloud.pockets.domain``) to
    the legacy wire-format dict. Byte-equivalent to the
    ``_pocket_response`` helper in ``service.py``.

    Also applies read-time normalization to ``rippleSpec``: old pockets
    persisted before the agent-alias safety net (``root`` / ``tree`` /
    etc. lifted into ``ui``) get fixed in flight without a DB rewrite.
    The normalizer is idempotent — specs already in the canonical
    ``{ui, state}`` shape pass through unchanged.
    """
    from pocketpaw_ee.cloud._core.time import iso_utc
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    return {
        "_id": p.id,
        "workspace": p.workspace_id,
        "name": p.name,
        "description": p.description,
        "type": p.type,
        "icon": p.icon,
        "color": p.color,
        "owner": p.owner,
        "visibility": p.visibility,
        "team": list(p.team),
        "agents": list(p.agents),
        "widgets": [_widget_to_wire(w) for w in p.widgets],
        "rippleSpec": normalize_ripple_spec(p.ripple_spec) if p.ripple_spec else p.ripple_spec,
        "shareLinkToken": p.share_link_token,
        "shareLinkAccess": p.share_link_access,
        "sharedWith": list(p.shared_with),
        "projectId": p.project_id,
        # RFC 03 v2 (Wave 3e) — the bundled-template slug the pocket was
        # instantiated from. ``None`` for legacy / cold-generated pockets.
        "templateSlug": getattr(p, "template_slug", None),
        # The create-pocket layout pattern (``"landing"`` for marketing
        # sites). Single-word key — no camelCase split. ``None`` for
        # legacy pockets.
        "pattern": getattr(p, "pattern", None),
        # Paw Sites generation track + svelte source map. Single-word keys,
        # no camelCase split. ``engine`` defaults to ``"ripple"`` and
        # ``source`` to ``None`` for legacy / ripple pockets.
        "engine": getattr(p, "engine", "ripple"),
        "source": getattr(p, "source", None),
        # Entity-rooms chunk ② — optional per-entity surface-profile override
        # (JSON dict mirroring the surface-domain ``SurfaceProfile``), or
        # ``None`` for legacy pockets. Two-word key → camelCase wire form, like
        # ``templateSlug`` / ``shareLinkToken``.
        "surfaceProfile": getattr(p, "surface_profile", None),
        "createdAt": iso_utc(p.created_at),
        "updatedAt": iso_utc(p.updated_at),
    }


def _widget_to_wire(w) -> dict:
    """Convert a domain ``Widget`` to the legacy wire-format dict. The
    Beanie model's ``model_dump(by_alias=True)`` produces the same shape
    so this just rebuilds it from the frozen dataclass."""
    return {
        "_id": w.id,
        "name": w.name,
        "type": w.type,
        "icon": w.icon,
        "color": w.color,
        "span": w.span,
        "dataSourceType": w.data_source_type,
        "config": dict(w.config),
        "props": dict(w.props),
        "data": w.data,
        # Per-tile rippleSpec subtree the home grid renders; ``None`` for
        # native and legacy widgets.
        "spec": getattr(w, "spec", None),
        "assignedAgent": w.assigned_agent,
        "position": {"row": w.position.row, "col": w.position.col},
    }
