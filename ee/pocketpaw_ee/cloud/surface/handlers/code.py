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
    ``storage_root`` / ``is_cloud_storage`` (set from the frontend's
    SurfaceMetaProvider on the /code page), the preamble tells the agent
    EXACTLY where the project lives and how to access its files — no more
    guessing or requiring the user to spell out the S3 storage path.

    Storage flavours (self-hosted local-disk adapter vs S3-only):
      * Local-disk adapter: ``current_dir`` is a real filesystem path
        (e.g. ``~/.pocketpaw/uploads/projects/{ws}/{uid}/{name}/``).
        ``is_cloud_storage`` is ``None``. The agent can ``cd`` to
        ``current_dir`` and use Bash/Read/Write/Edit normally.
      * Pure S3 (no local filesystem representation): ``current_dir``
        is the storage key prefix
        (e.g. ``projects/{ws}/{uid}/{name}/``) and
        ``is_cloud_storage = "true"``. Files aren't on disk — the agent
        must use a synced Daytona sandbox or the cloud project REST API.
    """
    route = meta.route_path or "/code"

    # ── Storage type ────────────────────────────────────────────────────
    is_s3 = meta.is_cloud_storage == "true"
    current_dir = meta.current_dir or "the workspace root"

    # ── Orientation — one block per storage flavour ─────────────────────
    orientation = _build_orientation(current_dir, meta.project_name, is_s3, meta.storage_root)
    procedure = _build_procedure(is_s3)

    return f'<surface kind="code" route="{route}" />\n{orientation}{procedure}'


def _build_orientation(
    current_dir: str,
    project_name: str | None,
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

    # Working directory — the one piece of info the agent absolutely needs
    # to navigate to the right place.
    if is_s3:
        lines.append(
            f"This project is backed by cloud storage (S3). "
            f"The storage key prefix is **{current_dir}**. "
            f"Files are NOT directly on the local filesystem — "
            f"use the cloud project REST API or a synced Daytona sandbox "
            f"to access them."
        )
    else:
        lines.append(f"Your working directory is **{current_dir}**.")

    # Project name — lets the agent refer to the project naturally.
    if project_name:
        lines.append(
            f"The current project is **{project_name}** — all work "
            f"should go into its directory tree."
        )

    lines.append("</code-orientation>")
    return "\n".join(lines) + "\n"


def _build_procedure(is_s3: bool) -> str:
    """Render the ``<code-procedure>`` block — how to do the work."""
    lines = [
        "<code-procedure>",
        "Treat the user's message on this surface as a coding task. Use the "
        "built-in tools to do the work: `Bash` to run commands, `Read` to read "
        "files, `Write` / `Edit` to change them, and `Glob` / `Grep` to find "
        "code. PREFER the `code` skill — invoke it by intent (no slash command "
        "needed); it owns the edit→run→verify loop. Always VERIFY before "
        "claiming the task is done: run the code or its tests and confirm the "
        "output, rather than asserting success from the edit alone. If a command "
        "fails, read the error, fix it, and re-run.",
    ]

    if is_s3:
        lines.extend(
            [
                "This is a cloud-storage project — files are in S3, not on the local "
                "disk. If a Daytona sandbox is provisioned and synced, you can use "
                "Bash/Read/Write/Edit there. Otherwise, use the cloud project REST "
                "API endpoints (/cloud/projects/{name}/files/*) to browse, read, "
                "and write files. If the user mentions S3 or cloud storage, you "
                "already know the storage key prefix — no need to ask.",
            ]
        )
    else:
        lines.append(
            "The workspace and project directory ARE on the local filesystem — "
            "navigate to the working directory above and work there."
        )

    lines.extend(
        [
            "The workspace is JAILED — stay inside it; do not reach outside the "
            "working directory. Destructive shell operations are blocked, so do not "
            "attempt to delete or move large trees, format disks, or run anything "
            "irreversible. When done, briefly summarize what changed and how you "
            "verified it.",
            "</code-procedure>",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = ["build_preamble"]
