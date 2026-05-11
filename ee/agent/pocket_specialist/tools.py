"""LangChain ``StructuredTool`` factories for the pocket specialist's
internal use.

Each factory closes over ``workspace_id`` and ``user_id``, so those are
NEVER tool arguments visible to the LLM. The LLM cannot accidentally
cross workspaces — multi-tenancy stays enforced even if the model
hallucinates argument names.

The thunk indirections (``_agent_list_pockets``, ``_agent_create``,
``_agent_update``, ``_get_manifest``) are bound at module level so
tests can patch ``ee.agent.pocket_specialist.tools.<name>`` without
reaching into ``ee.cloud`` internals.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ee.cloud.pockets.service import agent_create as _agent_create
from ee.cloud.pockets.service import agent_list as _agent_list_pockets
from ee.cloud.pockets.service import agent_update as _agent_update
from ee.ripple.manifest import get_manifest as _get_manifest
from ee.ripple.manifest import validate_against_manifest


class _ListPocketsArgs(BaseModel):
    """No arguments — workspace is closed over by the factory."""


def make_list_pockets_tool(*, workspace_id: str, user_id: str) -> StructuredTool:
    """Build a ``list_pockets`` tool bound to the given workspace/user.

    Returns a compact list of ``{id, name, description, type, icon, color, owner}``.
    """

    async def _run() -> list[dict[str, Any]]:
        return await _agent_list_pockets(workspace_id, user_id)

    return StructuredTool.from_function(
        coroutine=_run,
        name="list_pockets",
        description=(
            "List existing pockets in the current workspace. Call this BEFORE "
            "drafting a new spec to decide whether to extend an existing pocket "
            "or create a new one. Returns a compact list of "
            "{id, name, description, type, icon, color, owner}."
        ),
        args_schema=_ListPocketsArgs,
    )


class _ValidateSpecArgs(BaseModel):
    spec: dict[str, Any] = Field(..., description="The rippleSpec to validate.")


def _format_issue(issue: dict[str, Any]) -> str:
    """Render a manifest validator issue as a single human-readable line."""
    parts: list[str] = []
    path = issue.get("path", "<root>")
    type_ = issue.get("type", "?")
    unknown = issue.get("unknown_props") or []
    allowed = issue.get("allowed_props") or []
    item_issues = issue.get("item_issues") or []
    if unknown:
        parts.append(
            f"{path} ({type_}): unknown props {unknown}"
            + (f"; allowed={allowed}" if allowed else "")
        )
    for item in item_issues:
        parts.append(
            f"{item.get('path', path)}: '{item.get('from')}' -> '{item.get('to')}'"
            + (" (auto-fixed)" if item.get("applied") else "")
        )
    # validate_against_manifest only emits issues when unknown_props or
    # item_issues is non-empty, so parts is guaranteed non-empty here.
    return "; ".join(parts)


def make_validate_spec_tool(*, capture: dict[str, Any] | None = None) -> StructuredTool:
    """Build a ``validate_spec`` tool that checks a draft rippleSpec
    against the live widget manifest.

    Returns ``{"ok": bool, "warnings": [str, ...]}``. If the manifest is
    unavailable (offline, fetch error), the tool returns ``ok=True`` with
    an empty warnings list — best-effort, never block the user.

    If ``capture`` is provided, ``capture["last_validation"]`` is set to
    the most recent result dict. This is the runtime's side-channel for
    reading validator output without parsing truncated tool_result content
    (most agent backends don't surface tool return values verbatim).
    """

    # Lazy-import settings inside the thunk to avoid pulling pocketpaw
    # config at module import time (keeps test isolation clean).
    async def _run(spec: dict[str, Any]) -> dict[str, Any]:
        from pocketpaw.config import get_settings

        settings = get_settings()
        manifest = await _get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        if manifest is None:
            result: dict[str, Any] = {"ok": True, "warnings": []}
        else:
            issues = validate_against_manifest(spec, manifest, apply_aliases=True)
            warnings = [_format_issue(issue) for issue in issues]
            result = {"ok": len(warnings) == 0, "warnings": warnings}
        if capture is not None:
            capture["last_validation"] = result
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="validate_spec",
        description=(
            "Validate a draft rippleSpec against the renderer's widget "
            "manifest. Returns {ok, warnings}. Re-draft and re-validate if "
            "warnings is non-empty. After max retries (default 3), persist "
            "anyway — never block the user."
        ),
        args_schema=_ValidateSpecArgs,
    )


class _PersistPocketArgs(BaseModel):
    name: str | None = Field(
        default=None,
        description="Required when creating; ignored when target_pocket_id is set.",
    )
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    ripple_spec: dict[str, Any] = Field(..., description="The validated rippleSpec.")
    target_pocket_id: str | None = Field(
        default=None,
        description=("When set, updates the existing pocket. When None, creates a new one."),
    )


def make_persist_pocket_tool(
    *,
    workspace_id: str,
    user_id: str,
    capture: dict[str, Any] | None = None,
    max_validation_retries: int = 3,
) -> StructuredTool:
    """Build a ``persist_pocket`` tool bound to the given workspace/user.

    Creates a new pocket when ``target_pocket_id`` is None; updates an
    existing pocket otherwise. Returns the pocket view dict on success.
    Raises ``RuntimeError`` on persist failure (the runtime catches and
    surfaces the error to the agent).

    **Validation retry loop:** when the manifest validator returns
    warnings AND we haven't yet hit ``max_validation_retries``, the tool
    REFUSES to persist and returns ``{ok: false, redraft_required: True,
    warnings: [...]}``. The model sees the warnings, fixes the spec, and
    calls again. On attempt ``max_validation_retries + 1`` we persist
    regardless — never block the user with a perma-retry loop.

    If ``capture`` is provided, ``capture["pocket"]`` is set to the
    persisted pocket view when the tool runs successfully. This is the
    runtime's side-channel for getting the actual return value out of the
    LangGraph/MCP boundary, since most agent backends don't surface tool
    return values verbatim in tool_result events.
    """

    async def _run(
        ripple_spec: dict[str, Any],
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        target_pocket_id: str | None = None,
    ) -> dict[str, Any]:
        # Inline manifest validation — replaces the separate validate_spec
        # tool round-trip. apply_aliases=True normalizes known prop aliases
        # in-place; remaining warnings are surfaced in the run output.
        from pocketpaw.config import get_settings

        settings = get_settings()
        manifest = await _get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        warnings: list[str] = []
        if manifest is not None:
            issues = validate_against_manifest(ripple_spec, manifest, apply_aliases=True)
            warnings = [_format_issue(issue) for issue in issues]
        if capture is not None:
            capture["warnings"] = warnings
            attempt = int(capture.get("attempt", 0)) + 1
            capture["attempt"] = attempt
        else:
            attempt = 1

        if warnings and attempt <= max_validation_retries:
            return {
                "ok": False,
                "redraft_required": True,
                "attempt": attempt,
                "max_attempts": max_validation_retries + 1,
                "warnings": warnings,
                "message": (
                    "Your spec has invalid props that the renderer will ignore "
                    "(widgets render `undefined`). Re-draft using ONLY the props "
                    "listed in each widget's `allowed_props` and call "
                    "persist_pocket again. Common issue: `chart` accepts "
                    "{data, type, title, height, colors, tooltip} — NOT "
                    "series/xAxis/dataKey/categoryKey. Each chart data point "
                    "is {label, value} directly."
                ),
            }

        if target_pocket_id is not None:
            view, err = await _agent_update(
                pocket_id=target_pocket_id,
                name=name,
                description=description,
                icon=icon,
                color=color,
                ripple_spec=ripple_spec,
            )
            if err is not None or view is None:
                raise RuntimeError(f"persist failed: {err or 'update returned no view'}")
            if capture is not None:
                capture["pocket"] = view
            return view
        view, new_pocket_id, err = await _agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name=name or "Untitled pocket",
            description=description or "",
            icon=icon or "",
            color=color or "",
            ripple_spec=ripple_spec,
        )
        if err is not None or view is None or new_pocket_id is None:
            raise RuntimeError(f"persist failed: {err or 'create returned no view'}")

        # Bind the active chat session to the newly-created pocket AND
        # push the ``pocket_created`` SSE event so the frontend auto-
        # opens it. Both happen atomically with creation; we no longer
        # depend on the main agent's tool_result event being parsed by
        # ``_maybe_handle_specialist_response`` (backend-dependent and
        # was silently failing for some flows).
        #
        # The specialist runs as an inline ``await`` from the MCP tool
        # handler so it shares the parent stream's contextvars — the
        # session_mongo_id set in agent_router.attach_agent_identity
        # AND the SSE event sink bound by attach_sse_event_sink are
        # both reachable from here.
        try:
            from ee.cloud.chat.agent_service import (
                current_session_mongo_id,
                push_sse_event,
            )
            from ee.cloud.sessions import service as sessions_service

            session_mongo_id = current_session_mongo_id()
            if session_mongo_id:
                linked = await sessions_service.attach_pocket_to_session_doc(
                    session_mongo_id, user_id, new_pocket_id
                )
                if linked is None and capture is not None:
                    # Owner mismatch / save failure / missing doc — already
                    # logged at WARN by the service. Surface in capture so
                    # the runtime can include it in warnings.
                    existing = list(capture.get("warnings", []))
                    existing.append(
                        f"session->pocket bind skipped for session "
                        f"{session_mongo_id} (owner mismatch or missing)"
                    )
                    capture["warnings"] = existing

            # Frontend auto-open trigger. The desktop client listens for
            # ``pocket_created`` and mounts the new pocket without waiting
            # for a sidebar refresh. Pushing this from inside persist_pocket
            # means the canvas opens the moment the row hits Mongo — no
            # gap waiting for the parent agent's reply.
            push_sse_event(
                "pocket_created",
                {
                    "pocket_id": new_pocket_id,
                    "pocket": view,
                    "session_id": session_mongo_id,
                },
            )
        except Exception:
            # Never let bind or SSE-push failure break pocket creation —
            # the pocket already exists in mongo and that's the primary
            # contract of persist_pocket.
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "persist_pocket: post-create side effects failed (non-fatal)",
                exc_info=True,
            )

        if capture is not None:
            capture["pocket"] = view
        return view

    return StructuredTool.from_function(
        coroutine=_run,
        name="persist_pocket",
        description=(
            "Persist the rippleSpec as a new pocket OR update an existing one. "
            "Pass target_pocket_id to update; omit to create. "
            "Validates props against the live widget manifest BEFORE saving. "
            "If your spec uses invented props (e.g. `chart.series`, "
            "`chart.xAxis`, `chart.categoryKey` — all hallucinated, real chart "
            "props are {data, type, title, height, colors, tooltip}), the tool "
            "returns `{ok: false, redraft_required: true, warnings: [...]}` "
            "WITHOUT persisting; re-draft the spec and call again. After "
            "max_validation_retries (default 3) failed attempts the tool "
            "persists anyway and you exit. On success returns the pocket view."
        ),
        args_schema=_PersistPocketArgs,
    )


# ---------------------------------------------------------------------------
# Edit-specialist tools — granular ops keyed to a specific pocket_id.
#
# Each factory closes over ``pocket_id`` so the LLM never sees it as a
# parameter — the edit specialist can't accidentally target the wrong
# pocket, and the spec is one field smaller per tool. workspace_id +
# user_id come from the per-stream ContextVars in agent_service; the
# granular service ops read them directly when they push SSE events.
# ---------------------------------------------------------------------------


def _capture_op(capture: dict[str, Any] | None, op: str, args: dict[str, Any]) -> None:
    """Append an op record to ``capture['ops']`` for the runtime to inspect."""
    if capture is None:
        return
    ops = capture.get("ops")
    if not isinstance(ops, list):
        ops = []
        capture["ops"] = ops
    ops.append({"op": op, "args": args})


class _GetPocketArgs(BaseModel):
    """No arguments — pocket id is closed over."""


def make_get_pocket_tool(*, pocket_id: str) -> StructuredTool:
    """Read the current pocket document. Returns the full pocket view
    including ``rippleSpec.ui`` and ``rippleSpec.state``."""

    async def _run() -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import fetch_pocket_for_agent

        return await fetch_pocket_for_agent(pocket_id)

    return StructuredTool.from_function(
        coroutine=_run,
        name="get_pocket",
        description=(
            "Read the current pocket document. Call ONCE at the start of "
            "your edit run to see the existing rippleSpec.ui (widget tree) "
            "and rippleSpec.state (data). Returns the full pocket view."
        ),
        args_schema=_GetPocketArgs,
    )


class _SetStateArgs(BaseModel):
    path: str = Field(..., description="Dotted path with optional bracket indices.")
    value: Any = Field(..., description="New value — any JSON-serialisable type.")


def make_set_state_tool(*, pocket_id: str, capture: dict[str, Any] | None = None) -> StructuredTool:
    """Update one value in ``state`` at ``path``. Widgets bound to
    ``{state.<path>}`` re-render automatically — cheapest data edit."""

    async def _run(path: str, value: Any) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import set_state_for_agent

        result = await set_state_for_agent(pocket_id, path, value)
        _capture_op(capture, "set_state", {"path": path, "value": value})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="set_state",
        description=(
            "Cheapest data edit. Write `value` into the pocket's state at "
            "`path` (dotted, with optional bracket indices: "
            "`tasks[0].status`). Every widget bound to {state.<path>} "
            "re-renders automatically. Use for label tweaks, status toggles, "
            "filter changes, anything DATA."
        ),
        args_schema=_SetStateArgs,
    )


class _AppendStateArgs(BaseModel):
    path: str
    item: Any = Field(..., description="Element to append.")


def make_append_state_tool(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> StructuredTool:
    """Append an item to an array in state."""

    async def _run(path: str, item: Any) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import append_state_for_agent

        result = await append_state_for_agent(pocket_id, path, item)
        _capture_op(capture, "append_state", {"path": path, "item": item})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="append_state",
        description=(
            "Append `item` to the array at `path` in state. Creates an "
            "empty list if the path is absent. Use for adding tasks, "
            "comments, log entries, kanban cards."
        ),
        args_schema=_AppendStateArgs,
    )


class _RemoveStateArgs(BaseModel):
    path: str


def make_remove_state_tool(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> StructuredTool:
    """Remove a key or array element from state."""

    async def _run(path: str) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import remove_state_for_agent

        result = await remove_state_for_agent(pocket_id, path)
        _capture_op(capture, "remove_state", {"path": path})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="remove_state",
        description=(
            "Delete a key or array element from state. For dict keys, "
            "deletes the key. For list indices (`tasks[1]`), removes the "
            "element and shifts subsequent indices down."
        ),
        args_schema=_RemoveStateArgs,
    )


class _PatchStateArgs(BaseModel):
    partial: dict[str, Any]


def make_patch_state_tool(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> StructuredTool:
    """Batched top-level merge into state."""

    async def _run(partial: dict[str, Any]) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import patch_state_for_agent

        result = await patch_state_for_agent(pocket_id, partial)
        _capture_op(capture, "patch_state", {"partial": partial})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="patch_state",
        description=(
            "Shallow-merge `partial` into the top of state. Use for batched "
            "independent-key writes (resetting a form, clearing several "
            "flags at once). Nested dicts are REPLACED, not deep-merged."
        ),
        args_schema=_PatchStateArgs,
    )


class _SetNodePropArgs(BaseModel):
    node_id: str
    prop: str
    value: Any


def make_set_node_prop_tool(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> StructuredTool:
    """Update a single prop on a widget node."""

    async def _run(node_id: str, prop: str, value: Any) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import set_node_prop_for_agent

        result = await set_node_prop_for_agent(pocket_id, node_id, prop, value)
        _capture_op(capture, "set_node_prop", {"node_id": node_id, "prop": prop})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="set_node_prop",
        description=(
            "Change ONE prop on a widget. `prop` writes into props by "
            "default; top-level keys (show, bind, class, style, on_click, "
            "etc.) are addressable by bare name. Dotted paths "
            "(`data.rows`) walk inside props. Use for label, color, "
            "show-conditions, on_click handlers."
        ),
        args_schema=_SetNodePropArgs,
    )


class _AddNodeArgs(BaseModel):
    parent_id: str
    spec: dict[str, Any] = Field(..., description="UINode to insert.")
    after_id: str | None = Field(default=None, description="Insert after this sibling.")


def make_add_node_tool(*, pocket_id: str, capture: dict[str, Any] | None = None) -> StructuredTool:
    """Add a new widget under a parent."""

    async def _run(
        parent_id: str, spec: dict[str, Any], after_id: str | None = None
    ) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import add_node_for_agent

        result = await add_node_for_agent(pocket_id, parent_id, spec, after_id)
        _capture_op(capture, "add_node", {"parent_id": parent_id, "after_id": after_id})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="add_node",
        description=(
            "Insert a new widget as a child of `parent_id`. Pass `spec` as "
            "a UINode object. Use `after_id` to position after a specific "
            "sibling; omit to append. Returns the new node with id assigned."
        ),
        args_schema=_AddNodeArgs,
    )


class _ReplaceNodeArgs(BaseModel):
    node_id: str
    spec: dict[str, Any]


def make_replace_node_tool(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> StructuredTool:
    """Replace a subtree."""

    async def _run(node_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import replace_node_for_agent

        result = await replace_node_for_agent(pocket_id, node_id, spec)
        _capture_op(capture, "replace_node", {"node_id": node_id})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="replace_node",
        description=(
            "Replace the subtree at `node_id` with `spec`. Preserves the "
            "target's id. Use for shape-changing edits (swap a stat for a "
            "chart); for prop-only tweaks prefer `set_node_prop`."
        ),
        args_schema=_ReplaceNodeArgs,
    )


class _MoveNodeArgs(BaseModel):
    node_id: str
    new_parent_id: str
    after_id: str | None = None


def make_move_node_tool(*, pocket_id: str, capture: dict[str, Any] | None = None) -> StructuredTool:
    """Move a subtree to a new parent / position."""

    async def _run(node_id: str, new_parent_id: str, after_id: str | None = None) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import move_node_for_agent

        result = await move_node_for_agent(pocket_id, node_id, new_parent_id, after_id)
        _capture_op(
            capture,
            "move_node",
            {"node_id": node_id, "new_parent_id": new_parent_id, "after_id": after_id},
        )
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="move_node",
        description=(
            "Move a subtree under a new parent. Same op handles "
            "reorder-within-parent and cross-parent moves. Refuses to "
            "move a node into itself or a descendant."
        ),
        args_schema=_MoveNodeArgs,
    )


class _RemoveNodeArgs(BaseModel):
    node_id: str


def make_remove_node_tool(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> StructuredTool:
    """Remove a subtree."""

    async def _run(node_id: str) -> dict[str, Any]:
        from ee.cloud.pockets.agent_context import remove_node_for_agent

        result = await remove_node_for_agent(pocket_id, node_id)
        _capture_op(capture, "remove_node", {"node_id": node_id})
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="remove_node",
        description=("Remove the subtree at `node_id`. Errors on the root."),
        args_schema=_RemoveNodeArgs,
    )


def make_edit_pocket_tools(
    *, pocket_id: str, capture: dict[str, Any] | None = None
) -> list[StructuredTool]:
    """Bundle the full edit-specialist tool set for one pocket.

    Order is the order the LLM sees them; we lead with the read tool
    so the agent is prompted toward "get then mutate" rather than
    blind writes.
    """
    return [
        make_get_pocket_tool(pocket_id=pocket_id),
        make_set_state_tool(pocket_id=pocket_id, capture=capture),
        make_append_state_tool(pocket_id=pocket_id, capture=capture),
        make_remove_state_tool(pocket_id=pocket_id, capture=capture),
        make_patch_state_tool(pocket_id=pocket_id, capture=capture),
        make_set_node_prop_tool(pocket_id=pocket_id, capture=capture),
        make_add_node_tool(pocket_id=pocket_id, capture=capture),
        make_replace_node_tool(pocket_id=pocket_id, capture=capture),
        make_move_node_tool(pocket_id=pocket_id, capture=capture),
        make_remove_node_tool(pocket_id=pocket_id, capture=capture),
    ]
