# ee/pocketpaw_ee/sites/workers_deploy.py — deploy a STATIC Paw Site as a regular
# Cloudflare Worker on the FREE tier (workers.dev) via `wrangler deploy`.
#
# Created: 2026-06-25 (feat/sites-workers-deploy-mode) — the "workers" deploy mode.
#
# This is the third deploy target for a Paw Site, beside the LOCAL static server
# (local_server.deploy_local — dev/smoke) and Workers-for-Platforms
# (cloudflare_client.put_worker — the multi-tenant dispatch namespace). The
# "workers" mode publishes a static site as an ordinary Worker on a free
# workers.dev subdomain, which needs NO WfP dispatch namespace, NO D1, NO Queue —
# just a Cloudflare account token. It is the cheapest path to a real public URL and
# the one PROVEN live by the manual recipe this module automates.
#
# THE RECIPE (verified live — a real paw site serves on workers.dev with exactly
# this). After the generator emits ``<project>/.svelte-kit/cloudflare/`` (the
# adapter-cloudflare static output: ``_worker.js`` + the ``_app/...`` asset tree),
# two files make it deployable to workers.dev with wrangler 4.x:
#   1. ``<project>/.svelte-kit/cloudflare/.assetsignore`` — REQUIRED. wrangler 4.x
#      HARD-ERRORS ("Uploading a Pages _worker.js file as an asset") if the worker
#      entry sits inside the asset dir without it, and adapter-cloudflare does NOT
#      emit it. Contents are exactly the three lines ``_worker.js`` / ``_routes.json``
#      / ``_headers``.
#   2. ``<project>/wrangler.jsonc`` — the clean static config: ``main`` →
#      ``.svelte-kit/cloudflare/_worker.js``, ``assets.directory`` → the SAME dir,
#      ``workers_dev: true``, ``nodejs_compat``. We do NOT reuse the generator's own
#      ``wrangler.toml`` (it stamps an UNDERSCORE name variant + D1/Queue bindings
#      for the dynamic path — invalid for a clean static worker).
# Deploy: ``bunx wrangler@4.101.0 deploy`` with ``CLOUDFLARE_API_TOKEN`` +
# ``CLOUDFLARE_ACCOUNT_ID`` in the env. wrangler prints the deployed
# ``https://<name>.<account-subdomain>.workers.dev`` URL on stdout; we parse it
# (falling back to constructing it from PAW_CF_WORKERS_SUBDOMAIN if the parse fails).
#
# HARD RULES from the live test:
#   * Worker/subdomain names MUST match ``^[a-z0-9][a-z0-9-]*$`` (lowercase,
#     hyphens; NO underscores). The site_id is a 24-hex ObjectId (already
#     lowercase-safe), so the name is ``paw-site-<site_id>``. ``_sanitize`` guards a
#     non-conforming id so a bad id can never produce an invalid name.
#   * STATIC sites ONLY. A DYNAMIC site (pattern=="dynamic", or a spec carrying
#     live bindings) needs a per-tenant D1 + Queues provisioned — that is Phase 2.
#     In workers mode a dynamic site raises a clean ``ValidationError`` rather than
#     deploying a broken site that can't reach its data.
#
# The wrangler invocation is overridable via PAW_CF_WRANGLER_CMD (default
# ``bunx wrangler@4.101.0``) so it is pinnable, ``shlex.split`` like
# PAW_SITES_GEN_CMD. The subprocess style mirrors generator_client.py
# (``asyncio.create_subprocess_exec``, captured stdout/stderr, non-zero → raise
# with the stderr tail).

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from pocketpaw_ee.cloud._core.errors import Internal, ValidationError
from pocketpaw_ee.sites._wrangler import wrangler_argv as _wrangler_argv

logger = logging.getLogger(__name__)

# The static-output dir adapter-cloudflare emits, relative to the project dir. The
# worker entry AND the asset tree both live here; ``main`` and ``assets.directory``
# in the generated wrangler.jsonc both point at it.
_CF_OUTPUT_REL = ".svelte-kit/cloudflare"

