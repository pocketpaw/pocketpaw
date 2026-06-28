# ee/pocketpaw_ee/cloud/belt/service.py
# Updated: 2026-06-11 (feat/belt-autopilot) — the runs read model now renders a
#   QUEUED STATION RUN. A pending ``code_change`` Action whose blob carries
#   ``station_pending=True`` (filed by the mandate ``StationTaskDispatcher`` with
#   the task text but no diff yet) derives status ``queued`` / stage ``station``
#   instead of ``proposed`` / ``gate``, so the console shows it as waiting for a
#   human to open the develop station (one click) rather than sitting at the
#   approve gate.
# Created: 2026-06-10 (feat/belt-console-backend, SC-1 + SC-2) — the Belt &
# Pulley console read/write service. Powers the /belt page's three needs the
# first live runs exposed: (1) DISCOVER repos so the user can bind one up front
# instead of the agent asking, (2) ADD a new repo root durably (admin-gated),
# (3) read STATION RUNS + a run's diff so the page can show status/output.
#
# Updated: 2026-06-11 (feat/belt-repo-init) — added ``init_repo``: create a
# brand-new git REPOSITORY under an allowlisted root (admin-gated, same RBAC +
# realpath discipline as add_repo), seed a README + initial commit so it has a
# HEAD and a default branch, register it via the same persistence add_repo uses,
# and (optionally) create the GitHub remote via the injectable ``RepoCreator``
# (default ``GhCliRepoCreator`` shells ``gh repo create --private --source --push``,
# mirroring the executor's ``GhCliPrOpener`` injectable). A remote-creation
# failure KEEPS the local repo and returns a ``remote_error`` message — the local
# init is never rolled back. The runs read model (_run_summary) now also surfaces
# ``commit_sha`` for local-only (no-origin) landings, where ``pr_url`` is None.
#
# What lives here:
#   * ``resolve_allowlist_roots(workspace_id)`` — the union of
#     ``settings.belt_repo_allowlist`` and the per-workspace persisted extension
#     (``BeltWorkspaceConfig.allowlist_roots``), each realpath-resolved. This is
#     the single source of truth the discovery + add paths share; the MCP
#     resolver keeps its own settings-only view (defense in depth at propose /
#     execute time — the console extension is additive, never a replacement).
#   * ``discover_repos(workspace_id)`` — one-level-deep scan of every allowlist
#     root for git repos (a root that IS a git repo counts too). Returns the
#     wire shape {path, name, current_branch, branches[]}.
#   * ``add_repo(workspace_id, path)`` — validate the path is an existing git
#     repo, realpath-resolve it, and APPEND it to the workspace's persisted
#     allowlist extension. Returns the same repo shape or a typed error.
#   * ``list_runs(workspace_id)`` / ``get_run(workspace_id, action_id)`` — the
#     runs read model over the belt code-change Instinct Actions (kind=
#     code_change), newest-first, with status/stage derived from the Action
#     lifecycle and pr_url/branch read STRUCTURALLY off the blob (the executor
#     back-writes them on success — no free-text parsing).
#
# Security:
#   * git introspection runs through ``asyncio.create_subprocess_exec`` with an
#     ARG LIST — never ``shell=True``, never string-interpolated input. Reuses
#     the executor's ``_run`` chokepoint.
#   * a submitted add-repo path is realpath-resolved and confirmed to be a git
#     repo BEFORE it is persisted; a non-existent / non-git / unresolvable path
#     is refused. The path itself is NOT echoed into logs (only the workspace id
#     + a generic reason) so a path probe can't leak via logs.
#   * the diff text on a run detail is capped (~200 KB) so a giant blob can't be
#     pulled whole through the read API.
#
# SSE / realtime (SC-2): ``emit_belt_run_updated`` publishes on the WORKSPACE
# REALTIME BUS (``_core.realtime.emit`` → the ``belt_run_updated`` audience
# branch fans out to every workspace member), with an additional best-effort
# per-stream ``push_sse_event`` for in-turn freshness. The bus is the required
# path because approve / executed / failed fire after the chat turn's SSE drain
# is gone — the per-session push alone would never reach the page.

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Cap the diff text returned on a run-detail read. Mirrors the propose-time
# ``MAX_DIFF_BYTES`` cap so a read can never pull more than the gate accepted.
MAX_DIFF_BYTES = 200 * 1024  # 200 KB

