# sites_project.py — the agent's tools for PROJECT sites (engine ``project``): start a
# whole repo from a paw-sites base template, add backend recipes, edit any file, and
# build it in the sandbox. Registered on the ``pocketpaw_sites_manager`` server beside
# the html / svelte / react tools (``sites.build_sites_manager_server``), and every id
# rides ``SITES_TOOL_IDS`` so the /sites allow-list carries it.
#
# Twelve tools: ``list_site_templates``, ``start_site_from_template``,
# ``list_site_recipes``, ``apply_site_recipe``, ``list_site_files``,
# ``read_site_file``, ``read_site_files``, ``write_site_files``, ``patch_site_file``,
# ``delete_site_files``, ``run_site_build``, ``get_site_build_log``. File and build
# tools carry ``site`` in their names: pydantic-ai flattens toolsets, so a generic
# ``read_file`` here would collide with another server's. The CLI work, path policy,
# caps and plan gate live in ``sites/project_tools.py``; the persist is
# ``pockets.service.set_project_source``; builds and logs are A1's lane in
# ``sites/service.py`` / ``sites/project_build.py``.
#
# Invariants: the file tools refuse every engine but ``project`` (the other engines
# have their own edit tools and path rules); every write queues the draft build of
# the new source and answers ``verification.status: "pending"`` with the job id,
# like the fast-edit contract; a recipe conflict writes nothing; secret VALUES are
# never written by any tool here — recipes return secret NAMES and the agent asks
# the owner through ``request_site_secret`` (another lane; absent tools are named in
# text only, never called from here).
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args
from pocketpaw_ee.sites.capacity import reason_message

from ._audit import record_tool_call
from .sites_create import (
    SERVER_NAME,
    _bind_session_and_emit,
    _error_response,
    _identity,
    _mint_draft_site,
    _require_sites_plan_or_error,
    _success_response,
)

logger = logging.getLogger(__name__)

LIST_SITE_TEMPLATES_TOOL_ID = f"mcp__{SERVER_NAME}__list_site_templates"
START_SITE_FROM_TEMPLATE_TOOL_ID = f"mcp__{SERVER_NAME}__start_site_from_template"
LIST_SITE_RECIPES_TOOL_ID = f"mcp__{SERVER_NAME}__list_site_recipes"
APPLY_SITE_RECIPE_TOOL_ID = f"mcp__{SERVER_NAME}__apply_site_recipe"
LIST_SITE_FILES_TOOL_ID = f"mcp__{SERVER_NAME}__list_site_files"
READ_SITE_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__read_site_file"
READ_SITE_FILES_TOOL_ID = f"mcp__{SERVER_NAME}__read_site_files"
WRITE_SITE_FILES_TOOL_ID = f"mcp__{SERVER_NAME}__write_site_files"
PATCH_SITE_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__patch_site_file"
DELETE_SITE_FILES_TOOL_ID = f"mcp__{SERVER_NAME}__delete_site_files"
RUN_SITE_BUILD_TOOL_ID = f"mcp__{SERVER_NAME}__run_site_build"
GET_SITE_BUILD_LOG_TOOL_ID = f"mcp__{SERVER_NAME}__get_site_build_log"

SITES_PROJECT_TOOL_IDS = (
    LIST_SITE_TEMPLATES_TOOL_ID,
    START_SITE_FROM_TEMPLATE_TOOL_ID,
    LIST_SITE_RECIPES_TOOL_ID,
    APPLY_SITE_RECIPE_TOOL_ID,
    LIST_SITE_FILES_TOOL_ID,
    READ_SITE_FILE_TOOL_ID,
    READ_SITE_FILES_TOOL_ID,
    WRITE_SITE_FILES_TOOL_ID,
    PATCH_SITE_FILE_TOOL_ID,
    DELETE_SITE_FILES_TOOL_ID,
    RUN_SITE_BUILD_TOOL_ID,
    GET_SITE_BUILD_LOG_TOOL_ID,
)

#: ``run_site_build`` waits this long for the sandbox build, polling every few seconds.
RUN_BUILD_WAIT_SEC = 30.0
RUN_BUILD_POLL_SEC = 2.0
#: How much of a build log one tool result carries (the stored log keeps 64 KiB).
LOG_TAIL_CHARS = 12_000
#: ``list_site_files`` lists at most this many paths per call.
MAX_LISTED = 1_000

_TERMINAL = frozenset({"built", "failed"})

