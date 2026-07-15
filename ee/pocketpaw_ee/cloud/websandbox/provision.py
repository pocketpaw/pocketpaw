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
#      cold-provision a Daytona VM (auto_stop_interval=30 min) → wait for boot →
#      clone the PUBLIC repo (no credentials) → bind the Daytona id + mark ready.
#      On any mid-flight failure the row is marked ``stopped`` and a CloudError is
#      raised — the row is never left stuck in ``opening`` silently, and a
#      half-created VM is best-effort torn down.
#   2. ``get_tree`` — resolve the row (tenant-scoped) → authorize on its Daytona
#      id (fail-closed) → single-level ``list_files`` → the file tree.
#   3. Idle-TTL reaper — a background sweep that reclaims rows whose ``updated_at``
#      is older than the TTL by stopping+deleting the VM and marking the row
#      ``reaped``. Labels are NOT used to reconcile because ``DaytonaClient.
#      create_sandbox`` does not forward labels to the SDK; the Registry (our DB)
#      is the source of truth instead. WC-3 will refresh ``updated_at`` on live
#      WebSocket traffic; until then "idle" == ``updated_at`` age. The SDK's own
#      ``auto_stop_interval=30`` min is a second, independent backstop.
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

from fastapi import FastAPI

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, ConflictError, with_cause
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxView
from pocketpaw_ee.cloud.websandbox.dto import (
    OpenSandboxRequest,
    SandboxTreeResponse,
    TreeEntryResponse,
)

logger = logging.getLogger(__name__)

# The SDK backstop: a self-stop after 30 minutes of inactivity. NOTE the SDK's
# ``auto_stop_interval`` is in MINUTES (the SDK default 3600 = 60 HOURS is a
# latent trap); 30 is a 30-minute backstop that mirrors our idle-TTL reaper.
_AUTO_STOP_MINUTES = 30

# Daytona boot timeout (seconds) — block until the VM is ``started`` before clone.
_BOOT_TIMEOUT_SECONDS = 120.0

# ---------------------------------------------------------------------------
# Reaper env config.
# ---------------------------------------------------------------------------
_DEFAULT_IDLE_TTL_SECONDS = 1800  # 30 min
_DEFAULT_REAP_INTERVAL_SECONDS = 300  # 5 min
_REAPER_TASK_KEY = "_websandbox_reaper_task"


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


# ---------------------------------------------------------------------------
# Repo URL validation.
# ---------------------------------------------------------------------------


def _validate_public_repo_url(repo: str) -> str:
    """Validate ``repo`` is a plausible public http(s) git URL, returning it.

    Fail-closed on anything that isn't an ``http``/``https`` URL with a host —
    ``file://``, ``git@`` SSH, ``ssh://``, or bare paths are rejected so the
    provisioner never hands the sandbox a local-path or credentialed remote.
    """
    candidate = (repo or "").strip()
    if not candidate:
        raise BadRequest("websandbox.invalid_repo", "A repository URL is required")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BadRequest(
            "websandbox.invalid_repo",
            "Repository must be a public http(s) URL (e.g. https://github.com/owner/repo.git)",
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
    """Cold-provision a Daytona VM and clone a PUBLIC repo into it.

    Flow: upsert the registry row (``pending``) → mark ``opening`` →
    ``create_sandbox(auto_stop_interval=30)`` → ``wait_for_sandbox`` →
    ``git_clone`` the public repo (NO credentials) into the project dir →
    bind the Daytona sandbox id + mark ``ready`` → return the ready view.

    On any failure after the row exists, the row is advanced to ``stopped`` and a
    ``CloudError`` is raised (never left stuck in ``opening``); a VM created before
    the failure is best-effort stopped+deleted so a crash can't leak a live VM.
    """
    body = OpenSandboxRequest.model_validate(body)
    repo_url = _validate_public_repo_url(body.repo)
    daytona = _require_client(client)

    # 1. Upsert the registry row (idempotent on workspace+user+repo) and move it
    #    to ``opening`` so a concurrent reader sees the in-flight state.
    row = await websandbox_service.create_sandbox(
        workspace_id, user_id, {"repo": repo_url, "status": "pending"}
    )
    await websandbox_service.update_status(
        workspace_id, user_id, row.id, {"status": "opening"}
    )

    daytona_id: str | None = None
    try:
        # 2. Cold-provision. auto_stop_interval is in MINUTES — 30 is the backstop.
        info = await daytona.create_sandbox(
            name=f"websandbox-{row.id}",
            auto_stop_interval=_AUTO_STOP_MINUTES,
        )
        daytona_id = info.id

        # 3. Block until the VM is booted before cloning.
        await daytona.wait_for_sandbox(
            daytona_id, target_state="started", timeout=_BOOT_TIMEOUT_SECONDS
        )

        # 4. Clone the PUBLIC repo — pass NO credentials (clean; no token ever
        #    involved). Clone into the sandbox's project dir.
        project_dir = await daytona.get_project_dir(daytona_id)
        await daytona.git_clone(daytona_id, repo_url, project_dir, branch=body.branch)
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

    # 5. Bind the Daytona id and mark ready.
    ready = await websandbox_service.update_status(
        workspace_id, user_id, row.id, {"status": "ready", "sandbox_id": daytona_id}
    )
    logger.info("websandbox.open ready: row=%s daytona=%s repo=%s", row.id, daytona_id, repo_url)
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

    project_dir = await daytona.get_project_dir(row.sandbox_id)
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
