"""Pockets domain — request/response schemas and the wire serializers.

Holds the Pydantic models for every pockets route plus ``pocket_to_wire_dict`` /
``_widget_to_wire``, which convert a frozen domain object into the LEGACY wire
dict the frontend has always received. That dict is not the Beanie model dumped:
it is byte-equivalent to the older ``_pocket_response`` helper, and staying that
way is the point of it living in one function.

INVARIANTS a reader must not break:

* ``pocket_to_wire_dict``'s ``source_visible`` IS REQUIRED AND MUST STAY THAT
  WAY. It decides whether a site's authored source reaches the wire, and it has
  no default so a call site that forgets it is a TypeError rather than a leak.
  The only default that would not break the build pipeline is a fail-open one.
  Its own docstring has the full reasoning.
* MULTI-WORD WIRE KEYS ARE camelCase (``templateSlug``, ``shareLinkToken``,
  ``keepsClientBundle``), SINGLE-WORD ONES ARE NOT (``pattern``, ``engine``,
  ``source``). Every new field picks a side by that rule, not by taste.
* ``keepsClientBundle`` IS TRI-STATE ON THE WIRE. It is emitted as ``None`` when
  the author declared nothing — including for every legacy pocket — so publish
  can tell "undeclared" from an explicit ``False`` and apply
  ``sites_keep_client_bundle_default`` to only the former. Coercing it to a bool
  here erases that distinction before publish ever sees it.
* ``RunActionResponse`` IS ``extra="forbid"``. An executor-internal key
  (``_park``, ``outcome``) that a router fails to strip must raise on
  construction rather than leak the resolved write path and params onto the wire.
* READ-TIME rippleSpec NORMALIZATION IS IDEMPOTENT. Pockets persisted before the
  agent-alias safety net (``root`` / ``tree`` lifted into ``ui``) are repaired in
  flight, with no DB rewrite; a spec already canonical passes through unchanged.
* ``PocketSurfaceProfile`` IS IMPORTED FROM ``surface.domain``. Importing it from
  ``models.pocket`` instead would break the OSS-EE boundary contract.
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
    # for svelte sites, the hand-written SvelteKit source ENVELOPE
    # ``{relative_path: file_contents}``, which for a DYNAMIC svelte site also
    # carries the live-data bindings (``objects``/``sources``/``actions``/
    # ``auth``) as sibling keys — hence ``dict[str, Any]`` (DSV-5). Omitting them
    # persists the ripple defaults (``engine="ripple"``, ``source=None``).
    engine: str = "ripple"
    source: dict[str, Any] | None = None
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
    span: str | None = None
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
    # Paw Sites generation track (``"ripple"`` | ``"svelte"``) and the svelte
    # source envelope (or ``None`` for ripple pockets). For a dynamic svelte
    # site the envelope carries the live-data bindings (``objects``/``sources``/
    # ``actions``/``auth``) as siblings on the file map — hence
    # ``dict[str, Any]`` (DSV-5).
    engine: str = "ripple"
    source: dict[str, Any] | None = None
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


class ToolGrantDTO(BaseModel):
    """One tool-allowlist entry on the wire (feat/invoke-tool-v1).

    Mirrors ``models.pocket_backend.ToolGrant``. ``tool`` is either a
    built-in tool name (``"web_fetch"``) or a connector action
    (``"connector:<name>:<action>"``, e.g. ``"connector:github:list_issues"``).
    """

    tool: str = Field(min_length=1)


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

    ``allowed_tools`` is the per-pocket tool allowlist (feat/invoke-tool-v1)
    — the same fail-closed posture: empty by default, no ``invoke_tool``
    fires until a human allow-lists it.

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
    allowed_tools: list[ToolGrantDTO] = Field(default_factory=list)
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
    # Layered/learning gate (T6/T10) — set only on an OPTIMISTIC-lane write:
    # the id of the bounded compensation handle the client can roll back via
    # the optimistic rollback endpoint. None on every other path. The
    # executor returns it as ``_optimistic_compensation_id`` (internal key);
    # the router maps it onto this public field.
    optimistic_compensation_id: str | None = None
    # Workspace jobs (pp#1459) — set ONLY on a ``kind:"job"`` action dispatch
    # (``code:"job_enqueued"``). Carries the WorkspaceJobDoc id the client polls
    # via ``GET /workspaces/{ws}/jobs/{job_id}``. None on every other path.
    job_id: str | None = None
    on_success: list[dict] = Field(default_factory=list)
    on_error: list[dict] = Field(default_factory=list)


class SetWritePolicyRequest(BaseModel):
    """Body for ``PUT /pockets/{id}/backend/write-policy``.

    Replaces the pocket's whole write allowlist. An empty list is valid
    and meaningful — it revokes every write (fail-closed).
    """

    allowed_writes: list[AllowedWriteDTO] = Field(default_factory=list)


class SetToolPolicyRequest(BaseModel):
    """Body for ``PUT /pockets/{id}/backend/tool-policy`` (feat/invoke-tool-v1).

    Replaces the pocket's whole tool allowlist. An empty list is valid
    and meaningful — it revokes every tool (fail-closed), exactly like the
    write-policy precedent.
    """

    allowed_tools: list[ToolGrantDTO] = Field(default_factory=list)


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

    ``proposed_action_id`` carries the pending Instinct Action id when a
    WRITE grant is invoked (feat/invoke-tool-v1 v2): the WRITE is proposed
    for human approval rather than fired inline, so ``ok`` is true,
    ``code`` is ``"instinct_pending"``, and this id lets the client
    correlate the click with the pending Action it can watch in The Tray.
    It mirrors :class:`RunActionResponse.proposed_action_id` so a single
    client branch services gated tool-writes and gated ``call_binding``
    writes. ``None`` for reads / blocked / not-allowed responses. The id
    is ALSO echoed inside ``response`` for callers that read it there.

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
    proposed_action_id: str | None = None
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


def pocket_to_wire_dict(p, *, source_visible: bool) -> dict:
    """Convert a domain ``Pocket`` (from ``ee.cloud.pockets.domain``) to
    the legacy wire-format dict. Byte-equivalent to the
    ``_pocket_response`` helper in ``service.py``.

    Also applies read-time normalization to ``rippleSpec``: old pockets
    persisted before the agent-alias safety net (``root`` / ``tree`` /
    etc. lifted into ``ui``) get fixed in flight without a DB rewrite.
    The normalizer is idempotent — specs already in the canonical
    ``{ui, state}`` shape pass through unchanged.

    ``source_visible`` (SF-2) answers ONE question: may the caller this dict is
    being built for see the site's authored source? ``False`` emits ``None`` in
    its place. Only ``source`` is affected — ``rippleSpec`` is untouched, because
    ripple is a separate authoring track and outside this gate entirely.

    IT IS REQUIRED AND HAS NO DEFAULT, DELIBERATELY. This function is pure and
    synchronous over a frozen domain object, so it cannot resolve an entitlement
    itself; the answer has to come from the caller. A default would decide for
    every caller that forgot to, and the only default that does not break the
    build pipeline is ``True`` — which is to say, a fail-OPEN one. Requiring the
    argument turns a missed call site into a TypeError at import rather than a
    silent leak, which is the entire bug class this gate exists to close. Do not
    add a default to make a call site or a test shorter.

    ``None`` rather than dropping the key, because that is already the shape a
    consumer sees for a pocket with no source at all and for every row of the
    gallery list (which projects ``source`` out of the query). A dropped key
    would be a second, novel absence for callers to handle.
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
        # SF-2 — withheld as ``None`` when the caller may not see it. THE single
        # place source reaches the wire; ``_resolved_wire_dict`` in service.py
        # funnels ~26 of the ~30 call sites through here, which is why gating one
        # expression also covers the REST reads, the create/update return values
        # and the WebSocket broadcast (a socket bypasses the dependency layer, so
        # no route-level gate would ever have reached it).
        "source": getattr(p, "source", None) if source_visible else None,
        # MT-1 — this site keeps its client bundle. camelCased like every other
        # multi-word wire key, which also matches the generator's
        # ``siteConfig.keepsClientBundle``. TRI-STATE: emitted as ``None`` when
        # the author declared nothing (legacy pockets included) so that publish
        # can tell "undeclared" from an explicit ``False`` and apply
        # ``sites_keep_client_bundle_default`` only to the former. Coercing to a
        # bool here would erase that distinction before publish ever sees it.
        "keepsClientBundle": getattr(p, "keeps_client_bundle", None),
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


