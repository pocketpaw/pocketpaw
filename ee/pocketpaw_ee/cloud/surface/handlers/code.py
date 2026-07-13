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
    """Render the /code surface preamble — edit + run code in the workspace.

    When the client stamps ``current_dir`` / ``project_name`` /
    ``workspace_vm`` / ``storage_root`` / ``is_cloud_storage``
    (set from the frontend's SurfaceMetaProvider on the /code page), the
    preamble tells the agent EXACTLY where the project lives and how to
    access its files.

    Also sets the ``current_project_name`` ContextVar so Daytona tools
    (MCP and SDK) automatically scope to the correct project subdirectory
    inside the workspace VM.

    Storage flavours:
      * Workspace VM (NEW): ``workspace_vm = "true"`` means the project
        lives inside a shared workspace VM. The agent uses Daytona MCP
        tools (read_file, write_file, shell, run_python, etc.) which
        route through the sandbox. The project is a subdirectory of the
        VM's workspace root.
      * Local-disk adapter: ``current_dir`` is a real filesystem path
        (e.g. ``~/.pocketpaw/uploads/projects/{ws}/{uid}/{name}/``).
        The agent can ``cd`` to ``current_dir`` and use Bash/Read/Write/Edit.
      * Pure S3: ``is_cloud_storage = "true"``. Files aren't on disk.
    """
    route = meta.route_path or "/code"

    # ── Set project name context for Daytona tools ──────────────────────
    if meta.project_name:
        from pocketpaw_ee.cloud.daytona.context import set_current_project_name

        set_current_project_name(meta.project_name)

    # ── Storage type ────────────────────────────────────────────────────
    is_vm = meta.workspace_vm == "true"
    is_s3 = meta.is_cloud_storage == "true"
    current_dir = meta.current_dir or "the workspace root"

    # ── Orientation — one block per storage flavour ─────────────────────
    orientation = _build_orientation(
        current_dir,
        meta.project_name,
        is_vm,
        is_s3,
        meta.storage_root,
    )
    procedure = _build_procedure(is_vm, is_s3)

    return f'<surface kind="code" route="{route}" />\n{orientation}{procedure}'


def _build_orientation(
    current_dir: str,
    project_name: str | None,
    is_vm: bool,
    is_s3: bool,
    storage_root: str | None,
) -> str:
    """Render the ``<code-orientation>`` block — tells the agent what
    surface it's on, where the project lives, and how to refer to it."""
    lines = [
        "<code-orientation>",
        "The user is on the CODE surface, a coding workspace. You EDIT and RUN "
        "code here on the user's behalf. This is NOT a dashboard — do not build "
        "widgets, charts, or a ui-spec, and do not create a pocket. The "
        "deliverable is working CODE: files written or changed in the workspace "
        "and verified by running them. Talk about the work as 'code', 'files', "
        "'the workspace', 'tests' — never as a 'pocket' or 'dashboard'.",
    ]

    if is_vm:
        # Workspace VM mode — files live in a shared VM.
        lines.append(
            f"This project runs inside a shared workspace VM (Daytona sandbox). "
            f"The project directory is **{current_dir}** inside the VM. "
            f"Use the Daytona MCP tools (read_file, write_file, edit_file, "
            f"list_dir, shell, run_python, sync_to_s3, start_server, preview_url) "
            f"for ALL file I/O and command execution — these operate directly "
            f"inside the sandbox VM. Use start_server to start a web server "
            f"and get a URL; use preview_url to get a URL for an existing "
            f"server. The shell tool's working directory is already set to "
            f"the project directory."
        )
    elif is_s3:
        lines.append(
            f"This project is backed by cloud storage (S3). "
            f"The storage key prefix is **{current_dir}**. "
            f"Files are NOT directly on the local filesystem. "
        )
        daytona_hint = (
            f"If a Daytona sandbox is provisioned for project "
            f"'{project_name}', use the Daytona MCP tools "
            f"(read_file, write_file, edit_file, list_dir, shell, "
            f"run_python, sync_to_s3, start_server, preview_url) to "
            f"operate directly inside the sandbox VM."
            if project_name
            else "If a Daytona sandbox is provisioned, use the Daytona "
            "MCP tools (read_file, write_file, edit_file, list_dir, "
            "shell, run_python, sync_to_s3, start_server, preview_url) "
            "to operate directly inside the sandbox VM."
        )
        lines.append(daytona_hint)
    else:
        lines.append(f"Your working directory is **{current_dir}**.")

    if project_name:
        lines.append(
            f"The current project is **{project_name}** — all work "
            f"should go into its directory tree."
        )

    lines.append("</code-orientation>")
    return "\n".join(lines) + "\n"


def _build_procedure(is_vm: bool, is_s3: bool) -> str:
    """Render the ``<code-procedure>`` block — how to do the work."""
    lines = [
        "<code-procedure>",
        "Treat the user's message on this surface as a coding task. Use the "
        "built-in tools to do the work. PREFER the `code` skill — invoke it "
        "by intent (no slash command needed); it owns the edit→run→verify "
        "loop. Always VERIFY before claiming the task is done: run the code "
        "or its tests and confirm the output, rather than asserting success "
        "from the edit alone. If a command fails, read the error, fix it, "
        "and re-run.",
    ]

    if is_vm:
        lines.extend(
            [
                "This project is in a shared workspace VM. Use the Daytona MCP "
                "tools (read_file, write_file, edit_file, list_dir, shell, "
                "run_python, sync_to_s3) for ALL work. Do NOT use local Bash, "
                "Read, Write, Edit, Glob, or Grep — those operate on the local "
                "filesystem, not the VM. The shell tool runs commands inside "
                "the sandbox with the project directory as the working directory.",
            ]
        )
    elif is_s3:
        lines.extend(
            [
                "This is a cloud-storage project — files are in S3, not on the "
                "local disk. If a Daytona sandbox is provisioned, use Daytona "
                "MCP tools. Otherwise use the cloud project REST API endpoints "
                "(/cloud/projects/{name}/files/*).",
            ]
        )
    else:
        lines.append(
            "The workspace and project directory ARE on the local filesystem — "
            "navigate to the working directory above and work there."
        )

    lines.extend(
        [
            "The workspace is JAILED — stay inside it. Destructive shell "
            "operations are blocked. When done, briefly summarize what changed "
            "and how you verified it.",
            "</code-procedure>",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = ["build_preamble"]
