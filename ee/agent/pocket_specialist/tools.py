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


def make_validate_spec_tool() -> StructuredTool:
    """Build a ``validate_spec`` tool that checks a draft rippleSpec
    against the live widget manifest.

    Returns ``{"ok": bool, "warnings": [str, ...]}``. If the manifest is
    unavailable (offline, fetch error), the tool returns ``ok=True`` with
    an empty warnings list — best-effort, never block the user.
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
            return {"ok": True, "warnings": []}
        issues = validate_against_manifest(spec, manifest, apply_aliases=True)
        warnings = [_format_issue(issue) for issue in issues]
        return {"ok": len(warnings) == 0, "warnings": warnings}

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


def make_persist_pocket_tool(*, workspace_id: str, user_id: str) -> StructuredTool:
    """Build a ``persist_pocket`` tool bound to the given workspace/user.

    Creates a new pocket when ``target_pocket_id`` is None; updates an
    existing pocket otherwise. Returns the pocket view dict on success.
    Raises ``RuntimeError`` on persist failure (the runtime catches and
    surfaces the error to the agent).
    """

    async def _run(
        ripple_spec: dict[str, Any],
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        target_pocket_id: str | None = None,
    ) -> dict[str, Any]:
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
            return view
        view, _pocket_id, err = await _agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name=name or "Untitled pocket",
            description=description or "",
            icon=icon or "",
            color=color or "",
            ripple_spec=ripple_spec,
        )
        if err is not None or view is None:
            raise RuntimeError(f"persist failed: {err or 'create returned no view'}")
        return view

    return StructuredTool.from_function(
        coroutine=_run,
        name="persist_pocket",
        description=(
            "Persist the rippleSpec as a new pocket OR update an existing one. "
            "Pass target_pocket_id to update; omit to create. You MUST call this "
            "exactly once before returning. Returns {id, name, description, "
            "type, icon, color}."
        ),
        args_schema=_PersistPocketArgs,
    )
