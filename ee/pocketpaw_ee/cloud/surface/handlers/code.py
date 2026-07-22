# code.py — /code surface preamble.
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
# The preamble now states the single truth of this surface: the user's code is
# reachable ONLY through the ``code_mode`` tool, and the agent has no filesystem
# of its own here. It also tells the agent to call ``code_mode`` IMMEDIATELY when
# the user's edit is scoped to a selection they already made — no exploratory
# retrieval first. That is a latency mitigation, not a style note: the /code path
# is two model calls deep (chat agent → code agent), so a redundant retrieval
# round is paid twice, and the selection plus its file are already in context.
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
# Mirrors the layout of handlers/sites.py and handlers/belt.py: an async
# ``build_preamble`` returning an XML-ish ``<surface>`` + ``<orientation>`` +
# ``<procedure>`` block.
#
# Enforcement lives in the profile, not in this prose — a preamble that merely
# ASKS the agent not to touch the disk is not a control. The /code
# ``SurfaceProfile`` (see ``surface_registry.py``) sets ``ripple_mode="off"`` so
# the agent doesn't inherit the ~20k-char "default to ui-spec" ripple LAW, DENIES
# the file/shell built-ins AND ``Agent`` outright, and scopes the MCP surface to
# ``code_mode``. This text is the explanation the agent gets for a restriction
# already applied; the two must agree, so if the profile changes, change this
# too. The profile also carries NO skill: the `code` skill teaches only the
# denied built-ins and is retargeted onto ``code_mode`` in CD-2, so until then
# this preamble is the whole of the agent's guidance on this surface.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /code surface preamble — code reached only via ``code_mode``.

    Static: the preamble does not vary with storage flavour, working
    directory, or sandbox state, because none of those are the agent's
    concern on this surface. The ``code_mode`` tool owns the project — it
    resolves the sandbox and the file session itself. The only thing read
    from ``meta`` is ``project_name``, and purely so the agent can name the
    project the user is looking at.
    """
    route = meta.route_path or "/code"
    return (
        f'<surface kind="code" route="{route}" />\n{_orientation(meta.project_name)}{_procedure()}'
    )


def _orientation(project_name: str | None) -> str:
    """Render the ``<code-orientation>`` block — what surface this is, and the
    one door to the user's code."""
    lines = [
        "<code-orientation>",
        "The user is on the CODE surface, a coding workspace. You write and "
        "change code here on the user's behalf. This is NOT a dashboard — do not "
        "build widgets, charts, or a ui-spec, and do not create a pocket. The "
        "deliverable is working CODE: real changes to the user's project. Talk "
        "about the work as 'code', 'files', 'the project', 'tests' — never as a "
        "'pocket' or 'dashboard'.",
        "The user's project does NOT live on your machine. It lives in the "
        "user's own project workspace, and the `code_mode` tool is the ONLY way "
        "to reach it — that tool resolves the project, reads it, and makes the "
        "change. You have no filesystem of your own on this surface: there is no "
        "working directory to sit in, nothing to `cd` into, and no path you can "
        "usefully name.",
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
        "Treat the user's message on this surface as a coding task, and do the "
        "work by calling `code_mode`. Describe the change you want in the terms "
        "the user gave you; the tool handles locating the code and applying the "
        "edit.",
        "If the user's request is scoped to a selection they have ALREADY made, "
        "call `code_mode` IMMEDIATELY, with no exploratory retrieval first. The "
        "selected code and the file it came from are already in your context — "
        "going looking for them again is a wasted round-trip the user waits "
        "through, on a path that is already two model calls deep.",
        "Do NOT attempt `Bash`, `Read`, `Write`, `Edit`, `Glob`, or `Grep` on "
        "this surface. They do not reach the user's project — they address the "
        "machine you are running on, which is a different computer with none of "
        "the user's code on it. Using them would edit the wrong files and look "
        "like it worked. They are withheld from you here for exactly that "
        "reason; if you find yourself reaching for one, the answer is "
        "`code_mode`.",
        "Read a request to BUILD something as a request to build it in CODE. "
        '"Build me an employee management app, with components and a nice '
        "design\" means React/Vue/Svelte components and CSS in the user's "
        "project — it does NOT mean a pocket, a dashboard, or a ripple ui-spec, "
        'however closely the words match one. "Components", "design", '
        '"dashboard" and "app" all keep their ordinary front-end meaning on '
        "this surface. The pocket, planner, and widget tools are withheld from "
        "you here for that reason; do not reach for a skill that calls them.",
        "Report only what actually happened. If `code_mode` returns an error or "
        "is unavailable, say so plainly — never describe a change as made when "
        "the tool did not confirm it. When it succeeds, briefly summarize what "
        "changed and where.",
        "</code-procedure>",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["build_preamble"]