STATIC_PREVIEW_NOTE = (
    "preview_mode is static: the draft preview serves the built static assets only. "
    "Server routes (API routes, actions, server-rendered pages) run after publish. "
    "Say so when you show the preview."
)

SERVER_ONLY_PREVIEW_NOTE = (
    "preview_mode is server_only: every page is rendered by the site's server code and "
    "the build has no static entry page, so there is no draft preview (preview_url is "
    "null). The pages run after publish. Tell the user instead of showing a preview."
)

SECRETS_RULE = (
    "Never write a secret value into any file. For each secret name, call "
    "`request_site_secret` (pocket_id, name, description) so the owner fills it in; "
    "`list_site_secrets` shows which are set. If those tools are not available, tell "
    "the user which secrets the site needs and that they set them in the site's "
    "settings."
)

NOT_PROJECT = (
    "{tool} works on project sites only (engine=project). For an html, svelte or "
    "react site use read_site_source and that engine's edit tool."
)


def _structured_error(body: dict[str, Any]) -> dict[str, Any]:
    out = _success_response(body)
    out["is_error"] = True
    return out


def _with_text_blocks(body: dict[str, Any], blocks: list[str]) -> dict[str, Any]:
    """A JSON block plus plain-text blocks, never JSON-escaped (file contents and logs
    stay readable and are not inflated past the MCP output cap)."""
    content = [{"type": "text", "text": json.dumps(body, separators=(",", ":"), default=str)}]
    content += [{"type": "text", "text": b} for b in blocks]
    return {"content": content}


async def _begin(tool: str) -> tuple[str | None, str | None, dict | None]:
    """Identity, audit and the workspace Sites plan gate every tool runs first."""
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return (
            None,
            None,
            _error_response(
                f"{tool} requires workspace and user context (call from a cloud chat session)."
            ),
        )
    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name=f"_{tool}",
        status="ok",
        ok=True,
    )
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return None, None, gate
    return workspace_id, user_id, None


async def _project(
    tool: str, workspace_id: str, user_id: str, pocket_id: Any
) -> tuple[dict | None, dict | None]:
    """The project pocket, or an error naming why not (missing id, no access, wrong
    engine)."""
    if not isinstance(pocket_id, str) or not pocket_id:
        return None, _error_response(f"{tool} requires a `pocket_id` (the project site).")
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        pocket = await sites_service.project_pocket(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        )
    except CloudError as exc:
        if exc.code == "sites.not_a_project":
            return None, _error_response(NOT_PROJECT.format(tool=tool))
        return None, _error_response(f"{exc.code}: {exc.message}")
    return pocket, None


def _source(pocket: dict) -> dict[str, Any]:
    source = pocket.get("source")
    return dict(source) if isinstance(source, dict) else {}


def _project_meta(pocket: dict) -> dict[str, Any]:
    meta = pocket.get("siteMeta") or pocket.get("site_meta") or {}
    project = meta.get("project") if isinstance(meta, dict) else None
    return project if isinstance(project, dict) else {}


