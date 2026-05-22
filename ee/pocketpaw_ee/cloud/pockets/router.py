"""Pockets domain — FastAPI router.

Updated: 2026-04-19 (Cluster B Sub-PR #3) — Added three new routes that
close UI-TESTING-GUIDE §11 gap B5 (no widget layout save/share):

    POST /pockets/{id}/export-layout   — return the pocket's layout as YAML
    POST /pockets/templates            — save a YAML template to "My templates"
    GET  /pockets/templates            — list the workspace's user templates

The YAML + in-process store live in ee.cloud.pockets.layouts. Export is
pure. Template storage is workspace-scoped and in-process for now; the
REST contract matches the MongoDB-backed version that Wave 4 will ship.

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
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    PocketBackendConfigRequest,
    PocketBackendConfigResponse,
    ReorderWidgetsRequest,
    RunActionRequest,
    RunActionResponse,
    RunSourcesRequest,
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
    base_url, auth_type, auth_header, token, _allowed_writes = creds

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
    base_url, auth_type, auth_header, token, allowed_writes = creds

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
    return RunActionResponse(**result)


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