# How many branch names to return per repo (the contract caps at ~20).
MAX_BRANCHES = 20

# The ``_code_change`` blob key + kind discriminator — duplicated as literals
# here so the console service has no import dependency on the agent-side MCP
# module (the OSS-EE boundary keeps the agent layer out of the cloud read path).
_CODE_CHANGE_PARAM_KEY = "_code_change"

# A repo name must be a SINGLE safe directory segment — lowercase alphanumerics
# plus ``. _ -``, no path separators, no leading dot. This is the security
# contract for the init route: the name is joined onto an allowlisted
# ``location_root`` to form the target dir, so it must NOT be able to traverse
# (``..``), absolutize (``/foo``), or smuggle a separator. The realpath +
# containment check after the join is the second line of defense.
_REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# A repo name capped well under any filesystem limit; keeps the dir name sane.
_MAX_REPO_NAME = 100


async def emit_belt_run_updated(
    *,
    workspace_id: str,
    action_id: str,
    status: str,
    stage: str,
    pr_url: str | None = None,
) -> None:
    """Publish ``belt_run_updated`` for a station run lifecycle change.

    PRIMARY path — the WORKSPACE REALTIME BUS (``_core.realtime.emit``), the same
    path Tray / Mission Control / pocket events ride. This is REQUIRED because a
    run's status changes ASYNCHRONOUSLY relative to the chat turn: propose lands
    during the turn, but approve (in the Tray) and the executed / failed
    terminals fire long after the turn's per-session SSE drain is gone. The
    workspace bus fans the event out to every workspace member with the /belt
    console open (the audience resolver has a ``belt_run_updated`` branch keyed
    on ``workspace_id``). The page subscribes via its global workspace bus.

    SECONDARY path — the per-stream ``push_sse_event`` (the ``pocket_created``
    path) for in-TURN freshness on the propose emit, when an agent stream sink is
    in scope. A no-op outside a stream; never the sole delivery path.

    Best-effort by construction: a bus / SSE failure is swallowed so a
    propose / approve / reject / execute path is never broken. The runs read
    model (GET /belt/runs) is the source of truth; this is the live nudge.
    """
    data: dict[str, Any] = {
        "workspace_id": workspace_id,
        "action_id": action_id,
        "status": status,
        "stage": stage,
    }
    if pr_url:
        data["pr_url"] = pr_url

    # PRIMARY — workspace realtime bus (async fan-out to every workspace member).
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import BeltRunUpdated

        await emit(BeltRunUpdated(data=dict(data)))
    except Exception:  # noqa: BLE001 — bus publish must never break a lifecycle path
        logger.debug("belt: belt_run_updated bus emit failed (non-fatal)", exc_info=True)

    # SECONDARY — in-turn per-stream SSE (only reaches an active chat stream).
    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_sse_event

        push_sse_event("belt_run_updated", dict(data))
    except Exception:  # noqa: BLE001 — SSE push must never break a lifecycle path
        logger.debug("belt: belt_run_updated SSE push failed (non-fatal)", exc_info=True)


