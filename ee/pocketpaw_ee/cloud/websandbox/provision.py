# provision.py — Web Cursor cold-provision + file-tree orchestration + idle reaper.
# Created 2026-07-15 (WC-2, feat/websandbox-vm-provision).
#
# This is the service-layer orchestration for the Web Cursor "open a repo" slice.
# It sits ABOVE ``websandbox/service.py`` (the registry + auth oracle) and drives
# the Daytona runtime through ``DaytonaClient``. Per ee/cloud Rule 2 it NEVER
# touches the WebSandbox Beanie doc directly — every registry read/write goes
# through ``service`` (create_sandbox / update_status / get_sandbox /
# authorize_sandbox / list_reapable_sandboxes / mark_reaped). Importing the
# DaytonaClient here (not in router/dto/domain) is the correct layer.
#
# Three flows live here:
#   1. ``open_sandbox`` — upsert a registry row (pending) → opening →
#      cold-provision a Daytona VM (auto_stop 5 / archive 5 / delete-on-stop) →
#      wait for boot →
#      clone the repo: via the BROKER (server-side, token-isolated) when the
#      caller has a GitHub connection that can reach it (CM-3c — the token NEVER
#      enters the VM), else the PUBLIC in-VM clone (no credentials) → create +
#      check out a ``paw/edit-<hex>`` feature branch IN the VM so AI edits never
#      touch the checked-out default branch (WC-5a) → bind the Daytona id + branch
#      + mark ready. On any mid-flight failure the row is marked ``stopped`` and a
#      CloudError is raised — the row is never left stuck in ``opening`` silently,
#      and a half-created VM is best-effort torn down.
#   2. ``get_tree`` — resolve the row (tenant-scoped) → authorize on its Daytona
#      id (fail-closed) → single-level ``list_files`` → the file tree.
#   3. Idle-TTL reaper — a background sweep that reclaims rows whose ``updated_at``
#      is older than the TTL by stopping+deleting the VM and marking the row
#      ``reaped``. Labels are NOT used to reconcile because ``DaytonaClient.
#      create_sandbox`` does not forward labels to the SDK; the Registry (our DB)
#      is the source of truth instead. WC-3 will refresh ``updated_at`` on live
#      WebSocket traffic; until then "idle" == ``updated_at`` age. The SDK's own
#      Daytona-native lifecycle (stop 5 / archive 5 / delete-on-stop) is a
#      second, independent backstop that reclaims idle VMs even faster.
#
# Every provisioning fn takes ``client: DaytonaClient | None = None`` (default
# ``get_daytona_client()``) — the mandatory DI seam so tests inject a FAKE client
# and no test ever hits real Daytona.
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, ConflictError, with_cause
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import broker as websandbox_broker
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxView
from pocketpaw_ee.cloud.websandbox.dto import (
    OpenSandboxRequest,
    SandboxTreeResponse,
    TreeEntryResponse,
)

logger = logging.getLogger(__name__)

# Daytona-native VM lifecycle (all in MINUTES — the SDK counts these in minutes,
# NOT seconds; its own 3600 default is 60 HOURS, a latent trap). Aggressive by
# design: a Code Mode sandbox is pure-ephemeral (the durable half is the
# CodeProject + its S3 snapshot), so we let Daytona reclaim idle VMs fast rather
# than lean only on our own reaper.
#   • stop 5 min after inactivity, • archive 5 min after a stop,
#   • delete immediately on stop (0). A returning user re-provisions from the
#     durable project (open_project) instead of resuming a stale VM.
_AUTO_STOP_MINUTES = 5
_AUTO_ARCHIVE_MINUTES = 5
_AUTO_DELETE_MINUTES = 0

# Daytona boot timeout (seconds) — block until the VM is ``started`` before clone.
_BOOT_TIMEOUT_SECONDS = 120.0

# Auto-feature-branch (WC-5a): the repo is checked out onto a fresh
# ``paw/edit-<8 hex>`` branch in the VM so AI edits never touch the default
# branch. The slug is derived from a uuid4 (no time/random-in-loop concerns) and
# the checkout is a quick git op, so a short timeout is plenty.
_BRANCH_CHECKOUT_TIMEOUT_SECONDS = 30


def _new_edit_branch() -> str:
    """Mint a fresh, collision-safe ``paw/edit-<8 hex>`` branch name."""
    return f"paw/edit-{uuid4().hex[:8]}"

# ---------------------------------------------------------------------------
# Reaper env config.
# ---------------------------------------------------------------------------
_DEFAULT_IDLE_TTL_SECONDS = 1800  # 30 min
_DEFAULT_REAP_INTERVAL_SECONDS = 300  # 5 min
_REAPER_TASK_KEY = "_websandbox_reaper_task"

