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
#
# 2026-07-25 (B3, feat/code-scaffold-on-vm): the open flow split in two. The
# shared half — quota, the idempotent registry row, cold-provision, boot wait,
# the ready/rebind write, old-VM teardown — moved into ``_provision_into_row``;
# what differs is only what gets put INTO the booted VM. ``open_sandbox`` keeps
# the git clone (and keeps ``_validate_repo_url`` on it, unconditionally);
# ``open_bare_sandbox`` adds an EMPTY VM with no clone, no git remote, and no
# edit branch, for a scaffold project whose "repo" is a starter TEMPLATE id.
#
# The split exists so ``_validate_repo_url`` never has to be loosened. A template
# id ("react") is not a URL and never will be; the fix is to keep it off the
# clone path entirely rather than to teach the validator a second, weaker shape.
# That validator is a security control — it is what stops a local-path remote or
# a client-supplied credential reaching the VM's git config (and its snapshots) —
# so anything that would relax it for a non-git caller is the wrong direction.
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, ConflictError, with_cause
from pocketpaw_ee.cloud.codegit import wire as codegit_wire
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


# ---------------------------------------------------------------------------
# VM size.
# ---------------------------------------------------------------------------
# Stated HERE and passed EXPLICITLY, added 2026-07-22. Until now this module
# called ``create_sandbox`` with no resource arguments and silently inherited
# that function's signature defaults — cpu=2, memory=4. Nothing in the Code Mode
# tree said what size a workspace VM was, so the answer lived in a default
# argument three modules away and changed if anyone ever touched that signature.
#
# 1 vCPU / 1 GB is the deliberate floor: enough for `npm install` and a Vite dev
# server, and the size a per-user workspace should cost. Raise MEMORY first if
# installs start getting OOM-killed — node's resolver is the memory-hungry part,
# not the CPU count.
# ``_env_int`` (below, with the reaper config) already does exactly this — read,
# tolerate garbage, floor at 1 — so this reuses it rather than shipping a second
# copy that could drift.
def _vm_resources() -> tuple[int, int, int]:
    """(cpu, memory GB, disk GB) for a workspace VM. Read per call, not at import,
    so a deployment can change the size without a code change."""
    return (
        _env_int("POCKETPAW_WEBSANDBOX_CPU", 1),
        _env_int("POCKETPAW_WEBSANDBOX_MEMORY_GB", 1),
        _env_int("POCKETPAW_WEBSANDBOX_DISK_GB", 10),
    )


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


# The registry key's own bound. It is stored in ``WebSandbox.repo``, whose write
# command (``CreateSandboxRequest.repo``) caps at 1024 — checking it here turns a
# too-long key into a clean 400 instead of a pydantic error from two layers down.
_MAX_REGISTRY_KEY = 1024


def _validate_registry_key(key: str) -> str:
    """Validate a NON-git registry key (the row's ``repo`` slot), returning it.

    Deliberately NOT ``_validate_repo_url``. A bare VM clones nothing, so this
    string is only ever a registry key — the (workspace, user, key) tuple that
    makes ``create_sandbox`` idempotent. There is no URL to parse, no remote to
    write, and no credential that could ride along, so the URL rules would be
    both wrong and unenforceable here.

    That is also why this is not a public write surface: no router binds it. The
    only caller is the scaffold open path, which builds the key server-side from a
    project it has already tenant-checked.
    """
    candidate = (key or "").strip()
    if not candidate:
        raise BadRequest("websandbox.invalid_key", "A workspace key is required")
    if len(candidate) > _MAX_REGISTRY_KEY:
        raise BadRequest("websandbox.invalid_key", "Workspace key is too long")
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