# ── Per-Pocket Connector Permissions ──────────────────────────────────


class PocketConnectorPermissionsOut(BaseModel):
    """GET /pockets/{id}/connector-permissions response.

    ``allowed_connectors`` is ``None`` when the pocket inherits all workspace
    connectors (default, backward-compatible). A list (possibly empty) means
    the pocket is restricted to only those connectors.
    """

    allowed_connectors: list[str] | None = None


class SetPocketConnectorPermissionsRequest(BaseModel):
    """PUT /pockets/{id}/connector-permissions request.

    Pass ``allowed_connectors=null`` to inherit all workspace connectors.
    Pass ``allowed_connectors=[]`` to revoke all (pocket sees nothing).
    """

    allowed_connectors: list[str] | None = None


class GrantPocketConnectorRequest(BaseModel):
    """POST /pockets/{id}/connector-permissions/grant request."""

    connector_name: str


class RevokePocketConnectorRequest(BaseModel):
    """POST /pockets/{id}/connector-permissions/revoke request."""

    connector_name: str


class WorkspacePocketConnectorPermissionsOut(BaseModel):
    """GET /workspaces/{id}/pocket-connector-permissions response.

    A map of pocket_id → list of allowed connector names. A missing entry
    or ``None`` means the pocket inherits all workspace connectors.
    """

    permissions: dict[str, list[str] | None]
