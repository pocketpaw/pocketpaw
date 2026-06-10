# belt.py — in-process MCP server exposing the Belt & Pulley develop-station
# code-change gate to the claude_agent_sdk cloud chat backend. Created:
# 2026-06-10 (feat/belt-gate, BS-3).
#
# Updated: 2026-06-10 (feat/belt-trace, BS-4 — Decision-Graph chain) —
#   ``belt_propose_change`` now MINTS a Decision-Graph ``correlation_id``
#   at propose time and emits the chain-opening ``agent.proposed`` event
#   through ``journal_writer.record_agent_proposed`` (RFC 09). The
#   correlation_id is stamped onto the ``_code_change`` blob (schema 2,
#   mirroring the pocket-write bridge's ``_pocket_write.correlation_id``)
#   so the Instinct router's approve / reject paths and the executor can
#   close the SAME chain — one station run = one Decision chain. The
#   emitted ``agent.proposed`` event id is stashed on the blob as
#   ``proposed_event_id`` so the eventual ``human.corrected`` event can
#   cite it as its ``causation_id``. Both the emit and the blob fields are
#   best-effort: a Decision-Graph wiring failure must never fail the
#   propose response (the Action is already durable; the Slice 4 reconciler
#   is the safety net). The blob also carries the workspace/user on the
#   chain actor + scope so visibility filters narrow correctly.
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
from uuid import UUID, uuid4

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

# Schema version stamped on the ``_code_change`` blob.
#   * schema 1 (BS-3): repo / base_branch / diff / summary / task + workspace +
#     requester context, NO Decision-Graph chain.
#   * schema 2 (BS-4, RFC 09): adds ``correlation_id`` (the chain id minted here
#     at propose time, when ``agent.proposed`` fires) and ``proposed_event_id``
#     (the id of that ``agent.proposed`` event, so the eventual
#     ``human.corrected`` can chain its ``causation_id`` back to it). The
#     executor's schema-mismatch guard fails a stale schema-1 blob approved
#     post-deploy loud (same discipline as the pocket-write bridge).
CODE_CHANGE_SCHEMA = 2

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


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    repo_name: str,
    base_branch: str,
    summary: str,
    task: str,
    changed_lines: int,
    workspace_id: str,
    user_id: str,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a Belt code change.

    Mirrors ``action_executor``'s ``agent.proposed`` emit for pocket writes:
    the develop station is the actor that PROPOSED the change, so the chain
    actor is ``kind="agent"`` with the requesting user on its id and the
    workspace on its scope_context. The Belt action isn't bound to a pocket —
    its tenancy is the workspace — so ``pocket_id`` on the chain carries the
    workspace id (matching how the Action's ``pocket_id`` field carries the
    workspace, see ``_propose_change_handler``). The projection's
    ``_fold_proposed`` reads ``intent`` / ``action`` / ``pocket_id`` /
    ``inputs`` off the payload.

    Returns the emitted event id (``UUID``) so the caller can persist it on the
    blob's ``proposed_event_id`` field for the ``human.corrected`` causation
    chain, or ``None`` when the emit raised — best-effort per RFC 09; the Slice
    4 reconciler / abandon-sweeper picks up any orphans.
    """
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    intent = f"code change to {repo_name} ({base_branch}) — {changed_lines} changed lines"
    payload: dict[str, Any] = {
        # Fields the projection's ``_fold_proposed`` consumes.
        "intent": intent,
        "action": "code_change",
        "pocket_id": workspace_id,
        "inputs": [],
        # Richer fields for the explain narrator / a future swap to
        # soul-protocol's ``build_proposal_event(AgentProposal(...))``.
        "proposal_kind": "code_change",
        "summary": summary,
        "proposal": {
            "repo": repo_name,
            "base_branch": base_branch,
            "task": task,
            "changed_lines": changed_lines,
        },
        "action_id": action_id,
    }
    try:
        entry = record_agent_proposed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
        )
        return entry.id
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "belt agent.proposed emit failed for correlation_id=%s (action_id=%s) "
            "— Slice 4 reconciler will catch up",
            correlation_id,
            action_id,
            exc_info=True,
        )
        return None


async def _persist_chain_ids(
    *,
    store: Any,
    action_id: str,
    correlation_id: str,
    proposed_event_id: str | None,
) -> None:
    """Write ``correlation_id`` + ``proposed_event_id`` onto the persisted
    Action's ``parameters._code_change`` blob after ``agent.proposed`` fired.

    The blob is built with these fields already set from the in-memory values,
    so this re-write only matters when the proposed event id was unknown at
    propose-build time. We mint the correlation_id BEFORE building the blob, so
    that field is already correct on the stored row; ``proposed_event_id`` is
    the one this back-write fills in. Direct SQL update — same pattern as the
    pocket-write bridge's ``_persist_parked_policy_event_id``. Best-effort:
    failure leaves ``proposed_event_id`` None and the eventual
    ``human.corrected`` emits without a causation_id (the chain still folds;
    causation_id is optional on EventEntry).
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(CODE_CHANGE_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["correlation_id"] = correlation_id
        blob["proposed_event_id"] = proposed_event_id
        params[CODE_CHANGE_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "belt: failed to persist chain ids onto action %s — the chain's "
            "human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )


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

    # BS-4 — mint the Decision-Graph chain correlation_id BEFORE building the
    # blob so the stored Action carries it from the first write. The same id
    # threads through approve / reject (router) and apply (executor) so the
    # whole station run folds into ONE Decision chain.
    correlation_id = uuid4()

    # Build the code-change blob. ``kind`` is the discriminator the executor /
    # router dispatch on (the Action model has no literal kind column). The diff
    # rides verbatim — it is DATA, never interpolated into a shell.
    #
    # Schema 2 (BS-4, RFC 09) — carries the chain ``correlation_id`` and
    # ``proposed_event_id`` (the latter back-written after ``agent.proposed``
    # fires; None here, filled by ``_persist_chain_ids`` below).
    blob: dict[str, Any] = {
        "kind": CODE_CHANGE_KIND,
        "schema": CODE_CHANGE_SCHEMA,
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
        # RFC 09 chain-correlation fields (schema 2).
        "correlation_id": str(correlation_id),
        "proposed_event_id": None,
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

    # BS-4 — open the Decision-Graph chain now that the Action is durable.
    # ``agent.proposed`` is the chain origin; its event id is back-written onto
    # the blob so the router's ``human.corrected`` can cite it as causation.
    # Best-effort: a Decision-Graph wiring failure must NOT fail the propose
    # response — the Action is already stored; the Slice 4 reconciler closes
    # any chain that never opened.
    proposed_event_id = _emit_agent_proposed(
        correlation_id=correlation_id,
        action_id=action.id,
        repo_name=repo_name,
        base_branch=base_branch.strip(),
        summary=summary.strip(),
        task=task.strip(),
        changed_lines=changed_lines,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    if proposed_event_id is not None:
        await _persist_chain_ids(
            store=store,
            action_id=action.id,
            correlation_id=str(correlation_id),
            proposed_event_id=str(proposed_event_id),
        )

    logger.info(
        "belt: proposed code_change action %s (repo=%s, base=%s, changed_lines=%d, "
        "correlation_id=%s)",
        action.id,
        repo_name,
        base_branch.strip(),
        changed_lines,
        correlation_id,
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
    "CODE_CHANGE_SCHEMA",
    "PROPOSE_CHANGE_TOOL_ID",
    "SERVER_NAME",
    "build_belt_server",
]