# Max concurrent live (pending/opening/ready) sandboxes a single user may hold.
# Bounds cost/DoS by an authenticated tenant who would otherwise vary the repo URL
# to mint unbounded cold VMs. Re-opening a repo the user already has open does NOT
# count against this (it reuses the existing row). ``0`` disables the cap.
_DEFAULT_MAX_PER_USER = 10
_LIVE_STATUSES = ("pending", "opening", "ready")


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int — falling back to %d", name, raw, default)
        return default
    return max(1, value)


def _idle_ttl_seconds() -> int:
    return _env_int("POCKETPAW_WEBSANDBOX_IDLE_TTL_SECONDS", _DEFAULT_IDLE_TTL_SECONDS)


def _reap_interval_seconds() -> int:
    return _env_int("POCKETPAW_WEBSANDBOX_REAP_INTERVAL_SECONDS", _DEFAULT_REAP_INTERVAL_SECONDS)


def _reaper_enabled() -> bool:
    raw = os.environ.get("POCKETPAW_WEBSANDBOX_REAPER_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _max_per_user() -> int:
    """Per-user concurrent-sandbox cap (0 disables). See ``_DEFAULT_MAX_PER_USER``."""
    return _env_int("POCKETPAW_WEBSANDBOX_MAX_PER_USER", _DEFAULT_MAX_PER_USER)


# ---------------------------------------------------------------------------
# Repo URL validation.
# ---------------------------------------------------------------------------


def _validate_repo_url(repo: str) -> str:
    """Validate ``repo`` is a clean http(s) git URL (no embedded creds), returning it.

    Fail-closed on anything that isn't an ``http``/``https`` URL with a host —
    ``file://``, ``git@`` SSH, ``ssh://``, or bare paths are rejected so the
    provisioner never hands the sandbox a local-path remote. A URL carrying
    embedded credentials (``https://user:token@host/repo``) is rejected too: auth
    is the broker's job (CM-3c mints a repo-scoped token server-side), so a
    client-supplied credential must never be accepted — it would otherwise be
    written into the VM's git remote and persist into snapshots. Private repos are
    supported via the broker, not via credentials in the URL.
    """
    candidate = (repo or "").strip()
    if not candidate:
        raise BadRequest("websandbox.invalid_repo", "A repository URL is required")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BadRequest(
            "websandbox.invalid_repo",
            "Repository must be an http(s) URL (e.g. https://github.com/owner/repo.git)",
        )
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise BadRequest(
            "websandbox.invalid_repo",
            "Repository URL must not embed credentials — private repos connect via GitHub",
        )
    return candidate


def _require_client(client: DaytonaClient | None) -> DaytonaClient:
    """Resolve the Daytona client, raising a clean CloudError when unconfigured.

    ``get_daytona_client()`` returns ``None`` when the Daytona keys are unset;
    that is an operational condition, not a bug, so it surfaces as a 503-style
    CloudError rather than an ``AttributeError`` crash downstream.
    """
    resolved = client if client is not None else get_daytona_client()
    if resolved is None:
        raise CloudError(
            503,
            "websandbox.daytona_unavailable",
            "The sandbox runtime is not configured",
        )
    return resolved


# ---------------------------------------------------------------------------
# open flow.
# ---------------------------------------------------------------------------


async def open_sandbox(
    workspace_id: str,
    user_id: str,
    body: OpenSandboxRequest | dict,
    client: DaytonaClient | None = None,
) -> WebSandboxView:
    """Cold-provision a Daytona VM and clone a repo into it.

    Flow: upsert the registry row (``pending``) → mark ``opening`` →
    ``create_sandbox(auto_stop 5 / archive 5 / delete-on-stop)`` → ``wait_for_sandbox`` →
    clone the repo into the project dir — via the broker (server-side,
    token-isolated) when a connection can authenticate it (CM-3c), else the
    credential-free public in-VM ``git_clone`` → bind the Daytona sandbox id +
    mark ``ready`` → return the ready view.

    On any failure after the row exists, the row is advanced to ``stopped`` and a
    ``CloudError`` is raised (never left stuck in ``opening``); a VM created before
    the failure is best-effort stopped+deleted so a crash can't leak a live VM.
    """
    body = OpenSandboxRequest.model_validate(body)
    repo_url = _validate_repo_url(body.repo)
    daytona = _require_client(client)

    # Quota + re-open bookkeeping — read the caller's OWN rows once (tenant- and
    # owner-scoped via list_sandboxes). Re-opening a repo already open reuses its
    # row (not a new slot); opening a NEW repo past the per-user live cap is
    # rejected cleanly rather than cold-booting an unbounded number of VMs.
    owned = await websandbox_service.list_sandboxes(workspace_id, user_id)
    existing = next((r for r in owned if r.repo == repo_url), None)
    old_sandbox_id = existing.sandbox_id if existing is not None else None
    cap = _max_per_user()
    if cap and existing is None:
        live = sum(1 for r in owned if r.status in _LIVE_STATUSES)
        if live >= cap:
            raise ConflictError(
                "websandbox.too_many",
                f"You have too many open workspaces ({live}); close one and try again",
            )

    # 1. Upsert the registry row (idempotent on workspace+user+repo) and move it
    #    to ``opening`` so a concurrent reader sees the in-flight state.
    row = await websandbox_service.create_sandbox(
        workspace_id, user_id, {"repo": repo_url, "status": "pending"}
    )
    await websandbox_service.update_status(
        workspace_id, user_id, row.id, {"status": "opening"}
    )

    daytona_id: str | None = None
    branch = _new_edit_branch()
    try:
        # 2. Cold-provision with the aggressive Daytona lifecycle (all MINUTES):
        #    stop after 5 idle, archive 5 after stop, delete immediately on stop.
        info = await daytona.create_sandbox(
            name=f"websandbox-{row.id}",
            auto_stop_interval=_AUTO_STOP_MINUTES,
            auto_archive_interval=_AUTO_ARCHIVE_MINUTES,
            auto_delete_interval=_AUTO_DELETE_MINUTES,
        )
        daytona_id = info.id

        # 3. Block until the VM is booted before cloning.
        await daytona.wait_for_sandbox(
            daytona_id, target_state="started", timeout=_BOOT_TIMEOUT_SECONDS
        )

        # 4. Clone the repo into the sandbox's project dir.
        # Clone INTO the pinned workspace dir (WEBSANDBOX_WORKDIR = /home/daytona)
        # so the file tree, the terminal cwd, and the clone all agree on one
        # directory. get_project_dir() returns /root on this image, which the
        # terminal never opens in — that mismatch is why the tree looked empty.
        #
        # CM-3c: if the caller has a GitHub connection whose installation can reach
        # this repo, clone it via the BROKER — the token is minted + used entirely
        # server-side and NEVER enters the VM (the VM receives files + a token-free
        # git remote). Otherwise, clone the PUBLIC repo in-VM with NO credentials.
        project_dir = WEBSANDBOX_WORKDIR
        await daytona.execute_command(daytona_id, f"mkdir -p {project_dir}")
        scoped = await websandbox_broker.resolve_repo_token(workspace_id, user_id, repo_url)
        if scoped is not None:
            await websandbox_broker.clone_into_vm(
                daytona,
                daytona_id,
                scoped,
                project_dir,
                clean_url=repo_url,
                branch=body.branch,
            )
        else:
            await daytona.git_clone(daytona_id, repo_url, project_dir, branch=body.branch)

        # 4b. Check out a fresh ``paw/edit-<hex>`` branch IN the VM (WC-5a) so
        #     every AI edit lands on an isolated branch, never the checked-out
        #     default. cwd is the pinned workspace dir where the repo was cloned.
        await daytona.execute_command(
            daytona_id,
            f"git checkout -b {branch}",
            cwd=project_dir,
            timeout=_BRANCH_CHECKOUT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — any provisioning failure is handled uniformly
        # Best-effort teardown of a half-created VM so a failure can't leak it.
        if daytona_id is not None:
            with contextlib.suppress(Exception):
                await daytona.delete_sandbox(daytona_id)
        # Never leave the row stuck in ``opening``.
        with contextlib.suppress(Exception):
            await websandbox_service.update_status(
                workspace_id, user_id, row.id, {"status": "stopped"}
            )
        logger.warning("websandbox.open failed for repo=%s row=%s", repo_url, row.id, exc_info=True)
        raise with_cause(
            CloudError(502, "websandbox.provision_failed", "Failed to provision the sandbox"),
            exc,
        ) from exc

    # 5. Bind the Daytona id + the auto-created feature branch, and mark ready.
    ready = await websandbox_service.update_status(
        workspace_id,
        user_id,
        row.id,
        {"status": "ready", "sandbox_id": daytona_id, "branch": branch},
    )
    # Re-open: this row previously pointed at a different VM. Now that the row
    # references the NEW id, nothing references the old one — the idle reaper
    # sweeps rows, not orphaned ids, so tear the old VM down here or it leaks
    # (stopped-but-undeleted forever). Best-effort; a failure just defers cost.
    if old_sandbox_id and old_sandbox_id != daytona_id:
        with contextlib.suppress(Exception):
            await daytona.stop_sandbox(old_sandbox_id)
        with contextlib.suppress(Exception):
            await daytona.delete_sandbox(old_sandbox_id)
    logger.info(
        "websandbox.open ready: row=%s daytona=%s branch=%s repo=%s",
        row.id,
        daytona_id,
        branch,
        repo_url,
    )
    return ready


# ---------------------------------------------------------------------------
# tree flow.
# ---------------------------------------------------------------------------


async def get_tree(
    workspace_id: str,
    user_id: str,
    row_id: str,
    client: DaytonaClient | None = None,
) -> SandboxTreeResponse:
    """Return the (single-level) file tree of a ready sandbox.

    Resolves the row tenant-scoped (``get_sandbox`` raises ``NotFound`` for a
    row the caller doesn't own), then runs the fail-closed ``authorize_sandbox``
    on the bound Daytona id BEFORE any runtime op, then lists the project dir.
    A row that hasn't bound a Daytona id yet (never provisioned / still opening)
    is a 409, not a runtime crash.
    """
    daytona = _require_client(client)

    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")

    # Fail-closed authorization on the Daytona id BEFORE touching the runtime.
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    project_dir = WEBSANDBOX_WORKDIR
    files = await daytona.list_files(row.sandbox_id, project_dir)
    entries = [
        TreeEntryResponse(
            name=f.name,
            isDir=bool(getattr(f, "is_dir", False)),
            size=int(getattr(f, "size", 0) or 0),
        )
        for f in files
    ]
    return SandboxTreeResponse(
        id=row.id,
        sandboxId=row.sandbox_id,
        path=project_dir,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Idle-TTL reaper.
# ---------------------------------------------------------------------------


async def reap_idle_sandboxes(
    client: DaytonaClient | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Run ONE reaper sweep: reclaim rows idle longer than the TTL.

    Finds ``ready``/``opening`` rows whose ``updated_at`` is older than the TTL
    (via ``service.list_reapable_sandboxes`` — a deliberate global-read), stops +
    deletes the bound Daytona VM, then marks the row ``reaped``. A row with no
    bound Daytona id (opening that never got one) is marked ``reaped`` directly.
    A VM whose delete FAILS is left for the next sweep (the row stays live) rather
    than being marked reaped and leaked. Returns the count of rows reaped.
    """
    daytona = _require_client(client)
    reference = now or datetime.now(UTC)
    cutoff = reference - timedelta(seconds=_idle_ttl_seconds())

    candidates = await websandbox_service.list_reapable_sandboxes(cutoff)
    if not candidates:
        return 0

    reaped = 0
    for row in candidates:
        if row.sandbox_id:
            try:
                # stop is best-effort (delete destroys the VM regardless); a
                # failed DELETE means we couldn't reclaim, so skip the row.
                with contextlib.suppress(Exception):
                    await daytona.stop_sandbox(row.sandbox_id)
                await daytona.delete_sandbox(row.sandbox_id)
            except Exception:  # noqa: BLE001 — leave the row for the next sweep
                logger.warning(
                    "websandbox.reaper: failed to delete VM %s (row %s) — retry next sweep",
                    row.sandbox_id,
                    row.id,
                    exc_info=True,
                )
                continue
        marked = await websandbox_service.mark_reaped(row.id)
        if marked is not None:
            reaped += 1

    if reaped:
        logger.info("websandbox.reaper: reaped %d idle sandbox(es)", reaped)
    return reaped


async def _run_reaper_loop() -> None:
    """Background loop body — sweep, sleep, repeat. Per-pass errors are logged
    so one bad sweep can't kill the loop; ``CancelledError`` propagates for a
    clean shutdown."""
    interval = _reap_interval_seconds()
    logger.info(
        "websandbox.reaper: loop started (interval=%ds, idle_ttl=%ds)",
        interval,
        _idle_ttl_seconds(),
    )
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("websandbox.reaper: loop cancelled — exiting")
            raise
        try:
            await reap_idle_sandboxes()
        except Exception:  # noqa: BLE001
            logger.exception("websandbox.reaper: sweep failed")


async def start_websandbox_reaper(app: FastAPI) -> None:
    """Start the idle-TTL reaper. Idempotent — a second start is a no-op. Honors
    ``POCKETPAW_WEBSANDBOX_REAPER_ENABLED`` (default true)."""
    if not _reaper_enabled():
        logger.info("websandbox.reaper: disabled via POCKETPAW_WEBSANDBOX_REAPER_ENABLED")
        return
    existing = getattr(app.state, _REAPER_TASK_KEY, None)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_run_reaper_loop(), name="websandbox-reaper")
    setattr(app.state, _REAPER_TASK_KEY, task)


async def stop_websandbox_reaper(app: FastAPI) -> None:
    """Cancel + await the reaper loop. Safe to call multiple times."""
    task = getattr(app.state, _REAPER_TASK_KEY, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    setattr(app.state, _REAPER_TASK_KEY, None)


__all__ = [
    "get_tree",
    "open_sandbox",
    "reap_idle_sandboxes",
    "start_websandbox_reaper",
    "stop_websandbox_reaper",
]
