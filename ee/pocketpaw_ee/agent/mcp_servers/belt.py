# belt.py — in-process MCP server exposing the Belt & Pulley develop-station
# code-change gate to the claude_agent_sdk cloud chat backend. Created:
# 2026-06-10 (feat/belt-gate, BS-3).
#
# What this file does: clones the media.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` / ``current_session_mongo_id``
# accessors in ``ee.cloud.chat.agent_service`` the media / sites servers read),
# and the ``_error_response`` / ``_success_response`` helpers. The single tool
# id namespaces as ``mcp__pocketpaw_belt__belt_propose_change`` so the Claude
# Code allowlist machinery matches it (a sibling PR hardcodes this exact id).
#
# One SDK @tool def:
#   * belt_propose_change — the develop station agent produces a unified diff
#     and proposes it THROUGH Instinct (the human approve/reject layer, "sudo
#     for agents"). This tool does NOT apply anything: it validates the inputs
#     (identity present; diff non-empty and under the size cap; repo resolves to
#     an existing git repo INSIDE the settings-driven allowlist) and then files
#     an Instinct Action carrying a ``_code_change`` blob. A human approves it
#     in The Tray; the apply-on-approve executor (ee/cloud/belt/executor.py)
#     then applies the diff in a FRESH worktree, commits, pushes, and opens a
#     PR. The captain still merges on GitHub — Instinct is the MID gate, never
#     an auto-merge.
#
# Contract pins (a sibling PR hardcodes these — do not deviate):
#   * server name ``pocketpaw_belt``, tool ``belt_propose_change``
#   * input {repo, base_branch, diff, summary, task, orient_ref?}
#   * Action kind ``code_change`` (stored as the blob's ``kind`` discriminator
#     under ``Action.parameters._code_change`` — the Action model has no literal
#     ``kind`` column; the pocket-write bridge uses the same parameters-key
#     discriminator pattern).
#
# Security: the diff is DATA — it is stored verbatim in the Action blob and only
# ever written to a temp file + fed to ``git apply`` by the executor, never
# echoed/eval'd. The repo path is allowlist-gated here at propose time AND
# re-resolved at execute time (defense in depth). NO phantom successes: the tool
# returns ok only after ``store.propose`` confirms the Action is durably stored.
# Diff content is never logged — only action ids + changed-line counts.
#
# EE→OSS boundary: this module lives in pocketpaw_ee; the surface service loads
# BELT_TOOL_IDS as a plain frozenset[str] inside a try/except (never importing a
# pocketpaw_ee symbol into src/pocketpaw).
"""Agent-side MCP surface for the Belt & Pulley code-change gate."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_belt"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form. A sibling PR hardcodes
# ``mcp__pocketpaw_belt__belt_propose_change`` — keep it stable.
PROPOSE_CHANGE_TOOL_ID = f"mcp__{SERVER_NAME}__belt_propose_change"

BELT_TOOL_IDS = (PROPOSE_CHANGE_TOOL_ID,)

# The Instinct Action kind discriminator for a Belt code-change proposal. The
# executor + the router dispatch on the presence of this key under
# ``Action.parameters``; the blob also carries ``kind="code_change"`` for
# readers that introspect the blob directly.
CODE_CHANGE_KIND = "code_change"
# The parameters key under which the code-change blob rides — mirrors the
# pocket-write bridge's ``_pocket_write`` key. The router + executor dispatch on
# this key being present.
CODE_CHANGE_PARAM_KEY = "_code_change"

# Diff size cap. A proposal over EITHER bound is refused with a "split the task"
# error — a diff this large is a sign the station task wasn't decomposed, and a
# huge blob in Mongo + a sprawling PR defeats the human-review gate. Counts
# ADDED/REMOVED lines (``+``/``-`` lines, excluding the ``+++``/``---`` file
# headers), not context lines.
MAX_CHANGED_LINES = 1500
MAX_DIFF_BYTES = 200 * 1024  # 200 KB


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None, str | None]:
    """Resolve the active workspace + user + session id from the per-stream
    ContextVars set by the cloud chat agent runtime. Returns
    ``(workspace_id, user_id, session_mongo_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            current_user_id,
            current_workspace_id,
        )

        return current_workspace_id(), current_user_id(), current_session_mongo_id()
    except Exception:  # noqa: BLE001
        return None, None, None