async def _queue_build(workspace_id: str, user_id: str, pocket_id: str) -> dict[str, Any]:
    """The shared ``project_tools.build_verification`` plus the next step for the agent."""
    from pocketpaw_ee.sites import project_tools

    out = await project_tools.build_verification(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    if out.get("status") == "pending":
        out["next"] = "call run_site_build to wait for it, or get_site_build_log to read it"
    return out


def _cli_error(exc: Exception) -> dict[str, Any]:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites.project_tools import ProjectCliError

    if isinstance(exc, (CloudError, ProjectCliError)):
        return _error_response(f"{exc.code}: {exc.message}")
    logger.warning("sites.project: tool failed", exc_info=exc)
    return _error_response(f"failed: {exc}")


def _file_error(tool: str, exc: Exception) -> dict[str, Any]:
    """A file operation's failure as the agent reads it: the wrong engine names the
    right tools, a missing file points at list_site_files, everything else by code."""
    from pocketpaw_ee.cloud._core.errors import CloudError

    if isinstance(exc, CloudError) and exc.code == "sites.not_a_project":
        return _error_response(NOT_PROJECT.format(tool=tool))
    if isinstance(exc, CloudError) and exc.code == "site_file.not_found":
        return _error_response(
            f"{exc.code}: {exc.message}. Nothing was changed; call list_site_files to see the tree."
        )
    return _cli_error(exc)


async def _persist(
    tool: str,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    source: dict[str, Any],
    *,
    writes: dict[str, str] | None = None,
    deletes: list[str] | None = None,
    add_recipe: str | None = None,
    label: str | None = None,
) -> tuple[list[str], dict | None]:
    """``project_tools.save_changes`` as ``(lockfiles_dropped, error)``."""
    from pocketpaw_ee.sites import project_tools

    try:
        dropped = await project_tools.save_changes(
            user_id=user_id,
            pocket_id=pocket_id,
            source=source,
            writes=writes,
            deletes=deletes,
            add_recipe=add_recipe,
            label=label,
        )
    except Exception as exc:  # noqa: BLE001
        return [], _file_error(tool, exc)
    return dropped, None


def _lockfile_note(dropped: list[str]) -> str:
    if not dropped:
        return ""
    return (
        f" The dependency change made {', '.join(dropped)} stale, so it was removed; the "
        "sandbox build resolves package.json fresh."
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


async def _list_site_templates_handler(args: dict) -> dict:
    _ws, _user, err = await _begin("list_site_templates")
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    try:
        templates = await project_tools.list_templates()
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc)
    return _success_response(
        {
            "ok": True,
            "templates": templates,
            "message": (
                "Pick the template whose when_to_use matches the request, then call "
                "start_site_from_template. recipes lists the backend recipes it supports."
            ),
        }
    )


async def _start_site_from_template_handler(args: dict) -> dict:
    tool = "start_site_from_template"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    slug = args.get("slug")
    brief = args.get("brief")
    if not isinstance(slug, str) or not slug.strip():
        return _error_response(f"{tool} requires `slug` (from list_site_templates).")
    if not isinstance(brief, str) or not brief.strip():
        return _error_response(f"{tool} requires `brief`: one or two sentences on the site.")
    slug = slug.strip()

    from pocketpaw_ee.cloud.pockets.service import agent_create
    from pocketpaw_ee.sites import project_tools

    try:
        template = await project_tools.find_template(slug)
        source = await project_tools.copy_template(slug)
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc)

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else None
    name = name or f"{template['name']} site"
    try:
        view, pocket_id, create_err = await agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name=name,
            description=brief.strip(),
            type_="site",
            pattern="landing",
            ripple_spec=None,
            engine="project",
            source=source,
            trusted=True,
            site_meta={"project": {"template": slug, "framework": slug, "recipes": []}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: persist raised", tool, exc_info=True)
        return _error_response(f"create failed: {exc}")
    if create_err is not None or view is None or pocket_id is None:
        return _error_response(f"create failed: {create_err or 'create returned no view'}")

    await _mint_draft_site(workspace_id, user_id, pocket_id, name)
    await _bind_session_and_emit(pocket_id, view, user_id)
    verification = await _queue_build(workspace_id, user_id, pocket_id)

    agents_md = source.get("AGENTS.md") if isinstance(source.get("AGENTS.md"), str) else ""
    body = {
        "ok": True,
        "status": "draft",
        "is_live": False,
        "pocket_id": pocket_id,
        "pocket": {"id": pocket_id, "name": name, "type": "site", "engine": "project"},
        "template": {
            "slug": slug,
            "name": template["name"],
            "target": template.get("target"),
            "recipes": template.get("recipes") or [],
        },
        "file_count": len(source),
        "verification": verification,
        "next_steps": [
            "Read AGENTS.md below: it is the map of this repo (where things go, commands, "
            "canonical snippets). Follow it over your own framework defaults.",
            "Need a database, accounts or storage? list_site_recipes, then "
            "apply_site_recipe for each, before writing the features that use them.",
            "Request each secret a recipe returns with request_site_secret.",
            "Edit with read_site_file / patch_site_file / write_site_files "
            "(list_site_files for the tree).",
            "run_site_build, and on failure get_site_build_log; fix and build again.",
        ],
        "message": (
            "The project is saved as a DRAFT site; nothing is live. AGENTS.md follows verbatim."
        ),
    }
    return _with_text_blocks(body, [f"=== FILE: AGENTS.md ===\n{agents_md}"])


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


async def _list_site_recipes_handler(args: dict) -> dict:
    _ws, _user, err = await _begin("list_site_recipes")
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    try:
        recipes = await project_tools.list_recipes()
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc)
    template = args.get("template")
    if isinstance(template, str) and template.strip():
        recipes = [r for r in recipes if template.strip() in r["applies_to"]]
    return _success_response(
        {
            "ok": True,
            "recipes": recipes,
            "message": (
                "plan is the lowest site plan a recipe needs. Apply `requires` first; "
                "never apply two recipes that list each other in `conflicts`."
            ),
        }
    )


