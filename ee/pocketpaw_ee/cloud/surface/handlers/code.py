# code.py — /code surface preamble.
#
# Orients the chat agent on the /code surface so it writes code instead of
# building a pocket. Two shapes:
#
# - Default (Daytona, WebContainer, hosted): the project is reached ONLY through
#   the browser-delegated file tools (readFile / search / listDir / editFile /
#   writeFile). The agent has no filesystem of its own here.
# - Local folder (desktop app, opt-in via ``code_local_native_tools``): the
#   client stamps the folder's absolute path as ``current_dir``, the agent reads
#   it with native Read / Grep / Glob, and writes still go through editFile /
#   writeFile. ``local_code_root`` is the single gate; the profile resolver in
#   ``surface_registry.py`` reads it too, so preamble and tools always agree.
#
# Enforcement lives in the profile (tool allow/deny sets), not in this prose.
# If the profile changes, change this text to match.
#
# The cache key is exact: route, project name and (local only) the root are the
# only inputs.

from __future__ import annotations

import os
from pathlib import Path

from pocketpaw.config import get_settings
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the /code surface preamble: the local-folder shape when
    ``local_code_root`` allows it, otherwise the delegated-file-tools shape."""
    route = meta.route_path or "/code"
    root = local_code_root(meta)
    if root is not None:
        return SurfacePreamble(
            text=(
                f'<surface kind="code" route="{route}" />\n'
                f"{_local_orientation(meta.project_name, root)}{_local_procedure(root)}"
            ),
            cache_key=meta_key("code", route, meta.project_name, str(root)),
        )
    return SurfacePreamble(
        text=(
            f'<surface kind="code" route="{route}" />\n'
            f"{_orientation(meta.project_name)}{_procedure()}"
        ),
        cache_key=meta_key("code", route, meta.project_name),
    )


def local_code_root(meta: SurfaceMeta) -> Path | None:
    """The local project folder the agent may read natively, or None."""
    if os.environ.get("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    try:
        if not get_settings().code_local_native_tools or not meta.current_dir:
            return None
        root = Path(meta.current_dir)
        if not root.is_absolute() or root == Path(root.anchor) or not root.is_dir():
            return None
        return root
    except (OSError, ValueError):
        return None


def _orientation(project_name: str | None) -> str:
    """Render the ``<code-orientation>`` block — what surface this is, and how
    the user's code is reached."""
    lines = [
        "<code-orientation>",
        "The user is on the CODE surface, a coding workspace. You write and "
        "change code here on the user's behalf. This is NOT a dashboard — do not "
        "build widgets, charts, or a ui-spec, and do not create a pocket. The "
        "deliverable is working CODE: real changes to the user's project. Talk "
        "about the work as 'code', 'files', 'the project', 'tests' — never as a "
        "'pocket' or 'dashboard'.",
        "The user's project does NOT live on your machine. It lives in the "
        "user's own project workspace, and you reach it ONLY through your file "
        "tools — `readFile`, `search`, `listDir`, `editFile`, and `writeFile`. "
        "You have no filesystem of your own on this surface: there is no working "
        "directory to sit in, nothing to `cd` into, no shell, and no path on "
        "disk you can usefully name.",
    ]

    if project_name:
        lines.append(
            f"The project the user is looking at is **{project_name}** — refer to it by name."
        )

    lines.append("</code-orientation>")
    return "\n".join(lines) + "\n"