class BeltConsoleError(Exception):
    """A user-facing console error carrying an HTTP status + a clear message.

    The router maps this to the cloud error envelope. The message is safe to
    show the user (no path content beyond what they submitted, no stack)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Allowlist resolution — settings UNION persisted extension
# ---------------------------------------------------------------------------


def _resolve_settings_roots() -> list[Path]:
    """Realpath-resolve the static ``settings.belt_repo_allowlist`` roots.

    Mirrors the MCP resolver's ``_resolve_allowlist`` default behaviour: when the
    setting is empty, fall back to the cwd's PARENT (the workspace root holding
    the project checkouts) so a stock deployment still discovers the workspace
    tree rather than nothing.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    roots: list[Path] = []
    for raw in settings.belt_repo_allowlist or []:
        try:
            roots.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            logger.warning("belt: skipping unresolvable settings allowlist root")
    if not roots:
        roots.append(Path.cwd().resolve().parent)
    return roots


async def _load_persisted_roots(workspace_id: str) -> list[str]:
    """Load the workspace's persisted allowlist extension (raw strings).

    Best-effort: a missing doc (the common case — no console additions yet) or
    a Mongo read failure returns an empty list so discovery still works off the
    settings roots. Only this module reads ``BeltWorkspaceConfig``."""
    try:
        from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig

        doc = await BeltWorkspaceConfig.find_one(BeltWorkspaceConfig.workspace == workspace_id)
        if doc is None:
            return []
        return list(doc.allowlist_roots or [])
    except Exception:  # noqa: BLE001 — read is best-effort; settings roots still apply
        logger.warning("belt: failed to load persisted allowlist for workspace", exc_info=True)
        return []


async def resolve_allowlist_roots(workspace_id: str) -> list[Path]:
    """The console's allowlist view: settings roots UNION the persisted
    extension, each realpath-resolved + de-duplicated (order-stable).

    This is ADDITIVE over the settings-only view the MCP resolver uses — the
    console can authorize new roots at runtime, and both the discovery scan and
    the add-repo containment check read this union. Resolution collapses
    symlinks + ``..`` so a stored string can't widen the boundary by traversal.
    """
    roots: list[Path] = list(_resolve_settings_roots())
    for raw in await _load_persisted_roots(workspace_id):
        try:
            roots.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            logger.warning("belt: skipping unresolvable persisted allowlist root")
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# git introspection (read-only, arg-list subprocess only)
# ---------------------------------------------------------------------------


def _is_git_repo(path: Path) -> bool:
    """True when ``path`` is a directory holding a ``.git`` entry (worktree) or
    is itself a bare repo's git dir. Cheap filesystem check — no subprocess."""
    if not path.is_dir():
        return False
    return (path / ".git").exists()


async def _current_branch(path: Path) -> str:
    """Resolve the repo's current branch (``git rev-parse --abbrev-ref HEAD``).

    Returns the branch name, or ``"HEAD"`` on a detached head, or ``""`` on any
    error — a repo we can't introspect still lists, just without a branch.
    """
    from pocketpaw_ee.cloud.belt.executor import _run

    code, out, _err = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if code != 0:
        return ""
    return out.strip()


async def _branches(path: Path) -> list[str]:
    """List up to ``MAX_BRANCHES`` local branch names (``git branch
    --format=%(refname:short)``). Empty list on any error."""
    from pocketpaw_ee.cloud.belt.executor import _run

    code, out, _err = await _run(["git", "branch", "--format=%(refname:short)"], cwd=path)
    if code != 0:
        return []
    names = [line.strip() for line in out.splitlines() if line.strip()]
    return names[:MAX_BRANCHES]


async def _repo_view(path: Path) -> dict[str, Any]:
    """Build the wire shape for one repo: {path, name, current_branch, branches}."""
    return {
        "path": str(path),
        "name": path.name,
        "current_branch": await _current_branch(path),
        "branches": await _branches(path),
    }


# ---------------------------------------------------------------------------
# discover_repos — one-level scan of the allowlist roots
# ---------------------------------------------------------------------------


