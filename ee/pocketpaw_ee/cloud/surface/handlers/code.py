# code.py — /code surface preamble.
#
# Created: 2026-06-10 (feat/studio-code-migration) — Orients the chat agent when
# the user is on the /code surface (the agent edits + runs code in the
# workspace). Without it the surface falls back to GENERIC and the agent builds a
# dashboard pocket instead of editing code — the same drift the /sites preamble
# was created to fix. Static orientation — no live data to fake.
#
# Mirrors the layout of handlers/sites.py: an async ``build_preamble`` returning
# an XML-ish ``<surface>`` + ``<orientation>`` + ``<procedure>`` block. The
# procedure tells the agent to use the SDK built-in tools (Bash / Read / Write /
# Edit / Glob / Grep), PREFER the bundled ``code`` skill (the edit→run→verify
# loop), and VERIFY (run it / tests) before claiming done. The workspace is
# jailed and destructive shell is blocked.
#
# The /code SurfaceProfile sets ``ripple_mode="off"`` and an
# ``allowed_sdk_tools`` allowlist (see service.py) so the agent does not inherit
# the ~20k-char "default to ui-spec" ripple LAW and build a dashboard instead of
# editing code.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /code surface preamble — edit + run code in the workspace."""
    route = meta.route_path or "/code"
    return (
        f'<surface kind="code" route="{route}" />\n'
        "<code-orientation>\n"
        "The user is on the CODE surface, a coding workspace. You EDIT and RUN "
        "code here on the user's behalf. This is NOT a dashboard — do not build "
        "widgets, charts, or a ui-spec, and do not create a pocket. The "
        "deliverable is working CODE: files written or changed in the workspace "
        "and verified by running them. Talk about the work as 'code', 'files', "
        "'the workspace', 'tests' — never as a 'pocket' or 'dashboard'.\n"
        "</code-orientation>\n"
        "<code-procedure>\n"
        "Treat the user's message on this surface as a coding task. Use the "
        "built-in tools to do the work: `Bash` to run commands, `Read` to read "
        "files, `Write` / `Edit` to change them, and `Glob` / `Grep` to find "
        "code. PREFER the `code` skill — invoke it by intent (no slash command "
        "needed); it owns the edit→run→verify loop. Always VERIFY before "
        "claiming the task is done: run the code or its tests and confirm the "
        "output, rather than asserting success from the edit alone. If a command "
        "fails, read the error, fix it, and re-run.\n"
        "The workspace is JAILED — stay inside it; do not reach outside the "
        "working directory. Destructive shell operations are blocked, so do not "
        "attempt to delete or move large trees, format disks, or run anything "
        "irreversible. When done, briefly summarize what changed and how you "
        "verified it.\n"
        "</code-procedure>"
    )


__all__ = ["build_preamble"]