# The REQUIRED .assetsignore contents (exact three lines, no trailing blank). Drops
# the Pages-style worker/routing/header files so wrangler 4.x does not try to upload
# the worker entry as a static asset (which it HARD-ERRORS on). adapter-cloudflare
# does not emit this file, so the deploy step writes it.
_ASSETSIGNORE_LINES = ("_worker.js", "_routes.json", "_headers")

# wrangler reads the worker name + name-segment of the workers.dev URL from
# ``wrangler.jsonc``'s ``name``. It must match this (lowercase alnum + hyphen,
# leading alnum). The recipe names a site ``paw-site-<site_id>``; the 24-hex
# ObjectId is already lowercase-safe, so this is really a guard against a
# non-ObjectId id slipping through.
_WORKER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# The pinned wrangler invocation. Overridable (and ``shlex.split``) via
# The wrangler invocation (PAW_CF_WRANGLER_CMD, default `bunx wrangler@4.101.0`) is
# resolved by the shared, Windows-safe helper (imported at top as _wrangler_argv) so
# deploy + D1-migrate share one seam. See _wrangler.py for the `bunx`->`bun x` rewrite
# that keeps it launchable on Windows.


def _sanitize(raw: str) -> str:
    """Coerce an arbitrary id into a valid worker-name SEGMENT: lowercase, with any
    run of disallowed chars (incl. underscores) collapsed to a single hyphen, and
    leading/trailing hyphens stripped. A 24-hex ObjectId passes through unchanged
    (already lowercase + hyphen-free); this only ever bites a non-ObjectId id. Falls
    back to ``"site"`` if sanitizing leaves an empty string, so the name is never
    invalid."""
    s = raw.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "site"


def _worker_name(site_id: str) -> str:
    """The worker / workers.dev subdomain name for a site: ``paw-site-<site_id>``,
    sanitized so it always matches ``^[a-z0-9][a-z0-9-]*$`` (CF rejects underscores
    + uppercase). The ``paw-site-`` prefix starts with a letter, so the leading-char
    rule holds regardless of the (sanitized) id."""
    return f"paw-site-{_sanitize(site_id)}"


def _wrangler_jsonc(name: str) -> str:
    """The clean STATIC-site wrangler config (the proven recipe). ``main`` points at
    the worker entry adapter-cloudflare emits INSIDE the asset dir, and
    ``assets.directory`` points at that SAME dir (the ``.assetsignore`` we write
    keeps wrangler from uploading the worker entry as an asset). ``workers_dev:
    true`` publishes to the free ``<name>.<subdomain>.workers.dev`` URL;
    ``nodejs_compat`` is what the SvelteKit worker needs at runtime. Deliberately
    minimal — NO D1 / Queue bindings (those are the dynamic-site Phase-2 path, and
    the generator's own wrangler.toml that carries them is NOT reused here)."""
    config = {
        "name": name,
        "main": f"{_CF_OUTPUT_REL}/_worker.js",
        "compatibility_date": "2024-09-23",
        "compatibility_flags": ["nodejs_compat"],
        "workers_dev": True,
        "assets": {"binding": "ASSETS", "directory": _CF_OUTPUT_REL},
    }
    return json.dumps(config, indent=2) + "\n"


# The workers.dev URL wrangler prints on a successful deploy, e.g.
# ``https://paw-site-abc.my-acct.workers.dev``. We parse the FIRST such URL out of
# stdout; the fallback constructs it from PAW_CF_WORKERS_SUBDOMAIN when the parse
# fails (a wrangler output format change must not lose the URL).
_WORKERS_DEV_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9.-]*\.workers\.dev\S*")


def _parse_deploy_url(stdout: str, *, name: str) -> str:
    """Pull the deployed ``https://...workers.dev`` URL out of wrangler's stdout.

    wrangler prints the published URL on success; we return the FIRST workers.dev
    URL it emits. When the parse fails (no match — e.g. a wrangler output-format
    change), construct the canonical URL from the worker name + the account's
    workers.dev subdomain (PAW_CF_WORKERS_SUBDOMAIN). The fallback returns ``""``
    only when the subdomain is also unconfigured, so the caller still gets a
    successful deploy (just without a resolved URL to store)."""
    m = _WORKERS_DEV_URL_RE.search(stdout)
    if m:
        return m.group(0).rstrip(".,)\"'")
    subdomain = os.environ.get("PAW_CF_WORKERS_SUBDOMAIN", "").strip()
    if subdomain:
        return f"https://{name}.{subdomain}.workers.dev"
    return ""