async def discover_repos(workspace_id: str) -> dict[str, Any]:
    """Discover git repos under the workspace's allowlist roots, one level deep.

    For each root: if the root ITSELF is a git repo it counts; then every
    immediate child directory that is a git repo counts too. Returns
    ``{"repos": [<repo view>, ...]}`` de-duplicated by resolved path, sorted by
    name. A root that doesn't exist or isn't readable is skipped (the scan must
    not 500 because one root is stale).
    """
    roots = await resolve_allowlist_roots(workspace_id)
    found: dict[str, Path] = {}
    for root in roots:
        try:
            if _is_git_repo(root):
                found[str(root)] = root
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                try:
                    if child.is_dir() and _is_git_repo(child):
                        resolved = child.resolve()
                        found.setdefault(str(resolved), resolved)
                except OSError:
                    continue
        except OSError:
            logger.warning("belt: skipping unreadable allowlist root during discovery")
            continue

    repos = [await _repo_view(p) for p in found.values()]
    repos.sort(key=lambda r: (r["name"].lower(), r["path"]))
    return {"repos": repos}


# ---------------------------------------------------------------------------
# add_repo — validate + persist a new root (admin-gated at the router)
# ---------------------------------------------------------------------------


async def add_repo(workspace_id: str, raw_path: str) -> dict[str, Any]:
    """Validate ``raw_path`` is an existing git repo and persist it as a new
    allowlist root for the workspace. Returns ``{"repo": <repo view>}``.

    Validation order (fail fast, no path leakage):
      1. non-empty string
      2. realpath-resolves (symlinks + ``..`` collapsed)
      3. is an existing directory
      4. is a git repo (``.git`` present)
    Any failure raises ``BeltConsoleError(400, ...)`` with a clear message. On
    success the RESOLVED path string is appended to
    ``BeltWorkspaceConfig.allowlist_roots`` (idempotent — a path already present
    is not duplicated) and the repo view is returned.

    Persisting the REALPATH (not the raw submission) is the security contract:
    the stored root is the collapsed path, so a later containment check can't be
    fooled by a ``..`` in the original submission.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise BeltConsoleError(400, "A repo path is required.")

    try:
        resolved = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError):
        # Do NOT echo the raw path — a probe shouldn't learn anything from logs.
        logger.warning("belt: add_repo rejected an unresolvable path for workspace")
        raise BeltConsoleError(400, "That path could not be resolved.") from None

    if not resolved.is_dir():
        raise BeltConsoleError(400, "That path does not exist or is not a directory.")

    if not _is_git_repo(resolved):
        raise BeltConsoleError(400, "That path is not a git repository (no .git found).")

    await _persist_root(workspace_id, str(resolved))
    return {"repo": await _repo_view(resolved)}


async def _persist_root(workspace_id: str, resolved_path: str) -> None:
    """Append ``resolved_path`` to the workspace's persisted allowlist extension.

    Idempotent: a path already present is left as-is. Upserts the per-workspace
    ``BeltWorkspaceConfig`` doc (one row per workspace). A Mongo failure raises a
    ``BeltConsoleError(500, ...)`` so the user knows the add did NOT take — a
    silent no-op here would leave the agent unable to use a repo the user thinks
    they authorized.
    """
    try:
        from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig

        doc = await BeltWorkspaceConfig.find_one(BeltWorkspaceConfig.workspace == workspace_id)
        if doc is None:
            doc = BeltWorkspaceConfig(workspace=workspace_id, allowlist_roots=[resolved_path])
            await doc.insert()
            return
        if resolved_path not in (doc.allowlist_roots or []):
            doc.allowlist_roots = [*(doc.allowlist_roots or []), resolved_path]
            await doc.save()
    except BeltConsoleError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface the persistence failure to the user
        logger.warning("belt: failed to persist allowlist root for workspace", exc_info=True)
        raise BeltConsoleError(500, "Could not save the repo. Please retry.") from exc


# ---------------------------------------------------------------------------
# init_repo — create a brand-new git repo under an allowlisted root, register it
# ---------------------------------------------------------------------------


class RepoCreator(Protocol):
    """Injectable remote-repo creator (mirrors the executor's ``PrOpener``).

    The default implementation shells ``gh repo create`` from the new repo dir;
    tests inject a fake to assert the call args without touching GitHub. Raises
    on any failure so the init route can record a ``remote_error`` while keeping
    the local repo. ``gh`` reads the authenticated token from the environment —
    no secret is ever passed on the command line."""

    async def create_remote(self, *, repo_path: Path, name: str) -> None: ...


class GhCliRepoCreator:
    """Default ``RepoCreator`` — shells ``gh repo create --private --source
    <path> --push`` from the new repo dir.

    Mirrors the executor's ``GhCliPrOpener``: argv-only (never ``shell=True``,
    never string interpolation), the name + path are literal argv elements, and
    ``gh`` reads its token from the environment. Raises ``RuntimeError`` on a
    non-zero exit so the caller records the remote failure without rolling back
    the local repo."""

    async def create_remote(self, *, repo_path: Path, name: str) -> None:
        from pocketpaw_ee.cloud.belt.executor import _run

        code, out, err = await _run(
            [
                "gh",
                "repo",
                "create",
                name,
                "--private",
                "--source",
                str(repo_path),
                "--push",
            ],
            cwd=repo_path,
        )
        if code != 0:
            raise RuntimeError(f"gh repo create failed (exit {code}): {err.strip() or out.strip()}")


def _validate_repo_name(name: str) -> str:
    """Validate ``name`` is a safe single directory segment, return it stripped.

    Rejects (with a clear, path-free message) an empty name, a name with a path
    separator, a name that starts with a dot or hyphen, a too-long name, or any
    character outside ``[a-z0-9._-]``. This is the first security gate on the
    init route — the validated name is joined onto an allowlisted root to form
    the target dir, so it must never traverse or absolutize."""
    if not isinstance(name, str) or not name.strip():
        raise BeltConsoleError(400, "A repository name is required.")
    candidate = name.strip()
    if len(candidate) > _MAX_REPO_NAME:
        raise BeltConsoleError(
            400, f"Repository name must be {_MAX_REPO_NAME} characters or fewer."
        )
    if "/" in candidate or "\\" in candidate or candidate in (".", ".."):
        raise BeltConsoleError(
            400, "Repository name cannot contain path separators or be '.' / '..'."
        )
    if not _REPO_NAME_RE.match(candidate):
        raise BeltConsoleError(
            400,
            "Repository name must start with a letter or digit and use only "
            "lowercase letters, digits, '.', '_', or '-'.",
        )
    return candidate


async def _resolve_location_root(workspace_id: str, raw_root: str) -> Path:
    """Resolve ``raw_root`` and require it to be an existing dir INSIDE one of
    the workspace's allowlist roots. Returns the resolved root.

    The containment check runs on the REALPATH so a ``..`` in the submission
    can't escape the boundary. A root outside every allowlist root, or one that
    doesn't exist, is refused with a path-free 400 (we never confirm whether an
    out-of-bounds path exists)."""
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise BeltConsoleError(400, "A location is required.")
    try:
        resolved = Path(raw_root).expanduser().resolve()
    except (OSError, RuntimeError):
        logger.warning("belt: init_repo rejected an unresolvable location root for workspace")
        raise BeltConsoleError(400, "That location could not be resolved.") from None

    roots = await resolve_allowlist_roots(workspace_id)
    if not _is_within_roots(resolved, roots):
        # Don't leak whether the out-of-bounds path exists — same message either way.
        raise BeltConsoleError(400, "That location is not under an authorized root.")
    if not resolved.is_dir():
        raise BeltConsoleError(400, "That location does not exist or is not a directory.")
    return resolved


def _is_within_roots(path: Path, roots: list[Path]) -> bool:
    """True when ``path`` is inside (or equal to) one of the allowlist ``roots``.
    ``path`` MUST already be resolved by the caller. Mirrors the MCP resolver's
    ``_is_within_allowlist``."""
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


async def init_repo(
    workspace_id: str,
    *,
    name: str,
    location_root: str,
    create_remote: bool,
    repo_creator: RepoCreator | None = None,
) -> dict[str, Any]:
    """Create a brand-new git repo under an allowlisted root and register it.

    Steps (each failure raises a path-free ``BeltConsoleError`` BEFORE any
    filesystem mutation it would have to roll back):
      1. validate ``name`` is a safe single dir segment (no separators / ``..``).
      2. resolve ``location_root`` and require it under the workspace allowlist.
      3. refuse if ``<location_root>/<name>`` already exists.
      4. ``git init`` (argv-only subprocess — never ``shell=True``).
      5. seed a minimal ``README.md`` (the repo name) and commit it so the repo
         has a HEAD + a default branch.
      6. register the new repo path via the same persistence ``add_repo`` uses
         (``_persist_root``) so discovery + the code-change boundary pick it up.
      7. if ``create_remote`` — shell ``gh repo create ... --push`` via the
         injectable ``RepoCreator``. On remote FAILURE the LOCAL repo is KEPT and
         the response carries a ``remote_error`` message (the frontend shows it
         inline); the local init is NEVER rolled back.

    Returns ``{"repo": {path, name, current_branch, branches}}`` (the standard
    repo shape the registry returns) plus an optional top-level ``remote_error``
    string when a requested remote creation failed.
    """
    safe_name = _validate_repo_name(name)
    root = await _resolve_location_root(workspace_id, location_root)
    target = root / safe_name

    if target.exists():
        raise BeltConsoleError(400, f"A directory named '{safe_name}' already exists there.")

    from pocketpaw_ee.cloud.belt.executor import _run

    # 4. Create the dir + git init. We make the dir ourselves so a partial init
    #    leaves an obvious, removable artifact (no surprise reuse of an existing
    #    path — we already refused an existing target above).
    try:
        target.mkdir(parents=False, exist_ok=False)
    except OSError:
        logger.warning("belt: init_repo could not create the target dir for workspace")
        raise BeltConsoleError(400, "Could not create the repository directory.") from None

    code, _out, err = await _run(["git", "init"], cwd=target)
    if code != 0:
        # Clean up the empty dir we just made so a retry isn't blocked.
        _safe_rmdir(target)
        raise BeltConsoleError(400, f"git init failed: {err.strip()[:200]}")

    # 5. Seed README.md and make the initial commit so the repo has a HEAD and a
    #    default branch (an empty repo has neither, which breaks discovery's
    #    branch read and a later code-change base).
    try:
        (target / "README.md").write_text(f"# {safe_name}\n", encoding="utf-8")
    except OSError:
        _safe_rmdir(target)
        raise BeltConsoleError(400, "Could not seed the repository README.") from None

    code, _out, err = await _run(["git", "add", "README.md"], cwd=target)
    if code != 0:
        _safe_rmdir(target)
        raise BeltConsoleError(400, f"git add failed: {err.strip()[:200]}")
    # Stamp a deterministic committer identity via ``-c`` flags so the seed commit
    # lands even when the host has no global git ``user.name`` / ``user.email``
    # (a bare server / CI). argv-only — the identity is literal argv, never a
    # shell string.
    code, _out, err = await _run(
        [
            "git",
            "-c",
            "user.name=PocketPaw Belt",
            "-c",
            "user.email=belt@pocketpaw.local",
            "commit",
            "-m",
            "Initial commit",
        ],
        cwd=target,
    )
    if code != 0:
        _safe_rmdir(target)
        raise BeltConsoleError(400, f"git commit failed: {err.strip()[:200]}")

    resolved_target = target.resolve()

    # 6. Register the new repo so discovery + the code-change boundary see it.
    await _persist_root(workspace_id, str(resolved_target))

    result: dict[str, Any] = {"repo": await _repo_view(resolved_target)}

    # 7. Optional remote creation. A failure NEVER rolls back the local repo —
    #    the local init already succeeded and is registered; we surface the
    #    remote failure inline so the user can retry the remote half by hand.
    if create_remote:
        creator: RepoCreator = repo_creator or GhCliRepoCreator()
        try:
            await creator.create_remote(repo_path=resolved_target, name=safe_name)
            # Refresh the view so a new ``origin``/branch state is reflected.
            result["repo"] = await _repo_view(resolved_target)
        except Exception as exc:  # noqa: BLE001 — keep the local repo, report the remote gap
            logger.warning("belt: init_repo remote creation failed for workspace", exc_info=True)
            result["remote_error"] = (
                f"The local repository was created, but the GitHub remote could "
                f"not be created: {exc}. Create it manually or retry."
            )

    return result


def _safe_rmdir(path: Path) -> None:
    """Best-effort removal of a half-initialized target dir — never raises.
    Used to clean up after a git-init / seed-commit failure so a retry isn't
    blocked by a stale empty dir."""
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.debug("belt: init_repo cleanup failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# runs read model — over the belt code_change Instinct Actions
# ---------------------------------------------------------------------------


def _code_change_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_code_change`` blob on an Action, or ``None`` for a
    non-belt Action. Mirrors the instinct router's helper of the same name."""
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get(_CODE_CHANGE_PARAM_KEY)
    return blob if isinstance(blob, dict) else None


