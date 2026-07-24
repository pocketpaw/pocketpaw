# ee/pocketpaw_ee/agent/mcp_servers/ship.py — the agent-facing /ship surface.
#
# In-process MCP server (``pocketpaw_ship``) letting a chat agent drive managed
# deploys: list boxes and apps, provision a box, register and deploy an app,
# route a domain, attach a database, read logs and metrics — and PROPOSE the
# destructive verbs it may not run.
#
# THE SPLIT THAT MATTERS:
#   * Reversible / read verbs execute directly through ``cloud.ship.service``.
#   * ``destroy`` (box or app) NEVER executes here. It files an Instinct proposal
#     via the service's request-destroy path and returns ``status="proposed"``.
#     Only ``cloud.ship.executor.execute_approved_ship_action`` — reached solely
#     through a human approval — may touch the engine's destroy verb.
#
# Every tool calls the SERVICE layer, never the driver or the store: the service
# owns tenancy filtering, input validation, event emission, and the proposal
# filing. The handlers here resolve identity, shape arguments, and relay.
#
# Mirrors ``external_actions.py`` (the closest sibling): same ``_identity()``
# resolution, same ``_error_response`` / ``_success_response`` envelopes, same
# "no phantom successes" rule — ok is returned only after the service call
# actually succeeded.
#
# Created 2026-07-22 (feat/ship-4-agent-surface, SHIP-4): new module.

from __future__ import annotations

import logging
from typing import Any

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_ship"

# Stable tool ids — the surface profile allowlist references these.
SHIP_TOOL_IDS = tuple(
    f"mcp__{SERVER_NAME}__{name}"
    for name in (
        "ship_list_boxes",
        "ship_provision_box",
        "ship_list_apps",
        "ship_create_app",
        "ship_deploy_app",
        "ship_add_domain",
        "ship_create_db",
        "ship_set_scale",
        "ship_set_checks",
        "ship_set_resources",
        "ship_create_volume",
        "ship_restart",
        "ship_rebuild",
        "ship_logs",
        "ship_metrics",
        "ship_request_destroy",
    )
)