def _write_deploy_files(project_dir: str, name: str) -> None:
    """Write the two files the recipe needs into the project: the REQUIRED
    ``.svelte-kit/cloudflare/.assetsignore`` (exact three lines) and the clean
    static ``wrangler.jsonc`` at the project root. Both are overwritten on a
    re-publish so a stale config can never linger."""
    out_dir = Path(project_dir, _CF_OUTPUT_REL)
    if not out_dir.is_dir():
        # The static output must exist before this runs — generator.build() emits it.
        # If it is missing the deploy can't proceed (nothing to upload).
        raise ValidationError(
            "sites.workers_no_build",
            "The static build output is missing — the site must be built before a workers deploy.",
        )
    (out_dir / ".assetsignore").write_text("\n".join(_ASSETSIGNORE_LINES) + "\n")
    Path(project_dir, "wrangler.jsonc").write_text(_wrangler_jsonc(name))


def _cf_env() -> dict[str, str]:
    """The subprocess env for wrangler: the current env plus the two CF vars
    wrangler reads itself (``CLOUDFLARE_API_TOKEN`` + ``CLOUDFLARE_ACCOUNT_ID``).
    We pass the full ``os.environ`` through (so PATH / HOME / bun caches resolve)
    and let wrangler pick up the CF creds from it. The vars are NOT logged."""
    return dict(os.environ)


async def deploy_workers(site_id: str, project_dir: str) -> str:
    """Deploy a STATIC Paw Site as a regular Worker on the free workers.dev tier.

    Writes the proven recipe files (``.assetsignore`` + ``wrangler.jsonc``) into the
    already-built project, then runs ``wrangler deploy`` (PAW_CF_WRANGLER_CMD, default
    ``bunx wrangler@4.101.0``) with the CF creds in the env, and returns the deployed
    ``https://<name>.<subdomain>.workers.dev`` URL (parsed from wrangler's stdout,
    falling back to constructing it from PAW_CF_WORKERS_SUBDOMAIN).

    The project MUST already carry the generator's ``.svelte-kit/cloudflare/``
    static output (generator.build() emits it before this is called). On a non-zero
    wrangler exit this raises ``Internal`` with the stderr tail so the failure
    surfaces as a clean 5xx envelope, not an opaque crash.

    NOTE: dynamic-site rejection is the CALLER's job (the service deploy-mode
    selector knows the pattern). This function deploys whatever static output it is
    given — it does not re-classify the site."""
    name = _worker_name(site_id)
    if not _WORKER_NAME_RE.match(name):  # defensive — _worker_name always sanitizes
        raise ValidationError(
            "sites.workers_bad_name",
            f"Computed an invalid worker name from site id {site_id!r}.",
        )
    _write_deploy_files(project_dir, name)

    argv = [*_wrangler_argv(), "deploy"]
    logger.info("sites.workers: deploying %s via %s", name, argv[:-1])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=project_dir,
            env=_cf_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        # wrangler / bunx not on PATH — a misconfigured deploy image.
        raise Internal(
            "sites.workers_wrangler_missing",
            "The wrangler toolchain is unavailable — a workers-mode deploy needs "
            "wrangler reachable (bunx pulls it at publish time).",
        ) from exc
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if proc.returncode != 0:
        tail = (stderr or stdout)[-800:]
        logger.error("sites.workers: wrangler deploy failed (exit %s): %s", proc.returncode, tail)
        raise Internal(
            "sites.workers_deploy_failed",
            f"wrangler deploy failed (exit {proc.returncode}): {tail}",
        )
    url = _parse_deploy_url(stdout, name=name)
    logger.info("sites.workers: deployed %s -> %s", name, url or "(url unresolved)")
    return url


__all__ = ["deploy_workers"]
