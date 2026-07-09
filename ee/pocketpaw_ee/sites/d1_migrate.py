# ee/pocketpaw_ee/sites/d1_migrate.py — apply a Dynamic Paw Site's D1 migration
# with wrangler (DP0-3).
#
# Created: 2026-07-09 (feat/dp0-provision-job, DP0-3) — the wrangler-migrate step
# of the durable ``provision_site`` job. After the generator emits a built dynamic
# project (``wrangler.toml`` at the project root with the REAL ``database_id`` baked
# in at build time via ``d1DatabaseId``, and ``migrations/0001_init.sql`` as a
# sibling matching wrangler's default ``migrations_dir``), this runs
# ``wrangler d1 migrations apply paw-site-<siteId> --remote`` against that project
# so the per-tenant D1 gets its schema. NO file patching and NO ``--database-id``
# flag: the toml + migrations layout is spike-confirmed to match wrangler's
# defaults, and the id is already baked into the toml at build time.
#
# It mirrors ``workers_deploy.deploy_workers``' auth + subprocess style exactly —
# ``_wrangler_argv()`` (PAW_CF_WRANGLER_CMD, default ``bunx wrangler@4.101.0``) and
# the CF creds passed through the env (``CLOUDFLARE_API_TOKEN`` /
# ``CLOUDFLARE_ACCOUNT_ID`` off ``os.environ``) — and wraps the subprocess in the
# generator_client reap machinery (``start_new_session=True`` + a bounded
# ``_communicate_bounded`` that SIGKILLs the whole process group on timeout) so a
# wedged wrangler can't hang the jobs worker. A non-zero exit / missing toolchain /
# timeout raises the sites ``Internal`` contract with the stderr tail, so the
# provision job's failure path marks the site ``failed`` and re-raises cleanly.

from __future__ import annotations

import asyncio
import logging
import os

from pocketpaw_ee.cloud._core.errors import Internal
from pocketpaw_ee.sites._wrangler import wrangler_argv as _wrangler_argv
from pocketpaw_ee.sites.generator_client import _BuildTimeout, _communicate_bounded
from pocketpaw_ee.sites.workers_deploy import _sanitize

logger = logging.getLogger(__name__)

# The wrangler invocation (PAW_CF_WRANGLER_CMD, default `bunx wrangler@4.101.0`) is
# resolved by the shared, Windows-safe helper (imported above as _wrangler_argv) — the
# SAME seam workers_deploy uses, so migrate + deploy share one wrangler pin and one
# `bunx`->`bun x` Windows rewrite. See _wrangler.py.

# The migrate subprocess timeout. A legit ``d1 migrations apply`` is seconds; the
# bound is a safety net so a wedged wrangler (e.g. a hung network pull) can't hang
# the jobs worker unbounded. Override with PAW_CF_MIGRATE_TIMEOUT_SEC (int seconds).
_DEFAULT_MIGRATE_TIMEOUT_SEC = 120


def _cf_env() -> dict[str, str]:
    """The subprocess env for wrangler: the full current env (so PATH / HOME / bun
    caches resolve) plus whatever CF creds it already carries
    (``CLOUDFLARE_API_TOKEN`` / ``CLOUDFLARE_ACCOUNT_ID``). Mirrors
    ``workers_deploy._cf_env``; the creds are NEVER logged."""
    return dict(os.environ)


def _migrate_timeout_sec() -> int:
    """The migrate subprocess timeout in seconds (PAW_CF_MIGRATE_TIMEOUT_SEC, int,
    default 120). A malformed / empty value falls back to the default rather than
    raising — the timeout is a safety net and must never itself break a migrate."""
    raw = os.environ.get("PAW_CF_MIGRATE_TIMEOUT_SEC")
    if raw is None:
        return _DEFAULT_MIGRATE_TIMEOUT_SEC
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "sites: ignoring non-int PAW_CF_MIGRATE_TIMEOUT_SEC=%r, using default %ds",
            raw,
            _DEFAULT_MIGRATE_TIMEOUT_SEC,
        )
        return _DEFAULT_MIGRATE_TIMEOUT_SEC


def database_name(site_id: str) -> str:
    """The D1 ``database_name`` the generator bakes into the project's wrangler.toml:
    ``paw-site-<siteId>``. Sanitized identically to the worker name (shared
    ``workers_deploy._sanitize``) so a non-ObjectId id can't yield an invalid
    identifier; a 24-hex ObjectId passes through unchanged. ``wrangler d1 migrations
    apply`` addresses the database by THIS name (it resolves the id from the toml),
    so it must match the emitted ``database_name`` exactly. The provision job also
    creates the CF-side database under this name, so create + migrate agree."""
    return f"paw-site-{_sanitize(site_id)}"


async def apply_migrations(site_id: str, project_dir: str) -> None:
    """Apply a dynamic site's D1 migration via wrangler (DP0-3).

    Runs ``wrangler d1 migrations apply paw-site-<siteId> --remote`` with
    ``cwd=project_dir`` (the built project carrying wrangler.toml + migrations/) and
    the CF creds in the env. The real ``database_id`` is already baked into the toml
    at build time (``d1DatabaseId``) and the migrations dir matches wrangler's
    default, so no ``--database-id`` flag and no file patching are needed.

    The subprocess runs in its own session (``start_new_session=True``) and is bounded
    by ``_communicate_bounded`` (PAW_CF_MIGRATE_TIMEOUT_SEC), which SIGKILLs the whole
    process group on timeout so a wedged wrangler can't hang the jobs worker. A
    missing toolchain, a timeout, or a non-zero exit raises ``Internal`` (→ a clean
    5xx envelope) with the stderr tail — the provision job maps that to
    ``provision_status="failed"`` and re-raises."""
    db_name = database_name(site_id)
    argv = [*_wrangler_argv(), "d1", "migrations", "apply", db_name, "--remote"]
    timeout = _migrate_timeout_sec()
    logger.info("sites.migrate: applying D1 migrations for %s via %s", db_name, argv[:-4])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=project_dir,
            env=_cf_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        # wrangler / bunx not on PATH — a misconfigured deploy image.
        raise Internal(
            "sites.migrate_wrangler_missing",
            "The wrangler toolchain is unavailable — a D1 migrate needs wrangler "
            "reachable (bunx pulls it at publish time).",
        ) from exc
    try:
        stdout_b, stderr_b = await _communicate_bounded(proc, timeout, "d1-migrate")
    except _BuildTimeout as exc:
        raise Internal(
            "sites.migrate_timeout",
            f"D1 migrate timed out after {timeout}s and was killed.",
        ) from exc
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if proc.returncode != 0:
        tail = (stderr or stdout)[-800:]
        logger.error(
            "sites.migrate: wrangler d1 migrations apply failed (exit %s): %s",
            proc.returncode,
            tail,
        )
        raise Internal(
            "sites.migrate_failed",
            f"wrangler d1 migrations apply failed (exit {proc.returncode}): {tail}",
        )
    logger.info("sites.migrate: applied D1 migrations for %s", db_name)


__all__ = ["apply_migrations", "database_name"]