async def _apply_site_recipe_handler(args: dict) -> dict:
    tool = "apply_site_recipe"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    recipe_id = args.get("recipe_id")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        return _error_response(f"{tool} requires `recipe_id` (from list_site_recipes).")
    recipe_id = recipe_id.strip()
    pocket_id = args.get("pocket_id")
    pocket, err = await _project(tool, workspace_id, user_id, pocket_id)
    if err:
        return err
    dry_run = bool(args.get("dry_run"))

    from pocketpaw_ee.sites import project_tools
    from pocketpaw_ee.sites import service as sites_service

    try:
        recipe = await project_tools.find_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc)
    template = _project_meta(pocket).get("template")
    template = template if isinstance(template, str) and template else None
    if template and template not in recipe["applies_to"]:
        return _error_response(
            f"sites.recipe_not_applicable: {recipe_id} does not apply to the {template} "
            f"template (it applies to {', '.join(recipe['applies_to'])})."
        )
    plan_tier, status = await sites_service.project_site_plan(
        workspace_id=workspace_id, pocket_id=pocket_id
    )
    if not project_tools.recipe_plan_allowed(
        recipe["plan"], plan_tier=plan_tier, subscription_status=status
    ):
        return _error_response(
            f"sites.recipe_plan_required: the {recipe['name']} recipe needs the "
            f"{recipe['plan']} site plan, and this site is not on it. Nothing was "
            "changed. Tell the user to upgrade this site's plan, or pick a recipe on "
            "the free plan."
        )

    source = _source(pocket)
    try:
        result, writes = await project_tools.apply_recipe(
            source, recipe_id, template=template, dry_run=dry_run
        )
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc)

    requests = {
        "binding_requests": result.get("bindingRequests") or [],
        "secrets": [
            {
                "name": s.get("name"),
                "required": bool(s.get("required")),
                "description": s.get("description") or "",
            }
            for s in result.get("secretRequests") or []
            if isinstance(s, dict)
        ],
        "env_requests": result.get("envRequests") or [],
        "glue_tasks": result.get("agentTasks") or [],
        "verify": result.get("verify") or [],
    }
    requests["secret_names"] = [s["name"] for s in requests["secrets"]]
    if result.get("errors"):
        return _structured_error(
            {
                "ok": False,
                "status": "error",
                "recipe": recipe_id,
                "errors": result["errors"],
                "message": "Nothing was written. Fix what the errors name and apply again.",
            }
        )
    if result.get("conflicts"):
        return _structured_error(
            {
                "ok": False,
                "status": "conflict",
                "recipe": recipe_id,
                "conflicts": result["conflicts"],
                "written": [],
                "message": (
                    "Nothing was written: the recipe would overwrite these. Resolve each "
                    "conflict (usually by moving or merging your own file), then apply again."
                ),
            }
        )
    if dry_run:
        return _success_response(
            {
                "ok": True,
                "status": "dry_run",
                "recipe": recipe_id,
                "would_write": result.get("written") or [],
                "packages_added": result.get("packagesAdded") or [],
                **requests,
                "message": "Dry run: nothing was written.",
            }
        )
    if result.get("alreadyApplied") or not writes:
        return _success_response(
            {
                "ok": True,
                "status": "already_applied",
                "recipe": recipe_id,
                "written": [],
                **requests,
                "message": f"{recipe_id} is already applied; nothing changed. {SECRETS_RULE}",
            }
        )

    dropped, err = await _persist(
        tool,
        workspace_id,
        user_id,
        pocket_id,
        source,
        writes=writes,
        add_recipe=recipe_id,
        label=f"Applied recipe {recipe_id}",
    )
    if err:
        return err
    verification = await _queue_build(workspace_id, user_id, pocket_id)
    return _success_response(
        {
            "ok": True,
            "status": "draft",
            "is_live": False,
            "recipe": recipe_id,
            "version": result.get("version"),
            "written": sorted(writes),
            "packages_added": result.get("packagesAdded") or [],
            "migrations": result.get("migrations") or [],
            "lockfile_removed": dropped,
            **requests,
            "verification": verification,
            "message": (
                f"{recipe_id} is applied to the draft. Now do every glue task in order "
                f"(read AGENTS.md again: the recipe added a section). {SECRETS_RULE}"
                + _lockfile_note(dropped)
            ),
        }
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


async def _list_site_files_handler(args: dict) -> dict:
    tool = "list_site_files"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(f"{tool} requires a `pocket_id` (the project site).")
    prefix = args.get("prefix")
    try:
        listed = await project_tools.list_files(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            prefix=prefix if isinstance(prefix, str) else None,
        )
    except Exception as exc:  # noqa: BLE001
        return _file_error(tool, exc)
    files = listed["files"]
    return _success_response(
        {
            "ok": True,
            "pocket_id": pocket_id,
            "files": files[:MAX_LISTED],
            "file_count": len(files),
            "truncated": len(files) > MAX_LISTED,
        }
    )


async def _read(tool: str, args: dict, paths: Any) -> dict:
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(f"{tool} requires a `pocket_id` (the project site).")
    try:
        files = await project_tools.read_files(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, paths=paths
        )
    except Exception as exc:  # noqa: BLE001
        return _file_error(tool, exc)
    # The agent's view is capped (the HTTP read is not: a client that saves a file back
    # must hold all of it). A truncated file is nearly always a generated one.
    meta: list[dict[str, Any]] = []
    blocks: list[str] = []
    budget = project_tools.MAX_READ_CALL_BYTES
    for f in files:
        data = f["content"].encode("utf-8")
        entry: dict[str, Any] = {"path": f["path"], "size": f["size"]}
        if budget <= 0:
            entry["omitted"] = True
            meta.append(entry)
            continue
        cap = min(project_tools.MAX_READ_FILE_BYTES, budget)
        if len(data) > cap:
            entry["truncated"] = True
        budget -= min(len(data), cap)
        meta.append(entry)
        shown = data[:cap].decode("utf-8", "ignore")
        blocks.append(f"=== FILE: {f['path']} ({f['size']} bytes) ===\n{shown}")
    body = {
        "ok": True,
        "files": meta,
        "message": (
            "Each file follows verbatim after its `=== FILE ===` header line (the header "
            "is not part of the file). Copy `old` for patch_site_file exactly from it. "
            "truncated: only the start is shown (usually a generated file you should not "
            "edit); omitted: over this call's read budget, read it in another call."
        ),
    }
    return _with_text_blocks(body, blocks)


async def _read_site_file_handler(args: dict) -> dict:
    return await _read("read_site_file", args, [args.get("path")])


async def _read_site_files_handler(args: dict) -> dict:
    args = coerce_json_object_args(args, ("paths",))
    return await _read("read_site_files", args, args.get("paths"))


def _draft_body(result: dict[str, Any], message: str) -> dict[str, Any]:
    out = {"ok": True, "status": "draft", "is_live": False, **result}
    out["message"] = message + _lockfile_note(result.get("lockfile_removed") or [])
    if out.get("verification", {}).get("status") == "pending":
        out["verification"]["next"] = "call run_site_build to wait for it, or get_site_build_log"
    return _success_response(out)


async def _write_site_files_handler(args: dict) -> dict:
    tool = "write_site_files"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    args = coerce_json_object_args(args, ("files",))
    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(f"{tool} requires a `pocket_id` (the project site).")
    try:
        result = await project_tools.write_files(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            files=args.get("files"),
        )
    except Exception as exc:  # noqa: BLE001
        return _file_error(tool, exc)
    return _draft_body(result, "Saved to the draft; nothing is live.")


async def _patch_site_file_handler(args: dict) -> dict:
    tool = "patch_site_file"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    args = coerce_json_object_args(args, ("edits",))
    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(f"{tool} requires a `pocket_id` (the project site).")
    try:
        result = await project_tools.patch_file(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            path=args.get("path"),
            edits=args.get("edits"),
        )
    except Exception as exc:  # noqa: BLE001
        return _file_error(tool, exc)
    return _draft_body(result, "Saved to the draft; nothing is live.")


async def _delete_site_files_handler(args: dict) -> dict:
    tool = "delete_site_files"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    from pocketpaw_ee.sites import project_tools

    args = coerce_json_object_args(args, ("paths",))
    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(f"{tool} requires a `pocket_id` (the project site).")
    try:
        result = await project_tools.delete_files(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            paths=args.get("paths"),
        )
    except Exception as exc:  # noqa: BLE001
        return _file_error(tool, exc)
    return _draft_body(result, "Deleted from the draft; nothing is live.")


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


def _log_tail(log: str) -> tuple[str, bool]:
    if len(log) <= LOG_TAIL_CHARS:
        return log, False
    return log[-LOG_TAIL_CHARS:], True


async def _wait_for_build(pocket_id: str, job_id: str) -> dict[str, Any] | None:
    """Poll the build record until it is built or failed, or the wait runs out."""
    from pocketpaw_ee.sites import project_build, verify_store

    store = verify_store.default_verify_store()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUN_BUILD_WAIT_SEC
    record = project_build.read_build_record(store, pocket_id, job_id)
    while not (record and record.get("status") in _TERMINAL) and loop.time() < deadline:
        await asyncio.sleep(RUN_BUILD_POLL_SEC)
        record = project_build.read_build_record(store, pocket_id, job_id)
    return record


async def _run_site_build_handler(args: dict) -> dict:
    tool = "run_site_build"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    pocket_id = args.get("pocket_id")
    _pocket, err = await _project(tool, workspace_id, user_id, pocket_id)
    if err:
        return err
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        art = await sites_service.queue_project_build(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        )
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc)
    job_id = art.get("build_job_id")
    record: dict[str, Any] | None = None
    if not (art.get("build_status") == "none" and art.get("preview_mode")) and job_id:
        record = await _wait_for_build(pocket_id, job_id)
        if record and record.get("status") == "built":
            art = await sites_service.queue_project_build(
                workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
            )
    status = (
        "built"
        if art.get("build_status") == "none" and art.get("preview_mode")
        else (record or {}).get("status") or art.get("build_status") or "queued"
    )
    preview_mode = art.get("preview_mode") or (record or {}).get("preview_mode")
    body: dict[str, Any] = {
        "ok": status != "failed",
        "status": status,
        "job_id": job_id,
        "preview_mode": preview_mode,
        "preview_url": art.get("preview_url") if status == "built" else None,
    }
    blocks: list[str] = []
    if status == "built":
        note = {"static": STATIC_PREVIEW_NOTE, "server_only": SERVER_ONLY_PREVIEW_NOTE}
        body["message"] = "The draft built." + (
            " " + note[preview_mode] if preview_mode in note else ""
        )
    elif status == "failed" and reason_message(
        reason := (record or {}).get("reason") or art.get("build_reason")
    ):
        # No sandbox ran, so there is no log and nothing in the files to fix.
        body["reason"] = reason
        body["message"] = reason_message(reason)
    elif status == "failed":
        body["reason"] = (record or {}).get("reason") or art.get("build_reason")
        tail, cut = _log_tail(str((record or {}).get("log") or ""))
        body["log_truncated"] = cut or bool((record or {}).get("log_truncated"))
        body["message"] = (
            "The build failed. The log tail follows; the error is usually near the end. "
            "Fix the files it names and run_site_build again."
        )
        blocks.append(f"=== BUILD LOG ({job_id}) ===\n{tail}")
    else:
        waiting = reason_message((record or {}).get("reason") or art.get("build_reason"))
        body["message"] = (waiting + " " if waiting else "") + (
            f"Still {status} after {int(RUN_BUILD_WAIT_SEC)}s. Keep working and call "
            "run_site_build or get_site_build_log again shortly; do not report the site as built."
        )
    out = _with_text_blocks(body, blocks)
    if status == "failed":
        out["is_error"] = True
    return out


