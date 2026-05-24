"""Pockets domain — FastAPI router.

Updated: 2026-04-19 (Cluster B Sub-PR #3) — Added three new routes that
close UI-TESTING-GUIDE §11 gap B5 (no widget layout save/share):

    POST /pockets/{id}/export-layout   — return the pocket's layout as YAML
    POST /pockets/templates            — save a YAML template to "My templates"
    GET  /pockets/templates            — list the workspace's user templates

The YAML + in-process store live in ee.cloud.pockets.layouts. Export is
pure. Template storage is workspace-scoped and in-process for now; the
REST contract matches the MongoDB-backed version that Wave 4 will ship.

Updated: 2026-05-21 — added ``GET /pockets/home``, the home-as-pocket
foundation. It resolves-or-provisions the caller's home pocket via
``pockets_service.ensure_home_pocket`` and returns a typed
``HomePocketResponse`` (``{pocket_id, pocket, created}``) — ``created`` is
True only when the call just provisioned a brand-new home pocket, so the
client can gate one-time seeding/migration on it. Declared ahead of
``GET /{pocket_id}`` so the static ``/home`` segment wins the route match.
Updated: 2026-05-21 (RFC 04 alpha) — Added three routes for the per-pocket
backend binding + read-only source-run feature:

    PUT  /pockets/{id}/backend       — bind a pocket to one backend
    GET  /pockets/{id}/backend       — read the binding summary (no token)
    POST /pockets/{id}/sources/run   — run the spec's read-only GET sources

Updated: 2026-05-21 (PR #1177 security pass) — added the missing
DELETE /pockets/{id}/backend route so a configured credential can be
revoked; the GET route now requires pocket edit access (owner/editor),
matching the PUT route; the source-run route threads user_id into the
executor for per-user rate limiting + audit logging.

Updated: 2026-05-22 (RFC 05 M2a) — added the write-action routes:

    POST /pockets/{id}/actions/run        — run a declared write action
    PUT  /pockets/{id}/backend/write-policy — set the write allowlist

The action-run route is gated OWNER or explicit shared_with ONLY
(``require_pocket_action_run``) — narrower than source-run, because a
write has blast radius. The write-policy route is owner-only.

Updated: 2026-05-22 (RFC 05 M2b.1) — the action-run route now branches on
the executor's ``instinct_pending`` sentinel: a ``requires_instinct``
write is routed into an Instinct Action via ``instinct_bridge`` and the
route returns ``{ok:true, code:"instinct_pending", proposed_action_id}``
instead of firing. A fired, non-gated write emits a ``pocket.outcome``
event (M2b.2) when its binding declares an ``outcome``. Added the
owner-only ``PUT /pockets/{id}/backend/approval-route`` for the
per-pocket gated-write approver routing.

Updated: 2026-05-22 (security-review fix for PR #1183, SHOULD-FIX 2) —
``run_pocket_action`` now asserts the executor-internal ``_park`` blob is
absent from the wire dict before constructing ``RunActionResponse`` (the
DTO is also ``extra="forbid"``), so a resolved write path/params can
never leak onto the response if the strip drifts.

Updated: 2026-05-22 (RFC 04 M3) — added the webhook-refresh routes:

    POST /pockets/{id}/sources/{source}/refresh — inbound webhook trigger
    GET  /pockets/{id}/backend/webhook          — read the webhook secret
    POST /pockets/{id}/backend/webhook/rotate   — rotate the webhook secret

The inbound refresh route is authenticated by a per-pocket SECRET carried
in the ``X-Pocket-Webhook-Secret`` header — NOT by the cookie/Bearer auth
chain — so an upstream system can trigger a refresh without a user
session. A wrong / missing secret returns the SAME 403 whether or not the
pocket exists, so the endpoint is not a tenant-existence oracle. The two
secret-management routes are owner-only.

Updated: 2026-05-24 (#1206 part a) — added the tool-run wire stub:

    POST /pockets/{id}/tools/run — invoke a named server-side tool

The endpoint is the click-driven sibling of ``/sources/run`` (read-only
hydration) and ``/actions/run`` (named write binding). It accepts a tool
name plus pre-resolved args from the new ``invoke_tool`` Ripple action
verb and runs the named tool against the per-pocket allowlist. The
allowlist is intentionally empty in part (a) so the wire is locked down
before any tool can actually fire; the home-grid ``onEvent`` plumbing
that POSTs to this route lands in part (b), and Composio / WebFetch
routing through the real tool registry is a follow-up. Auth gating
mirrors ``/actions/run`` (owner OR explicit ``shared_with``) — a tool
invocation has the same blast radius as a write binding.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    BulkDispatchResponse,
    CreatePocketRequest,
    DispatchBulkRequest,
    HomePocketResponse,
    PocketBackendConfigRequest,
    PocketBackendConfigResponse,
    ReorderWidgetsRequest,
    RunActionRequest,
    RunActionResponse,
    RunSourcesRequest,
    RunToolRequest,
    RunToolResponse,
    SetApprovalRouteRequest,
    SetWritePolicyRequest,
    ShareLinkRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
)
from pocketpaw_ee.cloud.pockets.layouts import (
    UserPocketTemplate,
    UserTemplateStore,
    export_layout_yaml,
    get_user_template_store,
    parse_layout_yaml,
)
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_pocket_action_run,
    require_pocket_edit,
    require_pocket_owner,
)

router = APIRouter(prefix="/pockets", tags=["Pockets"], dependencies=[Depends(require_license)])


# ---------------------------------------------------------------------------
# Layout export + user templates — Cluster B Sub-PR #3.
# ---------------------------------------------------------------------------


class ExportLayoutRequest(BaseModel):
    """Optional overrides on the metadata block of the exported YAML.

    The pocket's own name / description / category seed the defaults —
    the override fields let the operator ship the template under a
    different display name without renaming the source pocket. Empty
    fields fall back to the pocket's values.
    """

    name: str | None = None
    description: str | None = None
    category: str | None = None


class ExportLayoutResponse(BaseModel):
    pocket_id: str
    yaml: str


class CreateTemplateRequest(BaseModel):
    """Body for POST /pockets/templates.

    ``yaml_source`` is the YAML a previous /export-layout call produced
    or a hand-authored equivalent. ``name`` / ``description`` /
    ``category`` are required on the template row even when the YAML
    carries them — the store indexes on those fields for the gallery.
    """

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    category: str = "custom"
    yaml_source: str = Field(min_length=1)


class UserTemplateResponse(BaseModel):
    id: str
    workspace_id: str
    owner_id: str
    name: str
    description: str
    category: str
    spec: dict
    created_at: str


@router.post("/{pocket_id}/export-layout", response_model=ExportLayoutResponse)
async def export_layout(
    pocket_id: str,
    body: ExportLayoutRequest | None = None,
    user_id: str = Depends(current_user_id),
) -> ExportLayoutResponse:
    """Serialise this pocket's layout as YAML.

    Read-only, safe on any pocket the caller can fetch. The YAML is
    deterministic (sort_keys=True) so a round-trip save-then-create
    reproduces the original layout byte-identically — the PR's e2e
    test depends on that guarantee.
    """

    body = body or ExportLayoutRequest()
    pocket = await pockets_service.get(pocket_id, user_id)
    widgets_dump = pocket.get("widgets") or []
    yaml_text = export_layout_yaml(
        pocket_id=pocket_id,
        name=body.name or pocket.get("name", ""),
        description=body.description or pocket.get("description", ""),
        category=body.category or pocket.get("type", "custom"),
        ripple_spec=pocket.get("rippleSpec"),
        widgets=widgets_dump,
    )
    return ExportLayoutResponse(pocket_id=pocket_id, yaml=yaml_text)


@router.post("/templates", response_model=UserTemplateResponse)
async def create_user_template(
    body: CreateTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
    store: UserTemplateStore = Depends(get_user_template_store),
) -> UserTemplateResponse:
    """Persist a user-defined YAML template under the caller's workspace.

    The template shows up in PocketTemplates's "My templates" category
    once Cluster B's frontend wires the read side. Malformed YAML
    returns 400 with a human-readable message instead of 500 — the UI
    surfaces the error inline on the Save-as-template dialog.
    """

    try:
        spec = parse_layout_yaml(body.yaml_source)
    except ValueError as exc:
        raise CloudError(400, "layout.invalid_yaml", str(exc)) from None

    row = store.save(
        UserPocketTemplate(
            id=uuid4().hex,
            workspace_id=workspace_id,
            owner_id=user_id,
            name=body.name,
            description=body.description,
            category=body.category,
            spec=spec,
        ),
    )
    return UserTemplateResponse(**row.to_dict())


@router.get("/templates", response_model=list[UserTemplateResponse])
async def list_user_templates(
    workspace_id: str = Depends(current_workspace_id),
    store: UserTemplateStore = Depends(get_user_template_store),
) -> list[UserTemplateResponse]:
    """List user-defined templates for the caller's active workspace."""

    return [UserTemplateResponse(**row.to_dict()) for row in store.list_for_workspace(workspace_id)]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("")