# Action lifecycle → (console status, console stage). ``proposed`` sits at the
# gate awaiting human review; everything terminal is ``done``. ``approved`` is a
# transient state (the executor runs immediately after approve) — kept at the
# gate so a run caught mid-apply doesn't read as done.
_STATUS_MAP: dict[str, tuple[str, str]] = {
    "pending": ("proposed", "gate"),
    "approved": ("approved", "gate"),
    "rejected": ("rejected", "done"),
    "executed": ("landed", "done"),
    "failed": ("failed", "done"),
}


def _derive_status_stage(action: Any, blob: dict[str, Any] | None = None) -> tuple[str, str]:
    """Map an Action's status to the console (status, stage) pair. An unknown
    status (forward-compat) falls back to (the raw value, 'gate').

    A QUEUED STATION RUN — a pending ``code_change`` Action whose blob carries
    ``station_pending=True`` (filed by the mandate ``StationTaskDispatcher`` with
    no diff yet) — reads as ``("queued", "station")`` so the console shows it as
    waiting for a human to open the develop station, not sitting at the gate."""
    if blob is not None and blob.get("station_pending"):
        raw = getattr(getattr(action, "status", None), "value", None) or str(
            getattr(action, "status", "")
        )
        # Only a still-pending queued run reads as "queued"; once the human drives
        # the station and a diff is proposed, a fresh non-pending row supersedes it.
        if raw == "pending":
            return ("queued", "station")
    raw = getattr(getattr(action, "status", None), "value", None) or str(
        getattr(action, "status", "")
    )
    return _STATUS_MAP.get(raw, (raw or "proposed", "gate"))