async def _get_site_build_log_handler(args: dict) -> dict:
    tool = "get_site_build_log"
    workspace_id, user_id, err = await _begin(tool)
    if err:
        return err
    pocket_id = args.get("pocket_id")
    _pocket, err = await _project(tool, workspace_id, user_id, pocket_id)
    if err:
        return err
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    job_id = args.get("job_id")
    try:
        latest = None
        if not isinstance(job_id, str) or not job_id:
            latest = await sites_service.project_latest_build(
                workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
            )
            job_id = latest["job_id"]
        result = await sites_service.project_build_log(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, job_id=job_id
        )
    except CloudError as exc:
        if exc.code.endswith("not_found") or getattr(exc, "status_code", None) == 404:
            return _error_response(
                f"{exc.code}: no build found. Call run_site_build to build the draft first."
            )
        return _error_response(f"{exc.code}: {exc.message}")
    tail, cut = _log_tail(result.get("log") or "")
    body = {
        "ok": True,
        "job_id": job_id,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "preview_mode": result.get("preview_mode"),
        "current": (latest or {}).get("current"),
        "log_truncated": cut or bool(result.get("log_truncated")),
        "updated_at": result.get("updated_at"),
        "message": "The log is redacted; secrets and sandbox paths are removed.",
    }
    return _with_text_blocks(body, [f"=== BUILD LOG ({job_id}) ===\n{tail}"])


