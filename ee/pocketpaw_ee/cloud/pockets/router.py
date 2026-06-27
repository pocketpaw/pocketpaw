"""Pockets domain — FastAPI router.

Updated: 2026-06-20 (feat/workspace-jobs, pp#1459) — ``run_pocket_action`` now
branches on ``raw_action["kind"] == "job"`` BEFORE the path-resolution +
``action_executor.run_action`` HTTP-write path. A job action enqueues a named
server-side job (``_dispatch_job`` → ``jobs.service.dispatch_job``) that runs in
the ARQ worker under the synthetic ``system:workspace_job`` identity, and
returns ``{ok:true, code:"job_enqueued", job_id}``. A missing ``kind`` falls
through unchanged (back-compat); ``requires_instinct:true`` on a job is rejected
with ``job.instinct_not_yet_supported`` (deferred to v1.1).
Updated: 2026-06-20 (review fix MINOR A) — ``_dispatch_job`` now REJECTS any
non-empty client ``params`` on a ``kind:"job"`` run with
``job.params_not_accepted`` (400). A job is parameterized only by its declared
author-time params, so accepting per-click input would let a clicker widen the
job's scope — the rejection makes that guarantee explicit instead of a silent
drop.

Updated: 2026-06-15 (feat/invoke-tool-v1) — UNLOCKED ``POST /{id}/tools/run``.
Added the owner-only ``PUT /{id}/backend/tool-policy`` route (the tool-allowlist
analog of write-policy) and rewired ``run_pocket_tool`` to read the per-pocket
``allowed_tools`` off the credential row (via
``pockets_service.get_pocket_backend_for_executor``, 9th tuple element) and pass
the tool NAMES to ``tool_executor.run_tool`` by parameter — never from the spec.
A pocket with no backend / no grants reads as an empty allowlist (fail-closed).

Updated: 2026-06-13 (feat/pocket-template-reconcile, P2.4) — added the
Template Reconcile REST adapter: ``POST /{id}/reconcile/preview`` (dry-run
diff) and ``POST /{id}/reconcile/apply`` (re-apply template-owned regions,
preserve instance state). Both are THIN — they resolve identity and call
``pockets.reconcile`` (the typed service); all reconcile logic lives there,
not here. The apply route carries the ``require_pocket_edit`` guard so it
fails closed at the boundary; the service re-checks too (the canonical
"primitive = service + thin adapters" reference shape). Additive — no
existing route touched.

Updated: 2026-06-10 (W2a — deny-by-default Instinct governance) — the
single-action ``POST /{id}/actions/run`` route now THREADS the pocket's
RFC 03 v2 template into ``action_executor.run_action`` so the template-
level CEL Instinct rules (gate 1.5) finally evaluate on this path (before
W2a only the bulk + temporal dispatchers gated; the direct single-action
run silently skipped CEL). The template is threaded only when it DECLARES
the action being run — ``resolve_instinct`` 404s on an undeclared action,
and a ``rippleSpec.actions`` write binding need not be a template action,
so threading unconditionally would 404 legitimate non-template writes.
When the template does not cover the action, ``template=None`` and the
binding-level deny-by-default gate (gate 7, now defaulting on) governs.
The ``instinct_pending`` handler now distinguishes the two park shapes:
a template-CEL escalation (carries ``approval_id`` — already persisted)
surfaces that id directly, while a binding-level park (no ``approval_id``)
is routed through ``instinct_bridge.propose_pocket_write`` as before. A new
``instinct_blocked`` branch surfaces a clean blocked response.

Updated: 2026-06-08 (fix/pocket-sources-run-400) — ``POST /{id}/sources/run``
no longer 400s when the pocket has no backend configured. The frontend runs
declared sources on every ``pocket_open``, so a blank/starter pocket (no
backend, nothing to fetch) was 400ing on open — surfacing as a noisy
``pocket_open sources run failed: HttpError: Bad Request``. The route now
computes the selected sources first (via ``source_executor.selected_source_keys``)
and returns a clean ``{"ran": [], "errors": []}`` 200 when nothing is
selected; when a runnable source IS authored but no backend is bound it
returns per-source ``pocket_backend.not_configured`` errors (200), matching
the soft/non-fatal handling every other source-run call site already uses.

Updated: 2026-06-03 (Sites fix A) — the desktop ``GET /pockets`` gallery
route now hides pockets that have been published as Paw Sites (they show
under ``/sites`` instead). It calls ``sites_service.site_pocket_ids`` (a
cross-entity read via the sites SERVICE, never the Site Beanie model) and
passes the result as ``exclude_pocket_ids`` to ``list_pockets``. Only this
route excludes sites — every other ``list_pockets`` caller is unchanged.

Updated: 2026-05-25 (PR #1222 R1 fixes) — the loopback bypass on the
spec-merge + pocket-read endpoints now requires a process-local
internal token in addition to the previous loopback + magic header +
tenancy header set. The token (~/.pocketpaw/internal-token, 0600,
generated once at dashboard boot) is compared via
``secrets.compare_digest``. Without it, a same-machine non-PocketPaw
process could forge any tenancy by sending arbitrary
``X-PocketPaw-Workspace-Id`` / ``X-PocketPaw-User-Id`` headers. The
prior ``body: dict`` shape on the merge route is replaced with a
typed ``MergeSpecRequest`` Pydantic model that enforces the
exactly-one rule at parse time. The dead ``"localhost"`` string
branch in ``_is_localhost`` is removed and a comment pins the
no-X-Forwarded-For contract — putting a reverse proxy in front of
the dashboard requires disabling this bypass.

Updated: 2026-05-24 — added the MVP ``POST /{pocket_id}/spec/merge``
endpoint. The endpoint accepts a body of exactly one
``{"replace": <full spec>}`` or ``{"merge": <partial spec>}`` and
delegates to ``pockets_service.merge_spec``. Supports a localhost-only
internal bypass so the new bundled ``pocketpaw-pocket-specialist``
skill (which runs in a Claude Code subprocess and ``curl``s the local
API directly) can authenticate without round-tripping a real JWT for
the MVP. The bypass requires:

  - request source IP == 127.0.0.1 / ::1, AND
  - ``X-PocketPaw-Internal: true`` header, AND
  - ``X-PocketPaw-Internal-Token`` matching the process-local secret, AND
  - ``X-PocketPaw-Workspace-Id`` + ``X-PocketPaw-User-Id`` headers
    carrying the calling user's identity.

The captain greenlights tightening this in PR-2 once we know whether
the cookie-based auth path works end-to-end (the bypass is the safety
net, not the primary path). For now the bypass is documented in the
endpoint docstring as the security caveat.

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

import secrets as _secrets
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud._core.internal_token import (
    INTERNAL_TOKEN_HEADER,
    get_internal_token,
)
from pocketpaw_ee.cloud.auth import current_optional_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    BulkDispatchResponse,
    CreatePocketRequest,
    DispatchBulkRequest,
    GrantPocketConnectorRequest,
    HomePocketResponse,
    MergeSpecRequest,
    PocketBackendConfigRequest,
    PocketBackendConfigResponse,
    PocketConnectorPermissionsOut,
    ReorderWidgetsRequest,
    RevokePocketConnectorRequest,
    RunActionRequest,
    RunActionResponse,
    RunSourcesRequest,
    RunToolRequest,
    RunToolResponse,
    SetApprovalRouteRequest,
    SetPocketConnectorPermissionsRequest,
    SetToolPolicyRequest,
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
    # Desktop /pockets gallery. Hide pockets already published as Paw Sites —
    # they show under /sites instead. The site→pocket lookup goes through the
    # sites SERVICE (cross-entity read via a service function, not the Site
    # Beanie model) to respect entity isolation. Only this gallery route
    # excludes sites; other list_pockets callers (mission control, planners,
    # kb, surface) keep listing every pocket.
    from pocketpaw_ee.sites import service as sites_service

    exclude = await sites_service.site_pocket_ids(workspace_id)
    return await pockets_service.list_pockets(
        workspace_id, user_id, project_id=project_id, exclude_pocket_ids=exclude
    )


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
    request: Request,
    user: Any = Depends(current_optional_user),
    x_pocketpaw_internal: str | None = Header(default=None, alias="X-PocketPaw-Internal"),
    x_pocketpaw_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_pocketpaw_workspace_id: str | None = Header(default=None, alias="X-PocketPaw-Workspace-Id"),
    x_pocketpaw_user_id: str | None = Header(default=None, alias="X-PocketPaw-User-Id"),
) -> dict:
    """Read a pocket. Same loopback-internal bypass as ``/spec/merge`` so
    the ``pocketpaw-pocket-specialist`` skill can read the spec before
    computing a partial. Otherwise standard cookie/bearer auth.

    The bypass requires loopback origin + ``X-PocketPaw-Internal: true``
    + a process-local token matching ``X-PocketPaw-Internal-Token`` +
    both ``X-PocketPaw-Workspace-Id`` and ``X-PocketPaw-User-Id``
    headers (PR #1222 R1 tightening — see ``_loopback_bypass_active``).
    """
    if _loopback_bypass_active(
        request,
        internal_header=x_pocketpaw_internal,
        internal_token=x_pocketpaw_internal_token,
        workspace_header=x_pocketpaw_workspace_id,
        user_header=x_pocketpaw_user_id,
    ):
        user_id = x_pocketpaw_user_id  # type: ignore[assignment]
    elif user is not None:
        user_id = str(user.id)
    else:
        raise CloudError(401, "auth.required", "Authentication required.")
    return await pockets_service.get(pocket_id, user_id)


@router.patch("/{pocket_id}", dependencies=[Depends(require_pocket_edit)])
async def update_pocket(
    pocket_id: str,
    body: UpdatePocketRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update(pocket_id, user_id, body)


# ---------------------------------------------------------------------------
# Spec merge endpoint — MVP for the new pocketpaw-pocket-specialist skill.
# ---------------------------------------------------------------------------


def _is_localhost(request: Request) -> bool:
    """Loopback-only check used by the internal-bypass path on the spec
    merge endpoint. ``starlette`` exposes the immediate peer on
    ``request.client.host`` — that is the unix socket / TCP peer, not the
    ``X-Forwarded-For`` header, so a reverse-proxy front-end cannot
    spoof it. Accept the IPv4 and IPv6 loopback addresses.

    Security contract: we deliberately do NOT honor ``X-Forwarded-For``
    here. If a future deployment puts a reverse proxy in front of the
    dashboard, ``request.client.host`` will resolve to the proxy's
    address (also loopback if the proxy runs on the same box), making
    every external client appear loopback. This bypass MUST be
    disabled in that deployment, or this contract revisited. The
    dead ``"localhost"`` string branch from the original MVP was
    removed in PR #1222 R1 — ``request.client.host`` resolves to an
    IP literal, never the hostname, so the branch was unreachable."""
    client = request.client
    if not client:
        return False
    return client.host in ("127.0.0.1", "::1")


def _bypass_token_matches(supplied: str | None) -> bool:
    """Constant-time compare of the supplied bypass token against the
    process-local secret.

    Returns False (and never raises) when:

      * The dashboard hasn't called ``ensure_internal_token`` yet
        (``get_internal_token`` returns None). Treat as "bypass not
        configured" — the caller must fall through to cookie/bearer
        auth instead of accepting a no-token compare.
      * The caller supplied no token, an empty token, or a non-string.

    Uses ``secrets.compare_digest`` to defeat timing attacks across
    repeated probes from the same loopback client.
    """
    expected = get_internal_token()
    if not expected or not isinstance(supplied, str) or not supplied:
        return False
    return _secrets.compare_digest(expected, supplied)


def _loopback_bypass_active(
    request: Request,
    *,
    internal_header: str | None,
    internal_token: str | None,
    workspace_header: str | None,
    user_header: str | None,
) -> bool:
    """Single source of truth for "is the loopback internal bypass active
    for this request?"

    The bypass requires ALL of:

      1. The request originated on the loopback interface
         (``_is_localhost``).
      2. ``X-PocketPaw-Internal: true`` was sent.
      3. ``X-PocketPaw-Internal-Token`` matches the process-local
         secret (constant-time compare).
      4. Both ``X-PocketPaw-Workspace-Id`` and
         ``X-PocketPaw-User-Id`` are present (non-empty).

    Any missing factor → bypass denied; the caller falls through to
    the standard cookie/bearer auth dependency.
    """
    if not _is_localhost(request):
        return False
    if internal_header != "true":
        return False
    if not _bypass_token_matches(internal_token):
        return False
    if not workspace_header or not user_header:
        return False
    return True


@router.post("/{pocket_id}/spec/merge")
async def merge_pocket_spec(
    pocket_id: str,
    body: MergeSpecRequest,
    request: Request,
    user: Any = Depends(current_optional_user),
    x_pocketpaw_internal: str | None = Header(default=None, alias="X-PocketPaw-Internal"),
    x_pocketpaw_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_pocketpaw_workspace_id: str | None = Header(default=None, alias="X-PocketPaw-Workspace-Id"),
    x_pocketpaw_user_id: str | None = Header(default=None, alias="X-PocketPaw-User-Id"),
) -> dict:
    """One-shot rippleSpec write: full ``replace`` or partial ``merge``.

    MVP entry point for the new ``pocketpaw-pocket-specialist`` skill —
    replaces the 17-tool LangChain edit surface with a single endpoint
    the agent invokes via ``curl``.

    **Body shape.** A ``MergeSpecRequest`` carrying EXACTLY ONE of:

    .. code-block:: json

        {"replace": { "version": "1.0", "state": {...}, "ui": {...} }}
        {"merge":   { "ui": { "id": "n_xxx", ... } }}

    A body that carries both or neither returns ``422`` (Pydantic
    rejects at parse time).

    **Auth.** Standard cookie / bearer JWT (mirrors every other pocket
    route) — OR a loopback-only internal bypass that requires ALL of:

      * The request originates on the loopback interface
        (127.0.0.1 / ::1, never via a reverse proxy — see
        ``_is_localhost`` for the contract pin).
      * ``X-PocketPaw-Internal: true``.
      * ``X-PocketPaw-Internal-Token`` matches the host's
        ``~/.pocketpaw/internal-token`` (compared via
        ``secrets.compare_digest``). The token is generated at
        dashboard boot and exported to ``POCKETPAW_INTERNAL_TOKEN``
        so the pocket-specialist subprocess inherits it.
      * Both ``X-PocketPaw-Workspace-Id`` and
        ``X-PocketPaw-User-Id`` are present.

    Any missing factor falls through to cookie/bearer auth.
    Service-level edit-access checks inside ``merge_spec`` still run
    on the resolved user_id, so a mistyped header cannot escalate.
    Captain greenlights tightening this in a follow-up PR (short-lived
    JWT) once we confirm the shape on real traffic — the token gate is
    interim, dev-grade.

    **Validation.** Runs the strict catalog + action-wiring gates
    (mirroring the agent-generation path in ``create_from_ripple_spec``).
    A blocking violation returns ``{ok: false, warnings: [...]}``
    without persisting; the agent can fix and retry. Non-blocking
    expression-grammar warnings persist with ``ok: true`` and the
    warnings folded into the list.
    """
    # Resolve identity — try the loopback internal bypass first, then
    # fall through to the standard session-user identity. The bypass
    # needs loopback + magic header + token + tenancy headers, ALL
    # together; any missing piece falls through to the auth dep above.
    if _loopback_bypass_active(
        request,
        internal_header=x_pocketpaw_internal,
        internal_token=x_pocketpaw_internal_token,
        workspace_header=x_pocketpaw_workspace_id,
        user_header=x_pocketpaw_user_id,
    ):
        # Mypy: the header values are non-empty by the time the bypass
        # check passes (it short-circuits on any None / empty header).
        workspace_id = x_pocketpaw_workspace_id  # type: ignore[assignment]
        user_id = x_pocketpaw_user_id  # type: ignore[assignment]
    elif user is not None:
        user_id = str(user.id)
        if not user.active_workspace:
            raise CloudError(
                400,
                "workspace.not_set",
                "No active workspace. Create or join a workspace first.",
            )
        workspace_id = user.active_workspace
    else:
        raise CloudError(401, "auth.required", "Authentication required.")

    return await pockets_service.merge_spec(workspace_id, user_id, pocket_id, body)


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
    """Bind this pocket to one external backend.

    ``backend_type="http"`` (the default) binds a base URL + auth credential;
    the token is encrypted server-side and never echoed back, and a bad base
    URL (non-https, internal host) yields a 400. ``backend_type="connector"``
    binds an existing workspace connector by ``connector_name`` (no base_url
    needed); an unknown / not-enabled connector yields a 400.
    """
    result = await pockets_service.set_pocket_backend(
        workspace_id,
        user_id,
        pocket_id,
        body.base_url,
        body.auth_type,
        body.auth_token,
        body.auth_header,
        backend_type=body.backend_type,
        connector_name=body.connector_name,
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

    from pocketpaw_ee.cloud.pockets import source_executor

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        # No backend bound. The frontend runs declared sources on EVERY
        # pocket_open, so a blank/starter pocket (no backend, nothing to
        # fetch) hits this path on every open. Don't 400 it — that surfaced
        # as a noisy "pocket_open sources run failed: HttpError: Bad Request"
        # in the browser. Decide by whether anything would actually run:
        #   * nothing selected  -> clean no-op (200, empty result)
        #   * source(s) selected -> a real misconfiguration: a runnable source
        #     was authored but no backend is bound. Report it as a per-source
        #     error (200), matching every other source-run call site
        #     (agent pocket_router, temporal_dispatcher, bulk_dispatch) which
        #     treat "no backend" as soft/non-fatal — never a hard 400.
        selected = source_executor.selected_source_keys(
            ripple_spec, trigger=body.trigger, only_source=body.source
        )
        return {
            "ran": [],
            "errors": [
                {
                    "source": key,
                    "error": "This pocket has no backend configured",
                    "code": "pocket_backend.not_configured",
                }
                for key in selected
            ],
        }
    # M2b.1 — the executor-creds tuple gained `allowed_writes` (M2a) and
    # `approval_route` (M2b.1); read-only source runs need neither. The
    # connector-backend feature appended `backend_type` / `connector_name`,
    # which the run DOES need to pick the http vs connector transport.
    (
        base_url,
        auth_type,
        auth_header,
        token,
        _allowed_writes,
        _approval_route,
        backend_type,
        connector_name,
    ) = creds

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
        workspace_id=workspace_id,
        backend_type=backend_type,
        connector_name=connector_name,
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
        workspace_id=workspace_id,
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
    "/{pocket_id}/backend/tool-policy",
    dependencies=[Depends(require_pocket_owner)],
)
async def set_pocket_tool_policy(
    pocket_id: str,
    body: SetToolPolicyRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketBackendConfigResponse:
    """Set this pocket's tool allowlist (feat/invoke-tool-v1). Owner-only.

    Replaces the whole ``allowed_tools`` list — an empty list revokes every
    tool (fail-closed). The policy lives on the backend-credential row,
    OUTSIDE the spec, so the agent that authors the spec cannot grant itself
    a tool (the same blast-radius invariant ``allowed_writes`` enforces). A
    grant's ``tool`` is a built-in tool name or a connector action
    ``connector:<name>:<action>``. Returns ``400`` when the pocket has no
    backend configured — a tool policy with no backend to apply to is
    meaningless.
    """
    result = await pockets_service.set_pocket_tool_policy(
        workspace_id,
        user_id,
        pocket_id,
        [grant.model_dump() for grant in body.allowed_tools],
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


async def _dispatch_job(
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    action: str,
    raw_action: dict,
    request_params: dict,
) -> RunActionResponse:
    """Enqueue a ``kind:"job"`` action as a workspace job (pp#1459).

    Merges the action's DECLARED params (the trusted, author-time spec data)
    with no client params — a job's params come from the binding, not the
    click — looks the job up in the registry, persists a queued status doc,
    enqueues the ARQ job, and returns ``{ok:true, code:"job_enqueued",
    job_id}``. The client polls ``GET /workspaces/{ws}/jobs/{job_id}``.

    ``requires_instinct:true`` is rejected with ``job.instinct_not_yet_supported``
    (DEFAULT #2 — the Instinct path is untested for jobs, deferred to v1.1; do
    NOT silently fire an ungated job that declared it wanted a gate).

    Client-supplied ``params`` are REJECTED with ``job.params_not_accepted``
    (400) — a job is parameterized only by its DECLARED (author-time) params,
    so accepting per-click input would let a clicker widen the job's scope.
    Enforcing the rejection makes the scope guarantee explicit rather than a
    silent drop. The normal (non-job) action path is unaffected: it never
    reaches this function — the ``kind:"job"`` branch returns first.
    """
    from pocketpaw_ee.cloud.jobs import service as jobs_service
    from pocketpaw_ee.cloud.jobs.registry import JobError

    if raw_action.get("requires_instinct"):
        raise CloudError(
            400,
            "job.instinct_not_yet_supported",
            "requires_instinct is not yet supported for jobs (deferred to v1.1)",
        )

    # A clicker cannot widen a job's scope: a job's params come from the
    # binding (declared, author-time), never from per-click input. Reject any
    # non-empty client params rather than silently dropping them.
    if request_params:
        raise CloudError(
            400,
            "job.params_not_accepted",
            "a job action does not accept client params — its params are fixed by the binding",
        )

    job_name = raw_action.get("job")
    if not isinstance(job_name, str) or not job_name:
        raise CloudError(
            400,
            "job.unknown",
            f"action '{action}' is kind=job but declares no job name",
        )

    # The job's params are the action's DECLARED params (author-time, trusted).
    # Client-sent params are ignored for jobs — a job is parameterized by the
    # binding, not by per-click input, so a client cannot widen a job's scope.
    declared_params = raw_action.get("params")
    params = declared_params if isinstance(declared_params, dict) else {}

    try:
        result = await jobs_service.dispatch_job(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            job_name=job_name,
            params=params,
            triggered_by=user_id,
        )
    except JobError as exc:
        # Registry miss / credential-bearing param → the contracted 400s.
        raise CloudError(exc.status, exc.code, exc.message) from exc

    return RunActionResponse(
        ok=True,
        action=action,
        code=result["code"],
        job_id=result["job_id"],
    )


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

    # Workspace jobs (pp#1459) — a ``kind:"job"`` action does NOT fire an HTTP
    # write. It enqueues a named server-side job that runs in the ARQ worker
    # under the synthetic workspace identity. Branch BEFORE the path resolution
    # + ``action_executor.run_action`` path below: a job action carries no
    # ``path`` (it's not an HTTP call), so it would otherwise be rejected by
    # the path check. A missing ``kind`` falls through unchanged (back-compat).
    if raw_action.get("kind") == "job":
        return await _dispatch_job(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            action=body.action,
            raw_action=raw_action,
            request_params=body.params,
        )

    # Resolve the request path. The read-time normalizer rewrites an inline
    # write `api` handler into a PATHLESS `call_binding` handler (it has no
    # request context to carry a path), so the normal client fires with
    # `{action, params}` and no `path`. The path is author-time data already
    # persisted on the binding (the spec must carry it to know the HTTP
    # method), so fall back to it. A client that DID send a resolved path —
    # a row-scoped binding whose stored path holds an unresolved `{item.id}`
    # template, resolved client-side — keeps precedence; the binding path is
    # only the fallback. The executor still matches the final (method, path)
    # against the owner's allowlist, so the fallback cannot widen blast radius.
    resolved_path = body.path or raw_action.get("path")
    if not isinstance(resolved_path, str) or not resolved_path:
        return RunActionResponse(
            ok=False,
            action=body.action,
            error=f"action '{body.action}' has no path — send one or declare it on the binding",
            code="bad_binding",
        )

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        raise CloudError(
            400,
            "pocket_backend.not_configured",
            "This pocket has no backend configured — set one via PUT /pockets/{id}/backend",
        )
    # Write-action run: the trailing `backend_type` / `connector_name` (added
    # by the connector-backend feature) don't apply to the http write executor,
    # so they're ignored here.
    base_url, auth_type, auth_header, token, allowed_writes, approval_route, *_ = creds

    from pocketpaw_ee.cloud.pockets import action_executor

    # W2a — thread the pocket's RFC 03 v2 template so the template-level
    # CEL Instinct rules (gate 1.5) actually FIRE on the single-action run.
    # Before W2a this path never passed a template, so a pocket's CEL rules
    # (block / require_approval / notify) silently never evaluated on a
    # direct `/actions/run` — the bulk + temporal paths gated, this one did
    # not. `resolve_pocket_template` returns `None` for a pocket with no
    # `template_slug`, an unknown slug, or an unloadable template (never an
    # error — same contract bulk/temporal rely on), so a pocket with no
    # template is byte-identical to the pre-W2a behavior on this line.
    #
    # We thread the template ONLY when it actually DECLARES the action being
    # run. `resolve_instinct` raises (→ 404) on an action absent from the
    # template's `actions[]`; a `rippleSpec.actions` write binding need not
    # correspond to a template action, so threading unconditionally would
    # 404 every legitimate non-template write. When the template does not
    # cover this action we pass `template=None` and the binding-level
    # deny-by-default gate (gate 7) governs — which now parks an agent-
    # authored write rather than firing it.
    template = await pockets_service.resolve_pocket_template(workspace_id, pocket_id)
    if template is not None and not any(a.name == body.action for a in template.actions):
        template = None

    # Layered/learning gate (T6) — resolve the workspace's triager activation
    # level (per-workspace field → global config default → "ASK") and the
    # global dry-run flag, and thread them into the executor's gate 1.5. When
    # the workspace has not opted in, the level is "ASK" and the gate behaves
    # exactly as before — every escalate parks for a human. The level is only
    # read when a template actually governs this action (otherwise the gate
    # never runs), so a non-template write is byte-identical to before.
    approval_level = "ASK"
    dry_run_mode = False
    if template is not None:
        approval_level = await pockets_service.resolve_workspace_approval_level(workspace_id)
        from pocketpaw.config import get_settings

        dry_run_mode = bool(get_settings().instinct_dry_run_mode)

    # no-event: the write result is response-body delivery, not persisted.
    result = await action_executor.run_action(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        action=body.action,
        raw_action=raw_action,
        path=resolved_path,
        params=body.params,
        base_url=base_url,
        auth_type=auth_type,
        auth_header=auth_header,
        token=token,
        allowed_writes=allowed_writes,
        idempotency_key=body.idempotency_key,
        template=template,
        approval_level=approval_level,
        dry_run_mode=dry_run_mode,
    )

    # W2a — a template-level CEL rule BLOCKED this write (gate 1.5). It
    # only fires when a template that declares this action is threaded
    # (see the threading above). The executor returned `ok:false,
    # code:"instinct_blocked"` and carries a `reason` the wire model does
    # not declare — surface a clean blocked response without the internal
    # `reason`/`_park` keys (`RunActionResponse` is `extra="forbid"`).
    if result.get("code") == "instinct_blocked":
        return RunActionResponse(
            ok=False,
            action=body.action,
            code="instinct_blocked",
            error="action blocked by an Instinct rule",
        )

    # Layered/learning gate (T7/T-31) — a DRY_RUN write. The executor
    # resolved + audited the write but made NO backend call and persisted no
    # approval row. It carries the resolved write under `_park`, which MUST
    # NOT reach the client wire (the same constraint as `instinct_pending`).
    # Surface a clean dry-run acknowledgement; the resolved write is visible
    # only in the audit log, never the response.
    if result.get("code") == "instinct_dry_run":
        return RunActionResponse(
            ok=True,
            action=body.action,
            code="instinct_dry_run",
        )

    # M2b.1 / W2a — a write was PARKED, not fired. There are TWO park
    # shapes the executor can return, both `code:"instinct_pending"`:
    #
    #   * gate 1.5 (template CEL → ESCALATE_APPROVAL) already PERSISTED an
    #     `InstinctApproval` row and carries its id on `approval_id`. Do
    #     NOT re-propose — `propose_pocket_write` would create a SECOND,
    #     unrelated park in a different store. Surface the existing id.
    #   * gate 7 (binding-level deny-by-default) parked WITHOUT persisting;
    #     it carries the resolved write under `_park` and no `approval_id`.
    #     Route it into an Instinct Action via `propose_pocket_write`.
    #
    # The executor validated the write before either park (a write the
    # allowlist rejects already came back `ok:false`).
    if result.get("code") == "instinct_pending":
        approval_id = result.get("approval_id")
        if approval_id is not None:
            # Template-CEL escalation — the approval row already exists.
            return RunActionResponse(
                ok=True,
                action=body.action,
                code="instinct_pending",
                proposed_action_id=approval_id,
            )

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
            # gap-3 — billable value/unit for the direct (non-gated) write.
            outcome_value=result.get("outcome_value"),
            outcome_unit=result.get("outcome_unit"),
        )

    # Strip executor-internal keys (`_park`, `outcome`) the wire model
    # does not carry before building the response. SHOULD-FIX 2
    # (PR #1183) — the strip is defensive: `_park` carries the resolved
    # write path/params and must NEVER reach the wire. The explicit
    # assertion below catches a strip that drifts out of sync with the
    # executor's result keys; `RunActionResponse` is also `extra="forbid"`
    # so a missed key fails construction rather than leaking.
    # Layered/learning gate (T10) — map the executor-internal optimistic
    # handle id onto the PUBLIC wire field before the strip below removes the
    # underscored key. Only set on an OPTIMISTIC-lane write that declared a
    # compensate; absent otherwise.
    optimistic_id = result.get("_optimistic_compensation_id")

    wire = {
        k: v
        for k, v in result.items()
        # gap-3 — strip the metering keys too: `outcome_value`/`outcome_unit`
        # join `outcome` as executor-internal fields the wire model
        # (`extra="forbid"`) does not carry. They were already consumed by
        # the `emit_pocket_outcome` call above. `_optimistic_compensation_id`
        # is the executor-internal key for the optimistic handle — re-exposed
        # below as the public `optimistic_compensation_id` field.
        if k
        not in (
            "_park",
            "outcome",
            "outcome_value",
            "outcome_unit",
            "_optimistic_compensation_id",
        )
    }
    assert "_park" not in wire, "executor `_park` blob must be stripped before the wire response"
    if optimistic_id:
        wire["optimistic_compensation_id"] = optimistic_id
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
# Pocket tool invocation (#1206 — invoke_tool; UNLOCKED in feat/invoke-tool-v1)
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

    The per-pocket tool allowlist (feat/invoke-tool-v1) lives on the
    backend-credential row, OUTSIDE the spec — read here via
    ``get_pocket_backend_for_executor`` and passed to the executor BY
    PARAMETER, never sourced from ``rippleSpec``. A pocket with no backend
    (or no grants) reads as an EMPTY allowlist → every tool returns
    ``ok:false, code:"not_allowed"`` (fail-closed). A ``connector:<name>:
    <action>`` grant for a READ action fires through
    ``connectors.service.execute``; a WRITE action returns ``code="blocked"``
    (approval lands in v2 via Instinct).

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

    # Read the allowlist off the per-pocket backend credential row. The 9th
    # tuple element is `allowed_tools` (a list of `{tool}` wire dicts). A
    # pocket with no backend configured → `None` → empty allowlist, so the
    # fail-closed `not_allowed` path holds with no special-casing. The
    # executor stays Beanie-free — it only ever sees the flat list of names.
    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    allowed_tools: list[str] = []
    if creds is not None:
        allowed_tools = [grant["tool"] for grant in creds[8]]

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
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await pockets_service.add_team_member(
        pocket_id,
        user_id,
        body["member_id"],
        actor_workspace_id=workspace_id,
    )


@router.delete("/{pocket_id}/team/{member_id}", status_code=204)
async def remove_team_member(
    pocket_id: str,
    member_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    await pockets_service.remove_team_member(
        pocket_id,
        user_id,
        member_id,
        actor_workspace_id=workspace_id,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Members — frontend-facing aliases for team management
# Uses workspace admin/owner check for private pockets.
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/members")
async def add_pocket_member(
    pocket_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await pockets_service.add_team_member(
        pocket_id,
        user_id,
        body.get("userId", body.get("member_id")),
        actor_workspace_id=workspace_id,
    )


@router.delete("/{pocket_id}/members/{member_id}", status_code=204)
async def remove_pocket_member(
    pocket_id: str,
    member_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    await pockets_service.remove_team_member(
        pocket_id,
        user_id,
        member_id,
        actor_workspace_id=workspace_id,
    )
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


# ---------------------------------------------------------------------------
# Template Reconcile (P2.4) — thin REST adapter over ``pockets.reconcile``.
#
# Re-apply a pocket's source-template-owned regions (ui/actions/sources/shape)
# while preserving instance-owned regions (state, name, owner, sharing).
# ``preview`` is a dry-run diff; ``apply`` writes through the spec path.
# Both delegate to the service — no reconcile logic lives in the router.
#
# Auth mirrors ``get_pocket`` / ``/spec/merge``: standard cookie/bearer JWT,
# OR the loopback internal-token bypass (so the ``pocketpaw pocket reconcile``
# CLI — an operator action run on the same box — can authenticate with the
# process-local token + workspace/user headers). The service re-checks
# read/edit access on the resolved identity, so a forged header can't escalate.
# ---------------------------------------------------------------------------


def _resolve_reconcile_identity(
    request: Request,
    *,
    user: Any,
    internal_header: str | None,
    internal_token: str | None,
    workspace_header: str | None,
    user_header: str | None,
) -> tuple[str, str]:
    """Resolve ``(workspace_id, user_id)`` for a reconcile request.

    Tries the loopback internal bypass first (operator CLI / local
    subprocess), then falls through to the authenticated session user. Raises
    ``CloudError(401)`` when neither path yields an identity. Shared by the
    preview + apply routes so the two stay in lockstep.
    """
    if _loopback_bypass_active(
        request,
        internal_header=internal_header,
        internal_token=internal_token,
        workspace_header=workspace_header,
        user_header=user_header,
    ):
        return workspace_header, user_header  # type: ignore[return-value]
    if user is not None:
        if not getattr(user, "active_workspace", None):
            raise CloudError(
                400,
                "workspace.not_set",
                "No active workspace. Create or join a workspace first.",
            )
        return user.active_workspace, str(user.id)
    raise CloudError(401, "auth.required", "Authentication required.")


@router.post("/{pocket_id}/reconcile/preview")
async def preview_pocket_reconcile(
    pocket_id: str,
    request: Request,
    user: Any = Depends(current_optional_user),
    x_pocketpaw_internal: str | None = Header(default=None, alias="X-PocketPaw-Internal"),
    x_pocketpaw_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_pocketpaw_workspace_id: str | None = Header(default=None, alias="X-PocketPaw-Workspace-Id"),
    x_pocketpaw_user_id: str | None = Header(default=None, alias="X-PocketPaw-User-Id"),
) -> dict:
    """Dry-run a template reconcile — report what WOULD change, write nothing.

    Returns the reconcile diff (``changed_regions`` / ``unchanged_regions`` /
    ``preserved_regions``). A pocket with no ``template_slug`` or an
    unresolvable template returns 422 via the service's ``ValidationError``;
    a caller without read access gets a 403.
    """
    from pocketpaw_ee.cloud.pockets import reconcile as reconcile_service

    workspace_id, user_id = _resolve_reconcile_identity(
        request,
        user=user,
        internal_header=x_pocketpaw_internal,
        internal_token=x_pocketpaw_internal_token,
        workspace_header=x_pocketpaw_workspace_id,
        user_header=x_pocketpaw_user_id,
    )
    return await reconcile_service.preview_reconcile(pocket_id, workspace_id, user_id)


@router.post("/{pocket_id}/reconcile/apply")
async def apply_pocket_reconcile(
    pocket_id: str,
    request: Request,
    user: Any = Depends(current_optional_user),
    x_pocketpaw_internal: str | None = Header(default=None, alias="X-PocketPaw-Internal"),
    x_pocketpaw_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_pocketpaw_workspace_id: str | None = Header(default=None, alias="X-PocketPaw-Workspace-Id"),
    x_pocketpaw_user_id: str | None = Header(default=None, alias="X-PocketPaw-User-Id"),
) -> dict:
    """Apply a template reconcile — re-write template-owned regions, preserve
    instance state, persist through the existing spec write path.

    Edit access is enforced INSIDE the service (``apply_reconcile`` raises
    ``Forbidden`` for a non-editor — including on the idempotent no-write
    path). Returns ``{ok, skipped, diff, pocket?}``; when the pocket already
    matches its template the write is skipped (``skipped: true``) and no
    ``PocketUpdated`` event is emitted.
    """
    from pocketpaw_ee.cloud.pockets import reconcile as reconcile_service

    workspace_id, user_id = _resolve_reconcile_identity(
        request,
        user=user,
        internal_header=x_pocketpaw_internal,
        internal_token=x_pocketpaw_internal_token,
        workspace_header=x_pocketpaw_workspace_id,
        user_header=x_pocketpaw_user_id,
    )
    return await reconcile_service.apply_reconcile(pocket_id, workspace_id, user_id)


# ---------------------------------------------------------------------------
# Per-pocket connector permissions
# ---------------------------------------------------------------------------


@router.get(
    "/{pocket_id}/connector-permissions",
    response_model=PocketConnectorPermissionsOut,
    dependencies=[Depends(require_pocket_edit)],
)
async def get_pocket_connector_permissions(
    pocket_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketConnectorPermissionsOut:
    """Read this pocket's connector allowlist.

    Returns ``allowed_connectors: null`` when the pocket inherits all
    workspace connectors (default / no restrictions). Returns an empty
    list when the pocket is restricted but has nothing allowed.
    """
    allowed = await pockets_service.get_pocket_connector_permissions(pocket_id)
    return PocketConnectorPermissionsOut(allowed_connectors=allowed)


@router.put(
    "/{pocket_id}/connector-permissions",
    dependencies=[Depends(require_pocket_edit)],
)
async def set_pocket_connector_permissions(
    pocket_id: str,
    body: SetPocketConnectorPermissionsRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> PocketConnectorPermissionsOut:
    """Set this pocket's connector allowlist.

    Pass ``allowed_connectors: null`` to inherit all workspace connectors
    (clear restrictions). Pass ``allowed_connectors: []`` to restrict the
    pocket but allow nothing — the pocket sees no connectors.
    """
    await pockets_service.set_pocket_connector_permissions(pocket_id, body.allowed_connectors)
    return PocketConnectorPermissionsOut(allowed_connectors=body.allowed_connectors)


@router.post(
    "/{pocket_id}/connector-permissions/grant",
    dependencies=[Depends(require_pocket_edit)],
    status_code=204,
)
async def grant_pocket_connector(
    pocket_id: str,
    body: GrantPocketConnectorRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    """Grant one connector to this pocket (additive).

    If the pocket currently inherits all connectors (``allowed_connectors``
    is ``None``), granting creates an explicit allowlist starting with
    this connector — effectively restricting the pocket to just this one.
    Idempotent: granting an already-allowed connector is a no-op.
    """
    await pockets_service.grant_pocket_connector(pocket_id, body.connector_name)
    return Response(status_code=204)


@router.post(
    "/{pocket_id}/connector-permissions/revoke",
    dependencies=[Depends(require_pocket_edit)],
    status_code=204,
)
async def revoke_pocket_connector(
    pocket_id: str,
    body: RevokePocketConnectorRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    """Revoke one connector from this pocket.

    If the pocket inherits all connectors (``allowed_connectors`` is
    ``None``), revoke is a no-op — there are no explicit restrictions to
    remove from. When the last connector is revoked, the pocket's list
    becomes empty (``[]``), meaning the pocket sees no connectors.
    Idempotent: revoking a connector that is not in the allowlist is a
    no-op.
    """
    await pockets_service.revoke_pocket_connector(pocket_id, body.connector_name)
    return Response(status_code=204)