def _run_summary(action: Any, blob: dict[str, Any]) -> dict[str, Any]:
    """Build the runs-list row for one belt Action (no diff — that's detail-only).

    Reads task/summary/repo/base_branch/correlation_id off the blob, and the
    landing fields — ``branch`` / ``pr_url`` / ``commit_sha`` — STRUCTURALLY off
    the blob (the executor back-writes them on success), and status/stage from the
    Action lifecycle. ``created_at`` is ISO-8601.

    Two landing shapes feed this row:
      * WITH-REMOTE — ``pr_url`` + ``branch`` are present; the page renders a PR
        link.
      * LOCAL-ONLY (no origin) — ``branch`` + ``commit_sha`` are present and
        ``pr_url`` is None; the page renders a branch chip. We emit ``pr_url:
        None`` cleanly (key present, value null) so the frontend's
        ``pr_url ? <link> : <chip>`` switch is unambiguous.
    """
    status, stage = _derive_status_stage(action, blob)
    created = getattr(action, "created_at", None)
    repo_path = str(blob.get("repo") or "")
    return {
        "action_id": str(getattr(action, "id", "")),
        "task": str(blob.get("task") or ""),
        "summary": str(blob.get("summary") or ""),
        "status": status,
        "stage": stage,
        "repo": repo_path,
        "repo_name": Path(repo_path).name if repo_path else "",
        "base_branch": str(blob.get("base_branch") or ""),
        "branch": blob.get("branch") or None,
        "pr_url": blob.get("pr_url") or None,
        "commit_sha": blob.get("commit_sha") or None,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else None,
        "correlation_id": str(blob.get("correlation_id") or "") or None,
    }