# ---------------------------------------------------------------------------
# Tool factories
# ---------------------------------------------------------------------------

_POCKET = {
    "type": "string",
    "minLength": 1,
    "description": "Id of the project site pocket (from start_site_from_template).",
}


def make_project_tools(tool: Any) -> list[Any]:
    """Build the twelve project-site tools with the SDK's ``tool`` decorator."""

    @tool(
        "list_site_templates",
        (
            "List the base app templates a PROJECT site starts from (full-stack repos: "
            "Astro, TanStack Start, Vite+React+Hono, Next, SvelteKit, all on Cloudflare "
            "Workers). Returns {slug, name, summary, when_to_use, stack, target, "
            "recipes} per template. Use a project site for full-stack apps, accounts, a "
            "database, or when the user names a framework; plain marketing pages stay "
            "on the html / react tracks."
        ),
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def list_site_templates(args):  # type: ignore[no-untyped-def]
        return await _list_site_templates_handler(args)

    @tool(
        "start_site_from_template",
        (
            "Create a PROJECT site (engine=project) from a base template: copies the "
            "whole repo into a new draft site pocket and returns its pocket_id, the "
            "template's AGENTS.md (the repo's map: read it before editing) and the next "
            "steps. Call ONCE per site; every later change goes through the file and "
            "recipe tools on the same pocket_id. Nothing is published."
        ),
        {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Template slug from list_site_templates.",
                },
                "brief": {
                    "type": "string",
                    "minLength": 1,
                    "description": "One or two sentences: what the site is and does.",
                },
                "name": {"type": "string", "description": "Site name (optional)."},
            },
            "required": ["slug", "brief"],
            "additionalProperties": False,
        },
    )
    async def start_site_from_template(args):  # type: ignore[no-untyped-def]
        return await _start_site_from_template_handler(args)

    @tool(
        "list_site_recipes",
        (
            "List the backend recipes a project site can add (D1 + Drizzle, accounts, "
            "KV, R2, Supabase): id, summary, applies_to, requires, conflicts, plan (the "
            "lowest site plan it needs), bindings, secret names. Pass `template` to "
            "list only the recipes for that template."
        ),
        {
            "type": "object",
            "properties": {"template": {"type": "string", "description": "Template slug filter."}},
            "additionalProperties": False,
        },
    )
    async def list_site_recipes(args):  # type: ignore[no-untyped-def]
        return await _list_site_recipes_handler(args)

    @tool(
        "apply_site_recipe",
        (
            "Add one backend recipe to a project site: writes its files, packages, "
            "wrangler binding requests (names only), migrations and AGENTS.md section "
            "into the draft. Returns glue_tasks (do them, in order), secret names and "
            "env names. For each secret call request_site_secret; NEVER write a secret "
            "value into a file. On a conflict NOTHING is written and the conflicts are "
            "returned. Refused when the site's plan is below the recipe's plan. "
            "`dry_run` shows what would change."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "recipe_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Recipe id from list_site_recipes.",
                },
                "dry_run": {"type": "boolean", "description": "Plan only; write nothing."},
            },
            "required": ["pocket_id", "recipe_id"],
            "additionalProperties": False,
        },
    )
    async def apply_site_recipe(args):  # type: ignore[no-untyped-def]
        return await _apply_site_recipe_handler(args)

    @tool(
        "list_site_files",
        (
            "List a project site's files (path + bytes). `prefix` narrows it to a "
            "directory, e.g. 'src/'. Project sites only."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "prefix": {"type": "string", "description": "Only paths starting with this."},
            },
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def list_site_files(args):  # type: ignore[no-untyped-def]
        return await _list_site_files_handler(args)

    @tool(
        "read_site_file",
        (
            "Read one file of a project site, verbatim, in a text block after a "
            "`=== FILE ===` header. Very large (usually generated) files are truncated."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "path": {"type": "string", "minLength": 1, "description": "Relative path."},
            },
            "required": ["pocket_id", "path"],
            "additionalProperties": False,
        },
    )
    async def read_site_file(args):  # type: ignore[no-untyped-def]
        return await _read_site_file_handler(args)

    @tool(
        "read_site_files",
        (
            "Read several files of a project site in one call (each in its own "
            "verbatim block). Prefer it over repeated read_site_file calls."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Relative paths.",
                },
            },
            "required": ["pocket_id", "paths"],
            "additionalProperties": False,
        },
    )
    async def read_site_files(args):  # type: ignore[no-untyped-def]
        return await _read_site_files_handler(args)

    @tool(
        "write_site_files",
        (
            "Create or overwrite files of a project site: `files` maps a relative path "
            "to its FULL new contents. Paths are relative to the repo root, no '..'; "
            "node_modules, .git, .paw/, paw-build.json and real .env / .dev.vars files "
            "are refused (secret values never go in the source). Changing package.json "
            "dependencies drops the stale lockfile. Saves to the draft and queues a "
            "build (verification.status pending). Prefer patch_site_file for small edits."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "{relative path: full file contents}.",
                },
            },
            "required": ["pocket_id", "files"],
            "additionalProperties": False,
        },
    )
    async def write_site_files(args):  # type: ignore[no-untyped-def]
        return await _write_site_files_handler(args)

    @tool(
        "patch_site_file",
        (
            "Edit one existing file of a project site with search/replace blocks. Each "
            "`old` must match the current file EXACTLY ONCE (copy it from read_site_file); "
            "blocks apply in order. 0 or several matches fails and saves nothing. Saves "
            "to the draft and queues a build."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "path": {"type": "string", "minLength": 1, "description": "Relative path."},
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string", "description": "Exact text to replace."},
                            "new": {"type": "string", "description": "Replacement text."},
                        },
                        "required": ["old", "new"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["pocket_id", "path", "edits"],
            "additionalProperties": False,
        },
    )
    async def patch_site_file(args):  # type: ignore[no-untyped-def]
        return await _patch_site_file_handler(args)

    @tool(
        "delete_site_files",
        (
            "Delete files from a project site's draft. Every path must exist (else "
            "nothing is deleted); package.json cannot be deleted."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["pocket_id", "paths"],
            "additionalProperties": False,
        },
    )
    async def delete_site_files(args):  # type: ignore[no-untyped-def]
        return await _delete_site_files_handler(args)

    @tool(
        "run_site_build",
        (
            "Build a project site's current draft in the sandbox (install, build, "
            "wrangler dry-run) and wait up to ~30 s. Returns status built / failed / "
            "still building, the preview_url, preview_mode, and on failure the log "
            "tail. preview_mode static means the draft serves static assets only and "
            "server routes run after publish: tell the user. Publishing needs a "
            "finished build of the current files."
        ),
        {
            "type": "object",
            "properties": {"pocket_id": _POCKET},
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def run_site_build(args):  # type: ignore[no-untyped-def]
        return await _run_site_build_handler(args)

    @tool(
        "get_site_build_log",
        (
            "Read a project site build's status and redacted log tail: the latest "
            "build, or `job_id` (from run_site_build or a write's verification)."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "job_id": {"type": "string", "description": "A build job id (optional)."},
            },
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def get_site_build_log(args):  # type: ignore[no-untyped-def]
        return await _get_site_build_log_handler(args)

    return [
        list_site_templates,
        start_site_from_template,
        list_site_recipes,
        apply_site_recipe,
        list_site_files,
        read_site_file,
        read_site_files,
        write_site_files,
        patch_site_file,
        delete_site_files,
        run_site_build,
        get_site_build_log,
    ]


__all__ = [
    "APPLY_SITE_RECIPE_TOOL_ID",
    "DELETE_SITE_FILES_TOOL_ID",
    "GET_SITE_BUILD_LOG_TOOL_ID",
    "LIST_SITE_FILES_TOOL_ID",
    "LIST_SITE_RECIPES_TOOL_ID",
    "LIST_SITE_TEMPLATES_TOOL_ID",
    "PATCH_SITE_FILE_TOOL_ID",
    "READ_SITE_FILES_TOOL_ID",
    "READ_SITE_FILE_TOOL_ID",
    "RUN_SITE_BUILD_TOOL_ID",
    "SITES_PROJECT_TOOL_IDS",
    "START_SITE_FROM_TEMPLATE_TOOL_ID",
    "WRITE_SITE_FILES_TOOL_ID",
    "make_project_tools",
]
