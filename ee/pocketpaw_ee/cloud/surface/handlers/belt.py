# belt.py — /belt surface preamble (the develop station).
#
# Created: 2026-06-10 (feat/belt-surface, BS-2 Belt & Pulley stations thin
# slice) — Orients the chat agent when the user is on the /belt surface, the
# develop station of the Belt & Pulley assembly line. The agent takes a coding
# task, GROUNDS itself with loom's orient() first, implements in a station
# worktree, and PROPOSES the resulting diff through the Instinct gate — it never
# applies changes directly and never claims success unless the gate tool returned
# ok. Without this preamble the surface falls back to GENERIC and the agent
# builds a dashboard pocket instead of running the station loop.
#
# Updated: 2026-06-10 (feat/belt-console-backend, SC-1) — ``build_preamble`` now
# consumes ``meta.repo`` + ``meta.base_branch`` (the repo + branch the /belt page
# bound for this run). When BOTH are present it injects a "Your repo / base
# branch" line and instructs the agent NOT to re-ask for the repo, and to pass
# exactly those values into ``belt_propose_change``. When either is absent the
# preamble keeps today's ask-first behavior (the agent must confirm the repo +
# base branch with the user before proposing). This is the fix for the first
# live runs where the agent had no repo and asked for it every time.
#
# Mirrors handlers/code.py: an async ``build_preamble`` returning an XML-ish
# ``<surface>`` + ``<orientation>`` + ``<procedure>`` block. The procedure
# enforces the three-stage station loop:
#   * ORIENT FIRST — call ``mcp__loom__orient`` with the task before reading any
#     code; drill down with locate / why / what_depends_on as needed.
#   * DEVELOP — implement in the station worktree with Bash/Read/Write/Edit/Glob/
#     Grep; run targeted tests; keep the diff small (large changes → split).
#   * PROPOSE VIA THE GATE — produce a clean unified diff and call
#     ``mcp__pocketpaw_belt__belt_propose_change``; NEVER apply to the user's
#     branches, NEVER push or merge; if the gate is unavailable/errors, say so —
#     no phantom successes.
#
# The /belt SurfaceProfile sets ``ripple_mode="off"`` (so the agent doesn't
# inherit the ~20k-char "default to ui-spec" ripple LAW and build a dashboard)
# and scopes ``allow_mcp_tool_ids`` to the loom orientation tools + the gate
# tool (see service.py).

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


def _repo_binding(meta: SurfaceMeta) -> tuple[str, str]:
    """Resolve the repo-binding block + the propose-call instruction from meta.

    Returns ``(binding_block, propose_instruction)``:
      * When BOTH ``meta.repo`` and ``meta.base_branch`` are present, the page
        has bound a repo for this run — the binding block states them and the
        propose instruction tells the agent to reuse them verbatim and NOT ask.
      * Otherwise (the ask-first path), the binding block is empty and the
        propose instruction tells the agent to confirm the repo + base branch
        with the user before proposing.
    """
    repo = (meta.repo or "").strip()
    base_branch = (meta.base_branch or "").strip()
    if repo and base_branch:
        binding = (
            f"<belt-repo>\n"
            f"Your repo: {repo} · base branch: {base_branch}\n"
            "This run is already bound to that repo and base branch — do NOT ask "
            "the user which repo to work on. Orient and develop against this repo, "
            "and when you propose, pass EXACTLY this repo path and base branch "
            "into `belt_propose_change` (repo=the path above, base_branch=the "
            "branch above). If the user explicitly names a different repo in their "
            "message, follow the message; otherwise this binding is the repo.\n"
            "</belt-repo>\n"
        )
        propose = (
            f"call `mcp__pocketpaw_belt__belt_propose_change` with `{{repo: "
            f'"{repo}", base_branch: "{base_branch}", diff, summary, task}}` — '
            "reuse the bound repo + base branch above verbatim. "
        )
        return binding, propose
    # Ask-first path — no repo bound for this run.
    propose = (
        "first CONFIRM which repo and base branch to target (ask the user if you "
        "don't already know it from the conversation), then call "
        "`mcp__pocketpaw_belt__belt_propose_change` with `{repo, base_branch, "
        "diff, summary, task}`. "
    )
    return "", propose


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /belt surface preamble — the develop station loop."""
    route = meta.route_path or "/belt"
    repo_binding, propose_instruction = _repo_binding(meta)
    return (
        f'<surface kind="belt" route="{route}" />\n'
        f"{repo_binding}"
        "<belt-orientation>\n"
        "The user is on the BELT surface, the develop station of the Belt & "
        "Pulley assembly line. You take a coding task, GROUND yourself in the "
        "codebase, implement the change in a station worktree, and PROPOSE the "
        "resulting diff through the Instinct gate for a human to review. This is "
        "NOT a dashboard — do not build widgets, charts, or a ui-spec, and do "
        "not create a pocket. The deliverable is a PROPOSED CHANGE: a clean "
        "unified diff handed to the gate, waiting in the Tray for human "
        "approval. You NEVER apply changes to the user's branches directly, and "
        "you NEVER claim the change was proposed unless the gate tool actually "
        "returned ok. Talk about the work as 'the change', 'the diff', 'the "
        "station', 'the gate', 'the Tray' — never as a 'pocket' or 'dashboard'.\n"
        "</belt-orientation>\n"
        "<belt-procedure>\n"
        "Treat the user's message on this surface as a coding task and run the "
        "station loop in three stages — in order.\n"
        "1. ORIENT FIRST. Before reading any code, call `mcp__loom__orient` with "
        "the user's task. Use the returned brief — scope, blast-radius, "
        "entrypoints — to plan your approach. Drill down with `mcp__loom__locate` "
        "(find where something lives), `mcp__loom__why` (understand a decision), "
        "and `mcp__loom__what_depends_on` (find blast radius) as needed. Do NOT "
        "start reading or editing code until you have oriented.\n"
        "2. DEVELOP. Implement the change in the station worktree using the "
        "built-in tools: `Bash` to run commands, `Read` to read files, `Write` / "
        "`Edit` to change them, and `Glob` / `Grep` to find code. Run TARGETED "
        "tests for what you touched. Keep the diff SMALL and focused — one task, "
        "one change. If the task genuinely needs a large change, tell the user to "
        "split it into smaller tasks rather than proposing a sprawling diff.\n"
        "3. PROPOSE VIA THE GATE. Produce a clean unified diff of your change and "
        f"{propose_instruction}This is the ONLY way a change leaves "
        "the station. NEVER apply your change to the user's branches directly, "
        "NEVER `git push`, NEVER `git merge`. If the gate tool is unavailable or "
        "returns an error, say so PLAINLY — do NOT claim the change was proposed "
        "(no phantom successes). After the gate accepts the proposal, tell the "
        "user the change is waiting in the Tray for review, and that on approve it "
        "is applied in a worktree, branched, and opened as a PR.\n"
        "The workspace is JAILED — stay inside the station worktree; do not reach "
        "outside it. Destructive shell operations are blocked.\n"
        "</belt-procedure>"
    )


__all__ = ["build_preamble"]