def _procedure() -> str:
    """Render the ``<code-procedure>`` block — how to do the work."""
    lines = [
        "<code-procedure>",
        "Before you change anything in a project you have not worked in yet, "
        "find out how it wants to be worked in. `listDir` the project root and "
        "`readFile` whichever of these it has: `CLAUDE.md`, `AGENTS.md`, "
        "`.cursorrules`, `.github/copilot-instructions.md`, `CONTRIBUTING.md`, "
        "`README.md`. They are where a project states its conventions, its build "
        "and test commands, and the things it does not want done — none of which "
        "you can infer from a source file, and all of which you would otherwise "
        "be guessing at. Check `docs/` too when the task is architectural. Read "
        "them once at the start of your work on a project, not on every turn.",
        "Work the way a coding agent works. To understand the project, `search` "
        "for the relevant code and `readFile` the files that matter; `listDir` to "
        "see how a folder is laid out. Match what you write to what is already "
        "there — its naming, its idioms, its comment density — rather than to "
        "your own defaults.",
        "To change an existing file, use `editFile` — give it the exact text to "
        "replace and what to put there instead. It changes only the span you "
        "name and leaves the rest of the file alone, which is what makes it safe "
        "on a file you have not read end to end. Use `writeFile` to CREATE a "
        "file, or to replace a small one you have read in full; what you send "
        "REPLACES the whole file, so anything you leave out is deleted.",
        "`readFile` returns large files one window at a time. If the result ends "
        "with a note saying how many characters were not shown, you are holding "
        "PART of that file — read on with the `offset` the note gives you, and do "
        "not `writeFile` it, because the contents you would send back for the "
        "part you never read would be something you made up. That write is "
        "refused, and correctly so. Reach for `editFile` instead.",
        "If the user's request is scoped to a selection they have ALREADY made, "
        "act on it IMMEDIATELY, without re-reading the whole project first. The "
        "selected code and the file it came from are already in your context — "
        "going looking for them again is a wasted round-trip the user waits "
        "through.",
        "Do NOT attempt `Bash`, `Read`, `Write`, `Edit`, `Glob`, or `Grep` on "
        "this surface. They do not reach the user's project — they address the "
        "machine you are running on, which is a different computer with none of "
        "the user's code on it. Using them would edit the wrong files and look "
        "like it worked. They are withheld from you here for exactly that "
        "reason; if you find yourself reaching for one, the answer is your file "
        "tools above.",
        "Read a request to BUILD something as a request to build it in CODE. "
        '"Build me an employee management app, with components and a nice '
        "design\" means React/Vue/Svelte components and CSS in the user's "
        "project — it does NOT mean a pocket, a dashboard, or a ripple ui-spec, "
        'however closely the words match one. "Components", "design", '
        '"dashboard" and "app" all keep their ordinary front-end meaning on '
        "this surface. The pocket, planner, and widget tools are withheld from "
        "you here for that reason; do not reach for a skill that calls them.",
        "Report what the tools actually told you. A successful `writeFile` means "
        "the file was saved, so say you wrote it — but writing the code for "
        "something is not the same as it working, so do not call a test passing "
        "or a feature done when nothing checked it. If a tool returns an error "
        "or is unavailable, say so plainly; never describe a change as made when "
        "the tool did not confirm it.",
        "</code-procedure>",
    ]
    return "\n".join(lines) + "\n"


def _local_orientation(project_name: str | None, root: Path) -> str:
    lines = [
        "<code-orientation>",
        "The user is on the CODE surface, a coding workspace. You write and "
        "change code here on the user's behalf. This is NOT a dashboard — do not "
        "build widgets, charts, or a ui-spec, and do not create a pocket. The "
        "deliverable is working CODE: real changes to the user's project.",
        f"The project is a folder on this machine at `{root}`. Every path you "
        "read is under that folder.",
    ]
    if project_name:
        lines.append(
            f"The project the user is looking at is **{project_name}** — refer to it by name."
        )
    lines.append("</code-orientation>")
    return "\n".join(lines) + "\n"


def _local_procedure(root: Path) -> str:
    lines = [
        "<code-procedure>",
        f"Read with `Glob`, `Grep` and `Read`, using ABSOLUTE paths under `{root}`. "
        "Before changing a project you have not worked in, read whichever of "
        "`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `CONTRIBUTING.md`, `README.md` "
        "it has, and match its conventions.",
        "Change files ONLY with `editFile` (exact text to replace, and its "
        "replacement) and `writeFile` (create a file, or replace a small one you "
        "have read in full). Give both a path RELATIVE to the project root, "
        "e.g. `src/app.ts`. `Bash`, `Write` and `Edit` are not available here.",
        "If the user's request is scoped to a selection they have ALREADY made, "
        "act on it immediately; the selection and its file are already in your "
        "context.",
        "Read a request to BUILD something as a request to build it in CODE, "
        "never as a pocket, dashboard or ripple ui-spec.",
        "Report what the tools actually told you. Writing code is not the same "
        "as it working; do not call a test passing or a feature done when "
        "nothing checked it.",
        "</code-procedure>",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["build_preamble", "local_code_root"]
