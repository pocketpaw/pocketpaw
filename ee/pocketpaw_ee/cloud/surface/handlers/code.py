# code.py — /code surface preamble.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble`` keyed on the route plus ``meta.project_name``. Nothing
# mutable is read (the CD-3 rewrite is what made that true — the four storage
# hints it used to branch on are gone), so those two inputs ARE the preamble
# and the key is exact. The user's code changing does NOT move it, which is
# right: this block orients the agent toward the file tools, it never claimed
# to describe the project's contents.
#
# Created: 2026-06-10 (feat/studio-code-migration) — Orients the chat agent when
# the user is on the /code surface. Without it the surface falls back to GENERIC
# and the agent builds a dashboard pocket instead of writing code — the same
# drift the /sites preamble was created to fix. Static orientation — no live data
# to fake.
#
# Rewritten: 2026-07-22 (feat/code-surface-profile, CD-3) — the original
# preamble pointed the agent at the WRONG MACHINE, and did so silently.
#
# It branched on four ``SurfaceMeta`` hints (``workspace_vm`` /
# ``is_cloud_storage`` / ``current_dir`` / ``storage_root``) into three storage
# flavours: a Daytona-VM branch naming MCP tools (read_file / write_file / shell
# / run_python / sync_to_s3 / start_server / preview_url), an S3 branch pointing
# at ``/cloud/projects/{name}/files/*``, and a local-disk branch. All three were
# stale. The current /code page stamps NONE of those hints, so every real turn
# fell through to the local-disk branch and told the agent "your working
# directory is the workspace root" — which is the BACKEND SERVER's filesystem,
# not the user's project. The agent would read and write the server's disk and
# report success. Nothing routes to the main agent on /code yet, so the failure
# was latent rather than observed; this rewrite removes it before that lands.
# The Daytona MCP tools were equally stale — they address an older cloud-projects
# model that knows nothing of the current ``codeproject`` + ``CodeFileSession``
# runtime (nothing under ``cloud/daytona/`` so much as mentions either).
#
# The preamble states the single truth of this surface: the user's code is
# reachable ONLY through the file tools (``readFile`` / ``search`` / ``listDir`` /
# ``writeFile``), and the agent has no filesystem of its own here. Updated
# 2026-07-24 (feat/code-mode-file-tools): the surface used to expose ONE coarse
# ``code_mode`` tool that handed a task to a browser sub-agent; that sub-agent is
# gone and the MAIN agent now drives the work itself over these four per-call file
# tools. It also tells the agent to act IMMEDIATELY when the user's edit is scoped
# to a selection they already made — no re-reading the project first, since the
# selection plus its file are already in context.
#
# Changed: 2026-07-25 — ``writeFile`` SAVES. It used to stage a proposal behind a
# per-hunk review panel, and this preamble carried two paragraphs keeping the
# agent from claiming a write that had not happened. The gate is gone, so the
# claim is true and the paragraphs went with it. What replaced them is the
# warning that actually matters on a whole-file write: what you send replaces the
# file, so read it first.
#
# Changed: 2026-07-22 (fix/code-surface-denies-pocket-authoring) — the procedure
# block gained a paragraph on what "build an app" MEANS here. Reported from a
# live session: with a React project open, "Let's build an employee management
# app, with components, nice design etc" made the agent create a pocket and
# author a ripple ui-spec. The orientation already said "do not create a pocket"
# and lost anyway — the request's vocabulary ("app", "components", "design")
# matches the create-pocket skill more strongly than a blanket prohibition
# repels it. The new paragraph re-points those exact words at their ordinary
# front-end meaning instead of restating the ban. The ENFORCEMENT is the profile's
# widened deny set (``_CODE_POCKET_DENY``), which withholds the pocket / planner /
# widget tools; this prose exists so the agent knows why they are absent rather
# than discovering it as a tool error mid-turn.
#
# Changed: 2026-07-28 (fix/code-truncated-read-destroys-file) — two paragraphs,
# both against the same reported symptom: the agent "fabricating things from
# another session instead of reading the files".
#
# The first is the EDIT paragraph. This preamble used to say "to change the code,
# call `writeFile` with the file's COMPLETE new contents… so `readFile` before
# changing something you have not read this turn." On any file past the browser's
# 30_000-char read window that instruction cannot be followed — and the model
# followed it anyway, sending back the head with the tail reconstructed. The
# surface now leads with ``editFile`` and states plainly that a partial read
# forbids a whole-file write. The ENFORCEMENT is in the browser
# (``delegate.ts``/``lossyWriteRefusal``), as it must be: this text explains a
# refusal that already happens rather than requesting good behaviour, which is
# the same division of labour the profile note below describes.
#
# The second is the ORIENTATION paragraph. Nothing told the agent to read a
# project's own conventions, so it filled the gap from priors — which is the
# other half of what "fabricating" described. It now reads ``CLAUDE.md`` /
# ``AGENTS.md`` / ``.cursorrules`` / ``CONTRIBUTING.md`` / ``README.md`` before
# working in a project it does not know. Deliberately an instruction rather than
# an injection: a repo's ``CLAUDE.md`` can be tens of KB, and spending that on
# every turn to serve the first one is the wrong trade. Auto-stamping the file
# into ``SurfaceMeta`` at project open is the stronger version and is a follow-up.
#
# Mirrors the layout of handlers/sites.py and handlers/belt.py: an async
# ``build_preamble`` returning an XML-ish ``<surface>`` + ``<orientation>`` +
# ``<procedure>`` block.
#
# Enforcement lives in the profile, not in this prose — a preamble that merely
# ASKS the agent not to touch the disk is not a control. The /code
# ``SurfaceProfile`` (see ``surface_registry.py``) sets ``ripple_mode="off"`` so
# the agent doesn't inherit the ~20k-char "default to ui-spec" ripple LAW, DENIES
# the file/shell built-ins AND ``Agent`` outright, and scopes the MCP surface to
# the file tools (``_CODE_FILE_TOOL_IDS``). This text is the explanation the agent
# gets for a restriction already applied; the two must agree, so if the profile
# changes, change this too. The profile also carries NO skill: the `code` skill
# taught only the denied built-ins, and its edit→run→verify discipline now lives
# in this preamble and ``CODE_SYSTEM_PROMPT``, retargeted onto the file tools.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the /code surface preamble — code reached only via the file tools.

    Static: the preamble does not vary with storage flavour, working
    directory, or sandbox state, because none of those are the agent's
    concern on this surface. The file tools (readFile / search / listDir /
    writeFile) reach the project; the browser that runs them owns the sandbox
    and the file session. The only thing read from ``meta`` is ``project_name``,
    and purely so the agent can name the project the user is looking at.

    That makes the cache key exact rather than a digest: the route and the
    project name are the only two inputs, so naming them names the preamble.
    Switching projects moves the key; the user editing their code does not,
    which is correct — the file tools read the project live and this block
    never claimed to describe its contents.
    """
    route = meta.route_path or "/code"
    return SurfacePreamble(
        text=(
            f'<surface kind="code" route="{route}" />\n'
            f"{_orientation(meta.project_name)}{_procedure()}"
        ),
        cache_key=meta_key("code", route, meta.project_name),
    )


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


__all__ = ["build_preamble"]