def _count_changed_lines(diff: str) -> int:
    """Count added/removed lines in a unified diff.

    A changed line starts with a single ``+`` or ``-`` but is NOT a file header
    (``+++ ``/``--- ``). Context lines (leading space) and hunk headers (``@@``)
    don't count. This is a heuristic line budget, not a parser — it only needs
    to flag a task that should have been split.
    """
    count = 0
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def _resolve_allowlist() -> list[Path]:
    """Return the resolved allowlist roots a repo path must live under.

    Settings-driven (``belt_repo_allowlist``). When empty, default to the
    current working directory's PARENT — the workspace root that holds the
    project checkouts — so a stock deployment still gates writes to the
    workspace tree rather than the whole filesystem. Each root is resolved
    (symlinks + ``..`` collapsed) so the containment check below can't be
    defeated by a ``..`` traversal.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    roots: list[Path] = []
    for raw in settings.belt_repo_allowlist or []:
        try:
            roots.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            logger.warning("belt: skipping unresolvable allowlist root %r", raw)
    if not roots:
        # Default: the parent of cwd (the workspace root holding the checkouts).
        roots.append(Path.cwd().resolve().parent)
    return roots


def _is_within_allowlist(repo_path: Path, roots: list[Path]) -> bool:
    """True when ``repo_path`` is inside (or equal to) one of the allowlist
    roots. ``repo_path`` MUST already be resolved by the caller."""
    for root in roots:
        try:
            repo_path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_repo(repo: str) -> tuple[Path | None, str | None]:
    """Resolve a proposal's ``repo`` field to an existing, allowlisted git repo.

    ``repo`` is an absolute path or a registered name. (Registered-name
    resolution is a follow-up — BS-3 ships the absolute-path form; a name that
    isn't an existing path is refused with a clear error.) Returns
    ``(resolved_path, None)`` on success or ``(None, error)`` on any refusal:
    a non-existent path, a path that isn't a git repo, or a path outside the
    allowlist. The allowlist check runs on the RESOLVED path so a ``..``
    traversal can't escape the boundary.
    """
    if not repo or not isinstance(repo, str):
        return None, "`repo` must be a non-empty absolute path (or registered name)."

    try:
        candidate = Path(repo).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"could not resolve repo path {repo!r}: {exc}"

    roots = _resolve_allowlist()
    # Allowlist FIRST — never leak whether an out-of-bounds path exists.
    if not _is_within_allowlist(candidate, roots):
        return None, (
            f"repo path {repo!r} is outside the allowed roots "
            f"({', '.join(str(r) for r in roots)}). Set "
            "POCKETPAW_BELT_REPO_ALLOWLIST to authorize a new root."
        )

    if not candidate.is_dir():
        return None, f"repo path {repo!r} does not exist or is not a directory."

    if not (candidate / ".git").exists():
        return None, f"repo path {repo!r} is not a git repository (no .git)."

    return candidate, None


async def _propose_change_handler(args: dict) -> dict:
    """MCP handler for ``belt__belt_propose_change``.

    Validates identity + inputs, then files an Instinct Action carrying the
    ``_code_change`` blob. Returns ``{ok, action_id, tray_hint}`` on success,
    or an ``is_error`` response with the reason. NO phantom successes: ok is
    returned only after ``store.propose`` confirms the Action is stored.
    """
    workspace_id, user_id, session_mongo_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "belt_propose_change requires workspace and user context "
            "(call from a cloud chat session)."
        )

    repo = args.get("repo")
    base_branch = args.get("base_branch")
    diff = args.get("diff")
    summary = args.get("summary")
    task = args.get("task")
    orient_ref = args.get("orient_ref")

    if not isinstance(base_branch, str) or not base_branch.strip():
        return _error_response("belt_propose_change requires a non-empty `base_branch`.")
    if not isinstance(summary, str) or not summary.strip():
        return _error_response(
            "belt_propose_change requires a non-empty `summary` (used as the commit body)."
        )
    if not isinstance(task, str) or not task.strip():
        return _error_response(
            "belt_propose_change requires the original `task` text the station was given."
        )

    # The diff is the payload — validate it hard before anything touches a store.
    if not isinstance(diff, str) or not diff.strip():
        return _error_response("belt_propose_change requires a non-empty unified `diff`.")

    diff_bytes = len(diff.encode("utf-8"))
    changed_lines = _count_changed_lines(diff)
    if changed_lines > MAX_CHANGED_LINES or diff_bytes > MAX_DIFF_BYTES:
        return _error_response(
            f"diff too large to gate ({changed_lines} changed lines, "
            f"{diff_bytes} bytes; cap is {MAX_CHANGED_LINES} lines / "
            f"{MAX_DIFF_BYTES} bytes). Split the task into smaller, "
            "independently reviewable changes and propose each separately."
        )

    repo_path, repo_err = _resolve_repo(repo if isinstance(repo, str) else "")
    if repo_err is not None or repo_path is None:
        return _error_response(repo_err or "could not resolve the repo path.")

    orient_clean = (
        orient_ref.strip() if isinstance(orient_ref, str) and orient_ref.strip() else None
    )

    # Build the code-change blob. ``kind`` is the discriminator the executor /
    # router dispatch on (the Action model has no literal kind column). The diff
    # rides verbatim — it is DATA, never interpolated into a shell.
    blob: dict[str, Any] = {
        "kind": CODE_CHANGE_KIND,
        "schema": 1,
        "repo": str(repo_path),
        "base_branch": base_branch.strip(),
        "diff": diff,
        "summary": summary.strip(),
        "task": task.strip(),
        "orient_ref": orient_clean,
        "workspace_id": workspace_id,
        "requested_by": user_id,
        # The proposing chat session — lets the executor / audit tie the PR back
        # to the conversation that produced it.
        "session_id": session_mongo_id,
    }

    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    # A short, content-free title/recommendation for The Tray. NEVER put diff
    # content in the title — only the summary (the agent's own 1-3 sentence
    # description) and the repo/branch.
    repo_name = repo_path.name
    title = f"Code change — {repo_name} ({base_branch.strip()})"
    recommendation = (
        f"Approve to apply this diff to {repo_name} on a new branch off "
        f"{base_branch.strip()} and open a PR. {changed_lines} changed lines. "
        f"Summary: {summary.strip()}"
    )

    trigger = ActionTrigger(
        type="agent",
        source="belt:develop",
        reason="code change proposed by the develop station — requires human approval",
    )

    store = get_instinct_store()
    try:
        action = await store.propose(
            # ``pocket_id`` carries the workspace for Belt actions — they aren't
            # bound to a pocket the way Mission Control items are. The workspace
            # also rides on the blob (the executor's tenancy gate reads it
            # there); pocket_id mirrors it so the existing per-pocket queries
            # still surface the row.
            pocket_id=workspace_id,
            title=title,
            description=recommendation,
            recommendation=recommendation,
            trigger=trigger,
            category=ActionCategory.EXTERNAL,
            priority=ActionPriority.HIGH,
            parameters={CODE_CHANGE_PARAM_KEY: blob},
            assignee=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("belt: propose raised (no Action stored)", exc_info=True)
        return _error_response(f"could not propose the change: {exc}")

    # Confirm the Action is durably readable before claiming success — NO
    # phantom success. ``propose`` returns the in-memory Action; a fetch proves
    # the INSERT committed.
    stored = await store.get_action(action.id)
    if stored is None:
        return _error_response("the change was not stored — please retry.")

    logger.info(
        "belt: proposed code_change action %s (repo=%s, base=%s, changed_lines=%d)",
        action.id,
        repo_name,
        base_branch.strip(),
        changed_lines,
    )

    return _success_response(
        {
            "ok": True,
            "action_id": action.id,
            "tray_hint": (
                "Proposed for approval in The Tray. A human must approve before "
                "the diff is applied and a PR is opened. Do not claim the change "
                "is merged — Instinct is the mid gate; the captain merges on "
                "GitHub."
            ),
        }
    )


def build_belt_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the Belt code-change gate, or
    return ``None`` if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_media_server`` (``(name, server)`` or
    ``None``) so the backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_belt MCP disabled")
        return None

    @tool(
        "belt_propose_change",
        (
            "Propose a CODE CHANGE for human approval (the Belt & Pulley develop "
            "station). Call this when you have produced a unified diff that should "
            "be applied to a repo. This does NOT apply the diff or merge anything: "
            "it files the change in The Tray for a human to approve or reject. On "
            "approval, the change is applied to a fresh branch and a PR is opened "
            "— the captain merges on GitHub. Args: `repo` (absolute path or "
            "registered name of the git repo), `base_branch` (the branch to base "
            "off, e.g. 'main'), `diff` (the full unified diff), `summary` (1-3 "
            "sentences describing the change — used as the commit message body), "
            "`task` (the original station task text), `orient_ref` (optional brief "
            "reference to the orientation you used). Returns {ok, action_id, "
            "tray_hint}. ok=false with an error means relay the reason (e.g. the "
            "diff is too large — split the task) — do NOT claim the change landed."
        ),
        {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Absolute path (or registered name) of the target git repo.",
                },
                "base_branch": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Branch to base the change off (e.g. 'main').",
                },
                "diff": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The full unified diff to apply.",
                },
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "1-3 sentence description; used as the commit message body.",
                },
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The original station task text.",
                },
                "orient_ref": {
                    "type": "string",
                    "description": "Optional brief reference to the orientation used.",
                },
            },
            "required": ["repo", "base_branch", "diff", "summary", "task"],
            "additionalProperties": False,
        },
    )
    async def belt_propose_change(args):  # type: ignore[no-untyped-def]
        return await _propose_change_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[belt_propose_change],
    )
    return SERVER_NAME, server


__all__ = [
    "BELT_TOOL_IDS",
    "CODE_CHANGE_KIND",
    "CODE_CHANGE_PARAM_KEY",
    "PROPOSE_CHANGE_TOOL_ID",
    "SERVER_NAME",
    "build_belt_server",
]