async def list_runs(workspace_id: str) -> dict[str, Any]:
    """List the workspace's Belt station runs, newest-first.

    Belt Actions carry ``pocket_id = workspace_id`` (they aren't bound to a
    pocket), so we list actions for the workspace and keep only those carrying a
    ``_code_change`` blob. ``list_actions`` already orders newest-first. Returns
    ``{"runs": [<run summary>, ...]}``.
    """
    from pocketpaw.stores import get_instinct_store

    # ISO: HTTP console path (no ``current_workspace`` ContextVar) — scope the
    # store to the caller's workspace so the listing reads the tenant's file.
    store = get_instinct_store(workspace_id=workspace_id or None)
    actions = await store.list_actions(pocket_id=workspace_id, limit=200)
    runs: list[dict[str, Any]] = []
    for action in actions:
        blob = _code_change_blob(action)
        if blob is None:
            continue
        runs.append(_run_summary(action, blob))
    return {"runs": runs}


async def get_run(workspace_id: str, action_id: str) -> dict[str, Any]:
    """Return a single run + its proposed diff (capped at ~200 KB).

    Tenancy: the Action must carry a ``_code_change`` blob whose ``workspace_id``
    matches the caller's workspace — a foreign or non-belt Action is a 404 (we
    never confirm a cross-tenant Action exists). The diff is read off the blob
    and truncated to ``MAX_DIFF_BYTES``; a ``diff_truncated`` flag tells the UI
    when it was cut.
    """
    from pocketpaw.stores import get_instinct_store

    # ISO: HTTP console path (no ``current_workspace`` ContextVar) — scope the
    # store to the caller's workspace so a foreign action is never read from the
    # shared file (the blob-workspace 404 below stays as belt-and-braces).
    store = get_instinct_store(workspace_id=workspace_id or None)
    action = await store.get_action(action_id)
    blob = _code_change_blob(action) if action is not None else None
    if action is None or blob is None:
        raise BeltConsoleError(404, "Run not found.")
    if str(blob.get("workspace_id") or "") != workspace_id:
        # Don't distinguish "wrong workspace" from "missing" — same 404.
        raise BeltConsoleError(404, "Run not found.")

    summary = _run_summary(action, blob)
    diff = blob.get("diff")
    diff_text = diff if isinstance(diff, str) else ""
    encoded = diff_text.encode("utf-8")
    truncated = False
    if len(encoded) > MAX_DIFF_BYTES:
        diff_text = encoded[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
        truncated = True
    summary["diff"] = diff_text
    summary["diff_truncated"] = truncated
    return summary


__all__ = [
    "BeltConsoleError",
    "GhCliRepoCreator",
    "MAX_BRANCHES",
    "MAX_DIFF_BYTES",
    "RepoCreator",
    "add_repo",
    "discover_repos",
    "emit_belt_run_updated",
    "get_run",
    "init_repo",
    "list_runs",
    "resolve_allowlist_roots",
]