async def create_pocket(
    body: CreatePocketRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.create(workspace_id, user_id, body)


@router.get("")
async def list_pockets(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
    project_id: str | None = Query(default=None, alias="project_id"),
) -> list[dict]:
    return await pockets_service.list_pockets(workspace_id, user_id, project_id=project_id)


@router.get("/home", response_model=HomePocketResponse)
async def get_home_pocket(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> HomePocketResponse:
    """Resolve-or-provision the caller's home pocket.

    Declared ahead of ``GET /{pocket_id}`` so the static ``/home`` segment
    is matched before the pocket-id wildcard. Returns
    ``{pocket_id, pocket, created}`` where ``pocket`` is the full wire dict
    (rippleSpec + widgets) and ``created`` is ``True`` only when this call
    just provisioned a brand-new home pocket. The client gates one-time
    work — seeding default widgets, migrating legacy localStorage widgets —
    on ``created``.
    """
    pocket, created = await pockets_service.ensure_home_pocket(workspace_id, user_id)
    return HomePocketResponse(pocket_id=pocket["_id"], pocket=pocket, created=created)


@router.get("/{pocket_id}")
async def get_pocket(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.get(pocket_id, user_id)


@router.patch("/{pocket_id}", dependencies=[Depends(require_pocket_edit)])
async def update_pocket(
    pocket_id: str,
    body: UpdatePocketRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update(pocket_id, user_id, body)


@router.delete("/{pocket_id}", status_code=204, dependencies=[Depends(require_pocket_owner)])
async def delete_pocket(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.delete(pocket_id, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Backend binding + read-only source run (RFC 04 alpha)
# ---------------------------------------------------------------------------


@router.put("/{pocket_id}/backend", dependencies=[Depends(require_pocket_edit)])
async def set_pocket_backend(
    pocket_id: str,
    body: PocketBackendConfigRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketBackendConfigResponse:
    """Bind this pocket to one external backend (base URL + auth credential).

    The token is encrypted server-side; the response never echoes it back.
    A bad base URL (non-https, internal host) yields a 400.
    """
    result = await pockets_service.set_pocket_backend(
        workspace_id,
        user_id,
        pocket_id,
        body.base_url,
        body.auth_type,
        body.auth_token,
        body.auth_header,
    )
    return PocketBackendConfigResponse(**result)


@router.get("/{pocket_id}/backend", dependencies=[Depends(require_pocket_edit)])
async def get_pocket_backend(
    pocket_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketBackendConfigResponse:
    """Read this pocket's backend binding summary. Never returns the token.

    Requires pocket **edit** access — backend config metadata is
    owner/editor-facing, consistent with the PUT route. A 404 here means
    "no backend configured" for this pocket.
    """
    # Mirror get_pocket's access check before exposing the binding.
    await pockets_service.get(pocket_id, user_id)
    result = await pockets_service.get_pocket_backend(workspace_id, pocket_id)
    if result is None:
        raise CloudError(404, "pocket_backend.not_found", "No backend configured for this pocket")
    return PocketBackendConfigResponse(**result)


@router.delete(
    "/{pocket_id}/backend",
    status_code=204,
    dependencies=[Depends(require_pocket_owner)],
)
async def delete_pocket_backend(
    pocket_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    """Revoke this pocket's backend binding — deletes the stored credential.

    Requires pocket **owner** access. Idempotent: a pocket with no backend
    configured still returns 204.
    """
    await pockets_service.remove_pocket_backend(workspace_id, user_id, pocket_id)
    return Response(status_code=204)


@router.post("/{pocket_id}/sources/run")
async def run_pocket_sources(
    pocket_id: str,
    body: RunSourcesRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Run the pocket's read-only ``rippleSpec.sources`` against its backend.

    Read access mirrors ``get_pocket`` — deliberately NOT gated on edit
    access. Any pocket reader may run already-authored sources: that is the
    core shared-live-pocket UX, where a viewer triggers the ``pocket_open``
    refresh of a shared dashboard. A viewer cannot change the backend or the
    source paths (both are edit-only), so the SSRF hardening in
    ``source_executor`` plus the immutable, edit-authored source list bound
    the risk to "fetch the same GET bindings the editors already approved".

    The hydrated state is returned in THIS response body — there is no
    ``pocket_mutation`` SSE emit, because the run endpoint is a standalone
    REST call outside any SSE stream.
    """
    pocket = await pockets_service.get(pocket_id, user_id)
    ripple_spec = pocket.get("rippleSpec") or {}

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        raise CloudError(
            400,
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one via PUT /pockets/{id}/backend",
        )
    # M2b.1 — the executor-creds tuple gained `allowed_writes` (M2a) and
    # `approval_route` (M2b.1); read-only source runs need neither.
    base_url, auth_type, auth_header, token, _allowed_writes, _approval_route = creds

    from pocketpaw_ee.cloud.pockets import source_executor

    # no-event: source hydration is response-body delivery, not persisted
    return await source_executor.run_sources(
        pocket_id=pocket_id,
        user_id=user_id,
        ripple_spec=ripple_spec,
        base_url=base_url,
        auth_type=auth_type,
        auth_header=auth_header,
        token=token,
        trigger=body.trigger,
        only_source=body.source,
    )


# ---------------------------------------------------------------------------
# Webhook refresh (RFC 04 M3)
# ---------------------------------------------------------------------------


class WebhookSecretResponse(BaseModel):
    """The pocket's webhook secret + the inbound URL to call with it.

    Owner-facing. ``secret`` is the value an upstream system echoes back in
    the ``X-Pocket-Webhook-Secret`` header on the refresh call. ``secret``
    is ``None`` until an owner runs a rotate.
    """

    pocket_id: str
    secret: str | None
    refresh_path: str


@router.post("/{pocket_id}/sources/{source}/refresh")
async def webhook_refresh_source(
    pocket_id: str,
    source: str,
    x_pocket_webhook_secret: str | None = Header(default=None),
) -> dict:
    """Re-run one ``"webhook"``-refresh source — triggered by an upstream
    system, authenticated by the per-pocket webhook secret.

    This route is DELIBERATELY outside the cookie/Bearer auth chain: an
    upstream backend has no PocketPaw user session. Authentication is the
    secret carried in ``X-Pocket-Webhook-Secret`` — generated server-side,
    stored on the backend-credential row, never in the spec.

    Security:
    * A wrong / missing / unset secret, a pocket with no backend, and a
      genuinely-missing pocket ALL return the SAME ``403`` — the endpoint
      reveals nothing about whether a pocket id exists.
    * The secret compare is constant-time (``secrets.compare_digest`` in
      the service).
    * The refresh is metered by the per-pocket auto-refresh budget — a
      webhook flood cannot run up unbounded backend cost; over-budget
      hits are skipped (HTTP 200 with a ``skipped`` marker), not queued.
    * The named source must actually carry ``"webhook"`` in its refresh
      policy — a webhook hit cannot run an arbitrary source.
    """
    from pocketpaw_ee.cloud.pockets import _refresh_budget, source_executor
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    creds = await pockets_service.resolve_webhook_pocket(pocket_id, x_pocket_webhook_secret or "")
    if creds is None:
        # Identical error for every failure mode — not a pocket oracle.
        raise CloudError(403, "pocket_webhook.unauthorized", "Invalid or missing webhook secret")
    base_url, auth_type, auth_header, token, _allowed, _route, workspace_id = creds

    ripple_spec = await pockets_service.get_pocket_ripple_spec(workspace_id, pocket_id)
    if ripple_spec is None:
        raise CloudError(403, "pocket_webhook.unauthorized", "Invalid or missing webhook secret")

    # The named source must exist AND opt into webhook refresh — a valid
    # secret does not grant the right to run a non-webhook source.
    sources = ripple_spec.get("sources")
    binding = sources.get(source) if isinstance(sources, dict) else None
    if not isinstance(binding, dict) or "webhook" not in (binding.get("refresh") or []):
        raise CloudError(
            404,
            "pocket_webhook.source_not_found",
            f"no webhook-refresh source named '{source}' on this pocket",
        )

    # Per-pocket auto-refresh budget — separate from the manual limiter.
    if not await _refresh_budget.consume_auto_refresh(pocket_id):
        return {"ran": [], "errors": [], "skipped": "rate_limited"}

    # no-event: webhook refresh is response-body delivery, not persisted.
    return await source_executor.run_sources(
        pocket_id=pocket_id,
        user_id="system:webhook-refresh",
        ripple_spec=ripple_spec,
        base_url=base_url,
        auth_type=auth_type,
        auth_header=auth_header,
        token=token,
        only_source=source,
    )


@router.get(
    "/{pocket_id}/backend/webhook",
    dependencies=[Depends(require_pocket_owner)],
)
async def get_pocket_webhook_secret(
    pocket_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> WebhookSecretResponse:
    """Read this pocket's webhook secret. Owner-only.

    The secret IS the credential an upstream caller echoes back, so the
    owner can read it to configure the upstream. ``secret`` is ``None``
    until a rotate generates one. Returns ``400`` when the pocket has no
    backend configured.
    """
    secret = await pockets_service.get_webhook_secret(workspace_id, pocket_id)
    return WebhookSecretResponse(
        pocket_id=pocket_id,
        secret=secret,
        refresh_path=f"/api/v1/pockets/{pocket_id}/sources/{{source}}/refresh",
    )


@router.post(
    "/{pocket_id}/backend/webhook/rotate",
    dependencies=[Depends(require_pocket_owner)],
)
async def rotate_pocket_webhook_secret(
    pocket_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> WebhookSecretResponse:
    """Generate a fresh webhook secret for this pocket. Owner-only.

    Rotating invalidates the previous secret immediately — any upstream
    still sending the old value gets a 403 until reconfigured. Returns
    ``400`` when the pocket has no backend configured.
    """
    secret = await pockets_service.rotate_webhook_secret(workspace_id, user_id, pocket_id)
    return WebhookSecretResponse(
        pocket_id=pocket_id,
        secret=secret,
        refresh_path=f"/api/v1/pockets/{pocket_id}/sources/{{source}}/refresh",
    )


@router.put(
    "/{pocket_id}/backend/write-policy",
    dependencies=[Depends(require_pocket_owner)],
)
async def set_pocket_write_policy(
    pocket_id: str,
    body: SetWritePolicyRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketBackendConfigResponse:
    """Set this pocket's write allowlist (RFC 05 M2a). Owner-only.

    Replaces the whole ``allowed_writes`` list — an empty list revokes
    every write (fail-closed). The policy lives on the backend-credential
    row, OUTSIDE the spec, so the agent that authors the spec cannot widen
    its own write blast radius. Returns ``400`` when the pocket has no
    backend configured — a write policy with no backend to apply to is
    meaningless.
    """
    result = await pockets_service.set_pocket_write_policy(
        workspace_id,
        user_id,
        pocket_id,
        [rule.model_dump() for rule in body.allowed_writes],
    )
    return PocketBackendConfigResponse(**result)


@router.put(
    "/{pocket_id}/backend/approval-route",
    dependencies=[Depends(require_pocket_owner)],
)
async def set_pocket_approval_route(
    pocket_id: str,
    body: SetApprovalRouteRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketBackendConfigResponse:
    """Set who approves this pocket's ``requires_instinct`` writes
    (RFC 05 M2b.1). Owner-only.

    A ``mode="user"`` route names a workspace member as the approver —
    the service validates that id is a current member. An omitted body
    (or ``route=null``) clears the route back to the default: the pocket
    owner. Returns ``400`` when the pocket has no backend configured.
    """
    body = body or SetApprovalRouteRequest()
    route = body.route.model_dump() if body.route is not None else None
    result = await pockets_service.set_pocket_approval_route(
        workspace_id,
        user_id,
        pocket_id,
        route,
    )
    return PocketBackendConfigResponse(**result)


@router.post(
    "/{pocket_id}/actions/run",
    dependencies=[Depends(require_pocket_action_run)],
)
async def run_pocket_action(
    pocket_id: str,
    body: RunActionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> RunActionResponse:
    """Run one declared ``rippleSpec.actions`` write action against the
    pocket's backend.

    Access is OWNER or explicit ``shared_with`` ONLY — a write has blast
    radius, so a workspace-visible pocket does NOT grant run access. The
    HTTP ``method`` is read server-side from the persisted action entry;
    the client only sends the resolved ``path`` / ``params``. The write
    fires only if the human owner allow-listed the method+path.

    RFC 05 M2b.1 — a binding marked ``requires_instinct`` is NOT fired
    here. The executor validates the write then returns an
    ``instinct_pending`` sentinel; this route hands the parked write to
    ``instinct_bridge.propose_pocket_write`` and returns
    ``{ok:true, code:"instinct_pending", proposed_action_id}``. The
    actual write fires later, from the instinct router's approve hook.

    On a fired (non-pending) success the route emits a ``pocket.outcome``
    event when the binding declares an ``outcome`` (M2b.2).

    The backend's response is delivered in THIS response body — there is
    no ``pocket_mutation`` SSE emit, because the run endpoint is a
    standalone REST call outside any SSE stream. The client applies the
    ``on_success`` / ``on_error`` reconcile handlers.
    """
    pocket = await pockets_service.get(pocket_id, user_id)
    ripple_spec = pocket.get("rippleSpec") or {}
    actions = ripple_spec.get("actions")
    if not isinstance(actions, dict) or body.action not in actions:
        return RunActionResponse(
            ok=False,
            action=body.action,
            error=f"no action named '{body.action}' on this pocket",
            code="action_not_found",
        )
    raw_action = actions[body.action]
    if not isinstance(raw_action, dict):
        return RunActionResponse(
            ok=False,
            action=body.action,
            error=f"action '{body.action}' is malformed",
            code="bad_binding",
        )

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        raise CloudError(
            400,
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one via PUT /pockets/{id}/backend",
        )
    base_url, auth_type, auth_header, token, allowed_writes, approval_route = creds

    from pocketpaw_ee.cloud.pockets import action_executor

    # no-event: the write result is response-body delivery, not persisted.
    result = await action_executor.run_action(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        action=body.action,
        raw_action=raw_action,
        path=body.path,
        params=body.params,
        base_url=base_url,
        auth_type=auth_type,
        auth_header=auth_header,
        token=token,
        allowed_writes=allowed_writes,
        idempotency_key=body.idempotency_key,
    )

    # M2b.1 — a `requires_instinct` write was PARKED, not fired. The
    # executor validated it (a write the allowlist rejects already came
    # back `ok:false`); `_park` carries the resolved write. Route it into
    # an Instinct Action and return the pending response.
    if result.get("code") == "instinct_pending":
        from pocketpaw_ee.cloud.pockets import instinct_bridge

        proposed_id = await instinct_bridge.propose_pocket_write(
            pocket=pocket,
            backend_config={
                "base_url": base_url,
                "auth_type": auth_type,
                "allowed_writes": allowed_writes,
                "approval_route": approval_route,
            },
            parked_write=result["_park"],
            requested_by=user_id,
        )
        return RunActionResponse(
            ok=True,
            action=body.action,
            code="instinct_pending",
            proposed_action_id=proposed_id,
        )

    # M2b.2 — a direct (non-gated) write succeeded. Emit its outcome when
    # the binding declared one. A binding with no `outcome` is a no-op.
    if result.get("ok"):
        from pocketpaw_ee.cloud.outcomes import service as outcomes_service

        await outcomes_service.emit_pocket_outcome(
            outcome=result.get("outcome"),
            pocket_id=pocket_id,
            workspace_id=workspace_id,
            action=body.action,
            actor=user_id,
            via_instinct=False,
            instinct_action_id=None,
        )

    # Strip executor-internal keys (`_park`, `outcome`) the wire model
    # does not carry before building the response. SHOULD-FIX 2
    # (PR #1183) — the strip is defensive: `_park` carries the resolved
    # write path/params and must NEVER reach the wire. The explicit
    # assertion below catches a strip that drifts out of sync with the
    # executor's result keys; `RunActionResponse` is also `extra="forbid"`
    # so a missed key fails construction rather than leaking.
    wire = {k: v for k, v in result.items() if k not in ("_park", "outcome")}
    assert "_park" not in wire, "executor `_park` blob must be stripped before the wire response"
    return RunActionResponse(**wire)


@router.post(
    "/{pocket_id}/actions/{action_name}/dispatch-bulk",
    dependencies=[Depends(require_pocket_action_run)],
)
async def dispatch_bulk_action_route(
    pocket_id: str,
    action_name: str,
    body: DispatchBulkRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> BulkDispatchResponse:
    """Fan out a ``kind: bulk`` action across the rows in ``body.rows``
    (RFC 03 v2 Wave 3b).

    The path parameters carry the pocket id + the bulk action name;
    ``body.rows`` carries the per-row payloads the operator selected.
    Bucketing follows the RFC contract:

    * ``executions`` — rows ready to fire (verdict ``EXECUTE`` or
      ``NOTIFY_AND_EXECUTE``). Fired through the action executor with
      the gate skipped (the OSS planner already evaluated it).
    * ``blocked`` — rows the Instinct composer blocked.
    * ``batch_approval_id`` — set when ANY row escalated to approval;
      exactly ONE InstinctApproval row covers every approval-needing
      row in the batch (RFC mandate — never N approvals).

    Wave 3e — template resolution is now wired through
    ``pockets.service.resolve_pocket_template`` which reads the pocket's
    ``template_slug`` and loads the OSS bundled template. A pocket with
    no slug, an unknown slug, or a stale on-disk template surfaces as
    ``404 pocket_template.not_found`` so the operator can fix the
    template binding rather than receive a generic 500.
    """
    body = body or DispatchBulkRequest(
        pocket_id=pocket_id,
        action_name=action_name,
        rows=[],
    )
    # Mirror the URL params onto the body (the body model carries them
    # so internal callers can hit the service directly).
    body_dict = body.model_dump()
    body_dict["pocket_id"] = pocket_id
    body_dict["action_name"] = action_name

    # Wave 3e — resolve the pocket's RFC 03 v2 template. ``None`` means
    # the pocket has no slug, the slug is unknown, or the on-disk
    # template is stale; treat as 404 so the operator is signalled to
    # fix the binding (vs. a 500 / generic error).
    template = await pockets_service.resolve_pocket_template(workspace_id, pocket_id)
    if template is None:
        raise CloudError(
            404,
            "pocket_template.not_found",
            (
                "No RFC 03 v2 template is bound to this pocket — set "
                "``template_slug`` on the pocket before dispatching a "
                "bulk action."
            ),
        )

    result_wire = await pockets_service.dispatch_bulk_action(
        workspace_id,
        user_id,
        body_dict,
        template=template,
    )
    return BulkDispatchResponse(**result_wire)


# ---------------------------------------------------------------------------
# Pocket tool invocation (#1206 part a — invoke_tool wire)
# ---------------------------------------------------------------------------


@router.post(
    "/{pocket_id}/tools/run",
    dependencies=[Depends(require_pocket_action_run)],
)
async def run_pocket_tool(
    pocket_id: str,
    body: RunToolRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> RunToolResponse:
    """Invoke a named server-side tool with the resolved args from the
    ``invoke_tool`` Ripple action verb (#1206 part a).

    Click-driven sibling of ``/sources/run`` (read-only hydration) and
    ``/actions/run`` (named write binding). The home grid's ``onEvent``
    plumbing (part b) POSTs here when a ripple button fires
    ``{action: "invoke_tool", tool: "X", args: {...}}``.

    Access is OWNER or explicit ``shared_with`` ONLY — a tool invocation
    has the same blast radius as a write binding, so a workspace-visible
    pocket does NOT grant run access.

    Part (a) is intentionally fail-closed: the per-pocket allowlist is
    empty (see :func:`tool_executor.get_pocket_allowed_tools`), so every
    tool name returns ``ok:false, code:"not_allowed"``. The wire shape +
    DTOs land here so part (b) (the home-grid ``onEvent`` wiring) has
    somewhere to POST to. Part (c) adds Composio / WebFetch routing
    through the real tool registry behind the allowlist.

    The result is delivered in THIS response body — there is no
    ``pocket_mutation`` SSE emit, because the run endpoint is a
    standalone REST call outside any SSE stream. The client applies the
    ``on_success`` / ``on_error`` reconcile handlers.
    """
    # Ensure the caller can actually see this pocket — `get` raises
    # NotFound when the pocket is missing or not in the user's scope, so
    # we don't expose a tenant-existence oracle via the tool wire either.
    await pockets_service.get(pocket_id, user_id)

    from pocketpaw_ee.cloud.pockets import tool_executor

    allowed_tools = await tool_executor.get_pocket_allowed_tools(workspace_id, pocket_id)

    # no-event: the tool result is response-body delivery, not persisted.
    result = await tool_executor.run_tool(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        tool=body.tool,
        args=body.args,
        allowed_tools=allowed_tools,
    )
    return RunToolResponse(**result)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/widgets")
async def add_widget(
    pocket_id: str,
    body: AddWidgetRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.add_widget(pocket_id, user_id, body)


@router.patch("/{pocket_id}/widgets/{widget_id}")
async def update_widget(
    pocket_id: str,
    widget_id: str,
    body: UpdateWidgetRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update_widget(pocket_id, widget_id, user_id, body)


@router.delete("/{pocket_id}/widgets/{widget_id}", status_code=204)
async def remove_widget(
    pocket_id: str,
    widget_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_widget(pocket_id, widget_id, user_id)
    return Response(status_code=204)


@router.post("/{pocket_id}/widgets/reorder")
async def reorder_widgets(
    pocket_id: str,
    body: ReorderWidgetsRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.reorder_widgets(pocket_id, user_id, body.widget_ids)


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/team")
async def add_team_member(
    pocket_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.add_team_member(pocket_id, user_id, body["member_id"])


@router.delete("/{pocket_id}/team/{member_id}", status_code=204)
async def remove_team_member(
    pocket_id: str,
    member_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_team_member(pocket_id, user_id, member_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/agents")
async def add_agent(
    pocket_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
) -> dict:
    agent_id = body.get("agentId") or body.get("agent_id")
    return await pockets_service.add_agent(pocket_id, user_id, agent_id)


@router.delete("/{pocket_id}/agents/{agent_id}", status_code=204)
async def remove_agent(
    pocket_id: str,
    agent_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_agent(pocket_id, user_id, agent_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Sharing — Share links
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/share", dependencies=[Depends(require_pocket_owner)])
async def generate_share_link(
    pocket_id: str,
    body: ShareLinkRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.generate_share_link(pocket_id, user_id, body.access)


@router.delete("/{pocket_id}/share", status_code=204, dependencies=[Depends(require_pocket_owner)])
async def revoke_share_link(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.revoke_share_link(pocket_id, user_id)
    return Response(status_code=204)


@router.patch("/{pocket_id}/share", dependencies=[Depends(require_pocket_owner)])
async def update_share_link_access(
    pocket_id: str,
    body: ShareLinkRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update_share_link(pocket_id, user_id, body.access)


@router.get("/shared/{token}")
async def access_via_share_link(token: str) -> dict:
    return await pockets_service.access_via_share_link(token)


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


@router.post(
    "/{pocket_id}/collaborators",
    status_code=204,
    dependencies=[Depends(require_pocket_owner)],
)
async def add_collaborator(
    pocket_id: str,
    body: AddCollaboratorRequest,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.add_collaborator(pocket_id, user_id, body)
    return Response(status_code=204)


@router.delete(
    "/{pocket_id}/collaborators/{target_user_id}",
    status_code=204,
    dependencies=[Depends(require_pocket_owner)],
)
async def remove_collaborator(
    pocket_id: str,
    target_user_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_collaborator(pocket_id, user_id, target_user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Sessions under pocket
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/sessions")
async def create_pocket_session(
    pocket_id: str,
    body: CreateSessionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    from pocketpaw_ee.cloud.sessions import service as sessions_service
    from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

    body_with_pocket = CreateSessionRequest(
        title=body.title,
        pocket_id=pocket_id,
        group_id=body.group_id,
        agent_id=body.agent_id,
        session_id=body.session_id,
    )
    ctx = sessions_service.legacy_ctx(user_id, workspace_id)
    session = await sessions_service.create(ctx, workspace_id, body_with_pocket)
    return session_to_wire_dict(session)


@router.get("/{pocket_id}/sessions")
async def list_pocket_sessions(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    from pocketpaw_ee.cloud.sessions import service as sessions_service
    from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

    ctx = sessions_service.legacy_ctx(user_id)
    items = await sessions_service.list_for_pocket(ctx, pocket_id)
    return [session_to_wire_dict(s) for s in items]