#: What puts CONTENT into a freshly-booted, empty VM, called once with
#: ``(daytona, daytona_id, project_dir)`` after the boot wait and the workdir
#: mkdir. ``open_sandbox`` passes the git clone; a bare open passes nothing.
_FillStage = Callable[[DaytonaClient, str, str], Awaitable[None]]


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

    ``_validate_repo_url`` runs here, on EVERY clone, before anything is
    provisioned. The bare path (``open_bare_sandbox``) exists precisely so that
    stays true — a caller with no repo takes a different door instead of asking
    this one to accept a weaker string.
    """
    body = OpenSandboxRequest.model_validate(body)
    repo_url = _validate_repo_url(body.repo)
    # WC-5a: the repo is checked out onto a fresh branch in the VM so AI edits
    # never touch the default branch. Minted before the fill stage so the same
    # name reaches both the checkout and the row's ``branch`` binding.
    branch = _new_edit_branch()

    async def _clone_into(daytona: DaytonaClient, daytona_id: str, project_dir: str) -> None:
        """Clone the repo, branch it, and (when brokered) wire the push remote."""
        # CM-3c: if the caller has a GitHub connection whose installation can reach
        # this repo, clone it via the BROKER — the token is minted + used entirely
        # server-side and NEVER enters the VM (the VM receives files + a token-free
        # git remote). Otherwise, clone the PUBLIC repo in-VM with NO credentials.
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

        # Check out a fresh ``paw/edit-<hex>`` branch IN the VM (WC-5a) so every AI
        # edit lands on an isolated branch, never the checked-out default. cwd is
        # the pinned workspace dir where the repo was cloned.
        await daytona.execute_command(
            daytona_id,
            f"git checkout -b {branch}",
            cwd=project_dir,
            timeout=_BRANCH_CHECKOUT_TIMEOUT_SECONDS,
        )

        # CM-3d: for a broker-cloned (connected) repo, repoint ``origin`` at the
        # git proxy so an in-VM ``git push``/``fetch`` works with the token still
        # minted server-side (never in the VM). Best-effort — a wiring miss leaves
        # the clone fully usable, it just can't push yet; it must never fail the
        # open. Skipped for public clones (no connection to push through) and when
        # no public backend URL is reachable from the VM.
        if scoped is not None:
            with contextlib.suppress(Exception):
                await codegit_wire.wire_push_remote(
                    daytona,
                    daytona_id,
                    workspace_id,
                    user_id,
                    websandbox_broker.repo_full_name(repo_url) or repo_url,
                    project_dir,
                )

    return await _provision_into_row(
        workspace_id,
        user_id,
        repo_url,
        branch=branch,
        fill=_clone_into,
        client=client,
    )


async def open_bare_sandbox(
    workspace_id: str,
    user_id: str,
    key: str,
    client: DaytonaClient | None = None,
) -> WebSandboxView:
    """Cold-provision an EMPTY Daytona VM — no clone, no git remote, no branch.

    The scaffold half of the open flow (B3). A scaffold project's ``repo`` field
    holds a starter TEMPLATE id, not a URL, so there is nothing to clone and no
    remote to authenticate: the VM boots empty and the caller materializes the
    starter into it (``websandbox.scaffold_service.scaffold_into_sandbox``).

    Everything else is identical to ``open_sandbox`` — the same per-user quota,
    the same idempotent (workspace, user, key) row, the same failure handling, the
    same old-VM teardown on a re-open. ``key`` is the row's registry key only; it
    is server-built by the caller and never reaches git, a URL builder, or a
    shell (see ``_validate_registry_key``).

    Returns the ready view with a bound Daytona id and NO ``branch`` — a VM with
    no repository has no branch to report, and inventing one would make the git
    surface look available on a project that has no git.
    """
    return await _provision_into_row(
        workspace_id,
        user_id,
        _validate_registry_key(key),
        branch=None,
        fill=None,
        client=client,
    )


async def _provision_into_row(
    workspace_id: str,
    user_id: str,
    registry_key: str,
    *,
    branch: str | None,
    fill: _FillStage | None,
    client: DaytonaClient | None,
) -> WebSandboxView:
    """Cold-provision a VM into the (workspace, user, ``registry_key``) row.

    The half both open paths share: quota + re-open bookkeeping, the idempotent
    registry row, the cold-provision with the aggressive Daytona lifecycle, the
    boot wait, the ``ready`` rebind, and the old-VM teardown. ``fill`` is the only
    difference between them — what (if anything) gets put into the booted VM.

    ``registry_key`` is ALREADY validated by the caller: ``open_sandbox`` runs the
    fail-closed ``_validate_repo_url`` on it, the bare path runs
    ``_validate_registry_key``. This function never relaxes either — it does not
    inspect the key at all, it only stores it.
    """
    daytona = _require_client(client)

    # Quota + re-open bookkeeping — read the caller's OWN rows once (tenant- and
    # owner-scoped via list_sandboxes). Re-opening a repo already open reuses its
    # row (not a new slot); opening a NEW repo past the per-user live cap is
    # rejected cleanly rather than cold-booting an unbounded number of VMs.
    owned = await websandbox_service.list_sandboxes(workspace_id, user_id)
    existing = next((r for r in owned if r.repo == registry_key), None)
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
        workspace_id, user_id, {"repo": registry_key, "status": "pending"}
    )
    await websandbox_service.update_status(workspace_id, user_id, row.id, {"status": "opening"})

    daytona_id: str | None = None
    try:
        # 2. Cold-provision with the aggressive Daytona lifecycle (all MINUTES):
        #    stop after 5 idle, archive 5 after stop, delete immediately on stop.
        cpu, memory_gb, disk_gb = _vm_resources()
        logger.info(
            "websandbox: provisioning row=%s at %d vCPU / %d GB RAM / %d GB disk",
            row.id,
            cpu,
            memory_gb,
            disk_gb,
        )
        info = await daytona.create_sandbox(
            name=f"websandbox-{row.id}",
            cpu=cpu,
            memory=memory_gb,
            disk=disk_gb,
            auto_stop_interval=_AUTO_STOP_MINUTES,
            auto_archive_interval=_AUTO_ARCHIVE_MINUTES,
            auto_delete_interval=_AUTO_DELETE_MINUTES,
        )
        daytona_id = info.id

        # 3. Block until the VM is booted before filling it.
        await daytona.wait_for_sandbox(
            daytona_id, target_state="started", timeout=_BOOT_TIMEOUT_SECONDS
        )

        # 4. Fill the sandbox's project dir (a clone, or nothing for a bare VM).
        # The dir is the pinned workspace dir (WEBSANDBOX_WORKDIR = /home/daytona)
        # so the file tree, the terminal cwd, and whatever lands here all agree on
        # one directory. get_project_dir() returns /root on this image, which the
        # terminal never opens in — that mismatch is why the tree looked empty.
        # It is created even on the bare path: the scaffold materializes into it,
        # and the file tree lists it.
        project_dir = WEBSANDBOX_WORKDIR
        await daytona.execute_command(daytona_id, f"mkdir -p {project_dir}")
        if fill is not None:
            await fill(daytona, daytona_id, project_dir)
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
        logger.warning(
            "websandbox.open failed for key=%s row=%s", registry_key, row.id, exc_info=True
        )
        raise with_cause(
            CloudError(502, "websandbox.provision_failed", "Failed to provision the sandbox"),
            exc,
        ) from exc

    # 5. Bind the Daytona id + the auto-created feature branch, and mark ready.
    #    ``branch`` is None on the bare path and ``update_status`` skips a None,
    #    so a bare VM's row simply carries no branch.
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
        "websandbox.open ready: row=%s daytona=%s branch=%s key=%s filled=%s",
        row.id,
        daytona_id,
        branch,
        registry_key,
        fill is not None,
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
    "open_bare_sandbox",
    "open_sandbox",
    "reap_idle_sandboxes",
    "start_websandbox_reaper",
    "stop_websandbox_reaper",
]