def _error_response(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    import json

    return {"content": [{"type": "text", "text": json.dumps(body)}]}


def _identity() -> tuple[str | None, str | None]:
    """Resolve (workspace_id, user_id) from the cloud chat session context."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001 — no cloud session context
        return None, None


def _audit(workspace_id: str, user_id: str, tool_name: str, *, ok: bool = True) -> None:
    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server=SERVER_NAME,
        tool_name=tool_name,
        status="ok" if ok else "error",
        ok=ok,
    )


async def _with_identity(tool_name: str):
    """Resolve identity or return the error envelope. Returns (ws, user, err)."""
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return (
            None,
            None,
            _error_response(
                f"{tool_name} requires workspace and user context "
                "(call it from a cloud chat session)."
            ),
        )
    _audit(workspace_id, user_id, tool_name)
    return workspace_id, user_id, None


def _view_error(exc: Exception, what: str) -> dict[str, Any]:
    """Relay a service failure plainly. CloudErrors carry a safe message."""
    logger.warning("ship mcp: %s failed", what, exc_info=True)
    return _error_response(f"could not {what}: {exc}")


# ---------------------------------------------------------------------------
# Handlers — reads and reversible writes execute; destroy only proposes.
# ---------------------------------------------------------------------------


async def _list_boxes_handler(_args: dict) -> dict:
    ws, user, err = await _with_identity("ship_list_boxes")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    try:
        views = await service.list_boxes(ws)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "list boxes")
    return _success_response({"boxes": [_box_wire(v) for v in views]})


async def _provision_box_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_provision_box")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import CreateBoxRequest

    try:
        body = CreateBoxRequest(
            provider=str(args.get("provider") or "hcloud"),
            server_type=args.get("server_type") or None,
            region=args.get("region") or None,
        )
        view = await service.create_box(ws, user, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "provision the box")
    return _success_response(
        {
            **_box_wire(view),
            "note": "provisioning runs in the background; poll ship_list_boxes for 'ready'.",
        }
    )


async def _list_apps_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_list_apps")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    try:
        views = await service.list_apps(ws, box_id=str(args.get("box_id") or "") or None)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "list apps")
    return _success_response({"apps": [_app_wire(v) for v in views]})


async def _create_app_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_create_app")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import CreateAppRequest

    try:
        # An agent points /ship at a git repo (source_kind="git" + repo_url) so
        # "write code -> ship it" needs no pre-built image. The token (private
        # repos) is accepted but NEVER echoed back — the app-wire view omits it.
        source_kind = str(args.get("source_kind") or "").strip() or None
        body = CreateAppRequest(
            name=str(args.get("name") or ""),
            box_id=str(args.get("box_id") or ""),
            image=str(args.get("image") or ""),
            git_ref=str(args.get("git_ref") or ""),
            **({"source_kind": source_kind} if source_kind else {}),
            **({"repo_url": str(args["repo_url"])} if args.get("repo_url") else {}),
            **({"repo_ref": str(args["repo_ref"])} if args.get("repo_ref") else {}),
            **({"token": str(args["token"])} if args.get("token") else {}),
        )
        view = await service.create_app(ws, user, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "create the app")
    return _success_response(_app_wire(view))


async def _deploy_app_handler(args: dict) -> dict:
    """Deploy an app. A PROD-flagged app is gated — it proposes instead."""
    ws, user, err = await _with_identity("ship_deploy_app")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_deploy_app requires an `app_id`.")
    try:
        result = await service.deploy_app_or_propose(ws, user, app_id)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "deploy the app")
    if isinstance(result, dict) and result.get("status") == "proposed":
        return _success_response(result)
    return _success_response(
        {
            "deploy_id": result.id,
            "app_id": result.app_id,
            "status": result.status,
            "note": "the deploy runs in the background; poll ship_list_apps.",
        }
    )


async def _add_domain_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_add_domain")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import AddDomainRequest

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_add_domain requires an `app_id`.")
    try:
        body = AddDomainRequest(domain=str(args.get("domain") or ""))
        view = await service.add_domain(ws, user, app_id, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "route the domain")
    return _success_response(
        {"domain": view.domain, "tls_enabled": view.tls_enabled, "url": view.url}
    )


async def _create_db_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_create_db")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import CreateDbRequest

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_create_db requires an `app_id`.")
    db_type = str(args.get("db_type") or "").strip() or None
    try:
        body = CreateDbRequest(**({"db_type": db_type} if db_type else {}))
        view = await service.create_db(ws, user, app_id, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "create the database")
    # The connection string is a secret and is NEVER returned — the app reads it
    # from the injected env var, whose NAME is all the agent needs.
    return _success_response({"service": view.service, "env_var": view.env_var})


async def _set_scale_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_set_scale")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import SetScaleRequest

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_set_scale requires an `app_id`.")
    raw = args.get("scale") or {}
    if not isinstance(raw, dict) or not raw:
        return _error_response('ship_set_scale requires a `scale` map, e.g. {"web": 2}.')
    try:
        scale = {str(k): int(v) for k, v in raw.items()}
        view = await service.set_scale(ws, user, app_id, SetScaleRequest(scale=scale))
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "scale the app")
    return _success_response({"app_id": view.id, "scale": dict(view.scale)})


async def _set_checks_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_set_checks")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import SetChecksRequest

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_set_checks requires an `app_id`.")
    try:
        body = SetChecksRequest(
            zero_downtime=bool(args.get("zero_downtime", True)),
            healthcheck_path=str(args.get("healthcheck_path") or ""),
        )
        view = await service.set_checks(ws, user, app_id, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "configure deploy checks")
    return _success_response(
        {
            "app_id": view.id,
            "zero_downtime": view.zero_downtime,
            "healthcheck_path": view.healthcheck_path,
        }
    )


async def _set_resources_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_set_resources")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import SetResourcesRequest

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_set_resources requires an `app_id`.")
    try:
        body = SetResourcesRequest(
            cpu=int(args.get("cpu") or 0),
            memory_mb=int(args.get("memory_mb") or 0),
        )
        view = await service.set_resources(ws, user, app_id, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "set resource limits")
    return _success_response(
        {"app_id": view.id, "cpu_limit": view.cpu_limit, "memory_limit_mb": view.memory_limit_mb}
    )


async def _create_volume_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_create_volume")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service
    from pocketpaw_ee.cloud.ship.dto import CreateVolumeRequest

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_create_volume requires an `app_id`.")
    name = str(args.get("name") or "").strip() or None
    mount_path = str(args.get("mount_path") or "")
    try:
        body = CreateVolumeRequest(**({"name": name} if name else {}), mount_path=mount_path)
        view = await service.create_volume(ws, user, app_id, body)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "create the volume")
    # host_path is a box-side directory, not a secret — safe to report back.
    return _success_response(
        {
            "app_id": view.id,
            "volumes": [{"name": n, "mount_path": m, "host_path": h} for (n, m, h) in view.volumes],
        }
    )


async def _restart_handler(args: dict) -> dict:
    return await _lifecycle_handler(args, action="restart")


async def _rebuild_handler(args: dict) -> dict:
    return await _lifecycle_handler(args, action="rebuild")


async def _lifecycle_handler(args: dict, *, action: str) -> dict:
    ws, user, err = await _with_identity(f"ship_{action}")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response(f"ship_{action} requires an `app_id`.")
    verb = service.restart_app if action == "restart" else service.rebuild_app
    try:
        view = await verb(ws, user, app_id)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, f"{action} the app")
    return _success_response({"app_id": view.app_id, "action": view.action})


async def _logs_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_logs")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    app_id = str(args.get("app_id") or "")
    if not app_id:
        return _error_response("ship_logs requires an `app_id`.")
    try:
        view = await service.get_logs(ws, app_id)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "read the logs")
    return _success_response({"lines": list(view.lines)})


async def _metrics_handler(args: dict) -> dict:
    ws, user, err = await _with_identity("ship_metrics")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    box_id = str(args.get("box_id") or "")
    if not box_id:
        return _error_response("ship_metrics requires a `box_id`.")
    try:
        view = await service.get_box_metrics(ws, box_id)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "read box metrics")
    return _success_response({"cpu": view.cpu, "mem": view.mem, "disk": view.disk})


async def _request_destroy_handler(args: dict) -> dict:
    """PROPOSE a teardown. This NEVER destroys anything.

    The service files an Instinct proposal; a human approves it in The Tray, and
    only then does the executor touch the box. The agent must relay "proposed",
    never "destroyed".
    """
    ws, user, err = await _with_identity("ship_request_destroy")
    if err:
        return err
    from pocketpaw_ee.cloud.ship import service

    box_id = str(args.get("box_id") or "")
    app_id = str(args.get("app_id") or "")
    if not box_id and not app_id:
        return _error_response("ship_request_destroy requires a `box_id` or an `app_id`.")
    try:
        if app_id:
            view = await service.request_app_destroy(ws, user, app_id)
        else:
            view = await service.request_box_destroy(ws, user, box_id)
    except Exception as exc:  # noqa: BLE001
        return _view_error(exc, "propose the teardown")
    return _success_response(
        {
            "status": "proposed",
            "proposal_id": view.proposal_id,
            "note": (
                "NOTHING has been destroyed. A human must approve this in The Tray "
                "before anything is torn down."
            ),
        }
    )


def _box_wire(view: Any) -> dict[str, Any]:
    return {
        "id": view.box_id,
        "provider": view.provider,
        "ip": view.ip,
        "status": view.status,
        "price_monthly": view.price_monthly,
    }


def _app_wire(view: Any) -> dict[str, Any]:
    return {
        "id": view.app_id,
        "name": view.name,
        "box_id": view.box_id,
        "status": view.status,
        "urls": list(view.urls),
    }


def build_ship_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for /ship, or ``None`` when the
    Claude Agent SDK isn't installed (chat must never break because of us)."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_ship MCP disabled")
        return None

    @tool(
        "ship_list_boxes",
        "List the workspace's managed-deploy boxes (provider, IP, status, price).",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def ship_list_boxes(args):  # type: ignore[no-untyped-def]
        return await _list_boxes_handler(args)

    @tool(
        "ship_provision_box",
        (
            "Provision a NEW server to deploy apps onto. Returns immediately; the "
            "box boots in the background — poll ship_list_boxes until its status "
            "is 'ready' before deploying to it."
        ),
        {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "Infra provider (default hcloud)."},
                "server_type": {"type": "string", "description": "Provider size (default cx22)."},
                "region": {"type": "string", "description": "Provider region (default fsn1)."},
            },
            "additionalProperties": False,
        },
    )
    async def ship_provision_box(args):  # type: ignore[no-untyped-def]
        return await _provision_box_handler(args)

    @tool(
        "ship_list_apps",
        "List the apps registered on the workspace's boxes.",
        {
            "type": "object",
            "properties": {"box_id": {"type": "string", "description": "Filter to one box."}},
            "additionalProperties": False,
        },
    )
    async def ship_list_apps(args):  # type: ignore[no-untyped-def]
        return await _list_apps_handler(args)

    @tool(
        "ship_create_app",
        (
            "Register an app on a box. `name` is a Dokku app name (lowercase "
            "alphanumeric + hyphens). Two source options: pass `source_kind='git'` "
            "with a `repo_url` (+ optional `repo_ref`, default 'main') to build and "
            "run from SOURCE CODE — the engine detects the stack (buildpack / "
            "nixpacks / Dockerfile), no pre-built image needed; or pass `image` for "
            "a pre-built container. For a private repo add `token` (write-only, "
            "never returned)."
        ),
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "box_id": {"type": "string", "minLength": 1},
                "source_kind": {
                    "type": "string",
                    "enum": ["image", "git"],
                    "description": "'git' builds from repo_url; 'image' runs a prebuilt image.",
                },
                "repo_url": {
                    "type": "string",
                    "description": "Git repo to build from (when source_kind='git').",
                },
                "repo_ref": {
                    "type": "string",
                    "description": "Branch/tag/commit to deploy (default 'main').",
                },
                "token": {
                    "type": "string",
                    "description": "Access token for a PRIVATE repo. Write-only; never echoed.",
                },
                "image": {"type": "string", "description": "Prebuilt container image reference."},
                "git_ref": {"type": "string", "description": "Legacy source ref, when relevant."},
            },
            "required": ["name", "box_id"],
            "additionalProperties": False,
        },
    )
    async def ship_create_app(args):  # type: ignore[no-untyped-def]
        return await _create_app_handler(args)

    @tool(
        "ship_deploy_app",
        (
            "Deploy an app's configured image. Runs in the background. NOTE: if "
            "the app is flagged PRODUCTION this does NOT deploy — it files a "
            "proposal for human approval and returns status 'proposed'. Relay "
            "that honestly; do not claim a prod deploy happened."
        ),
        {
            "type": "object",
            "properties": {"app_id": {"type": "string", "minLength": 1}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_deploy_app(args):  # type: ignore[no-untyped-def]
        return await _deploy_app_handler(args)

    @tool(
        "ship_add_domain",
        "Route a domain to an app and issue TLS for it.",
        {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "domain": {"type": "string", "minLength": 1},
            },
            "required": ["app_id", "domain"],
            "additionalProperties": False,
        },
    )
    async def ship_add_domain(args):  # type: ignore[no-untyped-def]
        return await _add_domain_handler(args)

    @tool(
        "ship_create_db",
        (
            "Attach a database to an app. `db_type` is postgres, redis, or mongo "
            "(defaults to mongo). Returns the service name and the NAME of the env "
            "var holding the connection string — never the credential itself."
        ),
        {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "db_type": {
                    "type": "string",
                    "enum": ["postgres", "redis", "mongo"],
                    "description": "Which database engine to provision + link.",
                },
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_create_db(args):  # type: ignore[no-untyped-def]
        return await _create_db_handler(args)

    @tool(
        "ship_set_scale",
        (
            'Set how many containers run per process type (e.g. {"web": 2, '
            '"worker": 1}). Scaling to 0 stops a process. Runs immediately.'
        ),
        {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "scale": {
                    "type": "object",
                    "description": 'Process name -> container count, e.g. {"web": 2}.',
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
            },
            "required": ["app_id", "scale"],
            "additionalProperties": False,
        },
    )
    async def ship_set_scale(args):  # type: ignore[no-untyped-def]
        return await _set_scale_handler(args)

    @tool(
        "ship_set_checks",
        (
            "Configure zero-downtime deploy checks for an app. `zero_downtime` "
            "toggles Dokku's settle-and-drain deploy (on by default); an optional "
            "`healthcheck_path` is the HTTP path the check hits."
        ),
        {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "zero_downtime": {"type": "boolean"},
                "healthcheck_path": {
                    "type": "string",
                    "description": "HTTP health path, e.g. /healthz (optional).",
                },
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_set_checks(args):  # type: ignore[no-untyped-def]
        return await _set_checks_handler(args)

    @tool(
        "ship_set_resources",
        (
            "Set an app's CPU and/or memory ceilings (the cost-control lever). "
            "`cpu` is in Dokku's CPU units, `memory_mb` in megabytes; a 0 leaves "
            "that dimension unlimited, but set at least one. Applies on next start."
        ),
        {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "cpu": {"type": "integer", "minimum": 0},
                "memory_mb": {"type": "integer", "minimum": 0},
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_set_resources(args):  # type: ignore[no-untyped-def]
        return await _set_resources_handler(args)

    @tool(
        "ship_create_volume",
        (
            "Attach a persistent volume to an app so its data survives redeploys. "
            "`mount_path` is the absolute container path (e.g. /data); `name` is "
            "optional and defaults to <app>-data. Takes effect on the next deploy."
        ),
        {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "mount_path": {
                    "type": "string",
                    "description": "Absolute container path to mount at, e.g. /data.",
                },
                "name": {"type": "string", "description": "Volume name (optional)."},
            },
            "required": ["app_id", "mount_path"],
            "additionalProperties": False,
        },
    )
    async def ship_create_volume(args):  # type: ignore[no-untyped-def]
        return await _create_volume_handler(args)

    @tool(
        "ship_restart",
        "Restart an app's containers — a graceful, reversible bounce.",
        {
            "type": "object",
            "properties": {"app_id": {"type": "string", "minLength": 1}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_restart(args):  # type: ignore[no-untyped-def]
        return await _restart_handler(args)

    @tool(
        "ship_rebuild",
        "Rebuild an app from its current source/image and restart it (reversible).",
        {
            "type": "object",
            "properties": {"app_id": {"type": "string", "minLength": 1}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_rebuild(args):  # type: ignore[no-untyped-def]
        return await _rebuild_handler(args)

    @tool(
        "ship_logs",
        "Read an app's recent log lines.",
        {
            "type": "object",
            "properties": {"app_id": {"type": "string", "minLength": 1}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    )
    async def ship_logs(args):  # type: ignore[no-untyped-def]
        return await _logs_handler(args)

    @tool(
        "ship_metrics",
        "Read a box's live CPU / memory / disk percentages.",
        {
            "type": "object",
            "properties": {"box_id": {"type": "string", "minLength": 1}},
            "required": ["box_id"],
            "additionalProperties": False,
        },
    )
    async def ship_metrics(args):  # type: ignore[no-untyped-def]
        return await _metrics_handler(args)

    @tool(
        "ship_request_destroy",
        (
            "PROPOSE tearing down a box or an app, for HUMAN APPROVAL. This does "
            "NOT destroy anything: it files the teardown in The Tray for a human "
            "to approve or reject, and only on approval does anything happen. "
            "Returns {status:'proposed', proposal_id}. NEVER tell the user "
            "something was destroyed or deleted — it is only PROPOSED."
        ),
        {
            "type": "object",
            "properties": {
                "box_id": {"type": "string", "description": "The box to tear down."},
                "app_id": {"type": "string", "description": "The app to tear down."},
            },
            "additionalProperties": False,
        },
    )
    async def ship_request_destroy(args):  # type: ignore[no-untyped-def]
        return await _request_destroy_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            ship_list_boxes,
            ship_provision_box,
            ship_list_apps,
            ship_create_app,
            ship_deploy_app,
            ship_add_domain,
            ship_create_db,
            ship_set_scale,
            ship_set_checks,
            ship_set_resources,
            ship_create_volume,
            ship_restart,
            ship_rebuild,
            ship_logs,
            ship_metrics,
            ship_request_destroy,
        ],
    )
    return SERVER_NAME, server


__all__ = ["SERVER_NAME", "SHIP_TOOL_IDS", "build_ship_server"]
