# ee/pocketpaw_ee/sites/workers_deploy.py — deploy a STATIC Paw Site as a regular
# Cloudflare Worker on the FREE tier (workers.dev) via `wrangler deploy`.
#
# Created: 2026-06-25 (feat/sites-workers-deploy-mode) — the "workers" deploy mode.
#
# Updated 2026-07-12 (feat/sites-html-assets-only-deploy, HE-4) — the deploy is now
# ENGINE-AWARE. An ``engine="html"`` site deploys as an ASSETS-ONLY Worker: the
# generator emits a raw static tree (``static_output_rel("html") == "."``, no
# ``_worker.js``), so the emitted ``wrangler.jsonc`` drops ``main`` + ``nodejs_compat``
# and just serves ``assets.directory: "."`` — a legal no-``main`` Worker that ships
# ZERO bytes of JavaScript for a form-less brochure. Because the asset dir is the
# project ROOT (which also holds ``wrangler.jsonc``), html writes a config-excluding
# ``.assetsignore`` (``wrangler.jsonc`` + ``.assetsignore``) so wrangler does not serve
# the config as a public asset — wrangler only auto-excludes its config when the asset
# dir is a SUBDIR (Cloudflare static-assets docs). ripple/svelte keep the exact
# SvelteKit-worker config + the ``_worker.js`` ``.assetsignore`` (the default engine is
# ``"ripple"``, so every existing caller is byte-for-byte unchanged).
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
from pocketpaw_ee.sites.engines import needs_node_build, static_output_rel

logger = logging.getLogger(__name__)

# The static-output dir adapter-cloudflare emits for a ripple/svelte build, relative
# to the project dir. The worker entry AND the asset tree both live here. Retained as
# a named constant for readability; the config is now driven off
# ``static_output_rel(engine)`` (HE-4), which resolves to this for ripple/svelte and
# to ``"."`` for html (whose raw static tree IS the project dir).
_CF_OUTPUT_REL = ".svelte-kit/cloudflare"

# The REQUIRED .assetsignore contents for a ripple/svelte (server-worker) deploy —
# exact three lines, no trailing blank. Drops the Pages-style worker/routing/header
# files so wrangler 4.x does not try to upload the worker entry as a static asset
# (which it HARD-ERRORS on). adapter-cloudflare does not emit this file, so the
# deploy step writes it.
_ASSETSIGNORE_LINES = ("_worker.js", "_routes.json", "_headers")

# The .assetsignore contents for an html deploy. An html site's ``assets.directory``
# is the project ROOT (``static_output_rel("html") == "."``), which also holds the
# deploy scaffold — ``wrangler.jsonc`` and this ``.assetsignore`` — so wrangler would
# otherwise upload the config file as a public asset (it only auto-excludes the config
# when the asset dir is a SUBDIR, per the Cloudflare static-assets docs). There is no
# ``_worker.js`` on this path; the exclusion is the config files, not a server entry.
_HTML_ASSETSIGNORE_LINES = ("wrangler.jsonc", ".assetsignore")

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


# The D1 binding name a dynamic site's remote functions read (``platform.env.DB``).
# Must match the WfP path's binding so the SAME built worker runs on either target.
_D1_BINDING_NAME = "DB"

# We write our own config, but a DYNAMIC project dir ALSO carries the generator's
# wrangler.toml (name + d1 + queue producers, aimed at the WfP upload). Two configs
# in one dir is ambiguous, so every wrangler invocation names ours explicitly.
_CONFIG_FILENAME = "wrangler.jsonc"


def _worker_name(site_id: str) -> str:
    """The worker / workers.dev subdomain name for a site: ``paw-site-<site_id>``,
    sanitized so it always matches ``^[a-z0-9][a-z0-9-]*$`` (CF rejects underscores
    + uppercase). The ``paw-site-`` prefix starts with a letter, so the leading-char
    rule holds regardless of the (sanitized) id."""
    return f"paw-site-{_sanitize(site_id)}"


def _wrangler_jsonc(name: str, engine: str = "ripple", d1_database_id: str | None = None) -> str:
    """The clean wrangler config for a workers.dev deploy (the proven recipe).

    HE-4 — the config is ENGINE-AWARE:

    * html (``not needs_node_build``) → an ASSETS-ONLY Worker. The generator emits a
      raw static tree (no ``_worker.js``), so the config drops ``main`` and
      ``nodejs_compat`` and just serves ``assets.directory`` (``static_output_rel`` →
      ``"."``, the project root). An assets-only Worker with no ``main`` is legal —
      ``assets.directory`` is the only required key — and it ships ZERO bytes of
      JavaScript for a form-less brochure.
    * ripple / svelte (``needs_node_build``) → the SvelteKit Cloudflare worker,
      UNCHANGED. ``main`` points at the worker entry adapter-cloudflare emits INSIDE
      the asset dir (``static_output_rel`` → ``.svelte-kit/cloudflare``), and
      ``assets.directory`` points at that SAME dir (the ``.assetsignore`` we write
      keeps wrangler from uploading the worker entry as an asset). ``nodejs_compat``
      is what the SvelteKit worker needs at runtime.

    ``workers_dev: true`` publishes to the free ``<name>.<subdomain>.workers.dev`` URL.

    ``d1_database_id`` (DYNAMIC sites — ripple/svelte only) adds the single ``DB`` D1
    binding — the same binding WfP's ``put_worker`` passes — so a dynamic site's
    remote functions reach their per-tenant database on the FREE tier. Dynamic html is
    out of scope (it has no server runtime), so an html config never carries a binding.

    Deliberately still NO Queue bindings. The generator's own wrangler.toml declares
    ``LEADS_QUEUE`` / ``WRITEBACK_QUEUE`` producers, but Cloudflare Queues is a paid
    feature and those queues are not created on this path — declaring a producer for
    a nonexistent queue fails the deploy outright. Both consumers already degrade
    gracefully on a missing binding (``api/submit`` forwards straight to the capture
    API; ``writeback.ts`` warns and skips), so omitting them costs durability
    buffering, never a dropped lead."""
    output_rel = static_output_rel(engine)
    # html: an assets-only Worker — no server script, so no ``main`` / ``nodejs_compat``
    # and no D1 (dynamic html is out of scope). Just serve the static tree.
    if not needs_node_build(engine):
        config: dict[str, object] = {
            "name": name,
            "compatibility_date": "2024-09-23",
            "workers_dev": True,
            "assets": {"directory": output_rel},
        }
        return json.dumps(config, indent=2) + "\n"

    config = {
        "name": name,
        "main": f"{output_rel}/_worker.js",
        "compatibility_date": "2024-09-23",
        "compatibility_flags": ["nodejs_compat"],
        "workers_dev": True,
        "assets": {"binding": "ASSETS", "directory": output_rel},
    }
    if d1_database_id:
        config["d1_databases"] = [
            {"binding": _D1_BINDING_NAME, "database_name": name, "database_id": d1_database_id}
        ]
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


def _write_deploy_files(
    project_dir: str, name: str, engine: str = "ripple", d1_database_id: str | None = None
) -> None:
    """Write the recipe files into the project: an ``.assetsignore`` inside the asset
    dir + the clean ``wrangler.jsonc`` at the project root. Both are overwritten on a
    re-publish so a stale config can never linger.

    HE-4 — engine-aware. The asset dir is ``static_output_rel(engine)``
    (``.svelte-kit/cloudflare`` for ripple/svelte, ``"."`` for html). The
    ``.assetsignore`` differs by engine:

    * ripple/svelte → drops the Pages worker entry (``_worker.js`` + friends) so
      wrangler does not upload the server entry as a static asset.
    * html → the asset dir IS the project root, which also holds ``wrangler.jsonc`` +
      ``.assetsignore``, so it drops THOSE (the deploy scaffold) — otherwise wrangler
      serves the config file as a public asset.

    ``d1_database_id`` adds the dynamic site's D1 binding to the emitted config
    (ripple/svelte only)."""
    output_rel = static_output_rel(engine)
    out_dir = Path(project_dir, output_rel)
    if not out_dir.is_dir():
        # The static output must exist before this runs — generator.build() emits it.
        # If it is missing the deploy can't proceed (nothing to upload).
        raise ValidationError(
            "sites.workers_no_build",
            "The static build output is missing — the site must be built before a workers deploy.",
        )
    ignore_lines = _ASSETSIGNORE_LINES if needs_node_build(engine) else _HTML_ASSETSIGNORE_LINES
    (out_dir / ".assetsignore").write_text("\n".join(ignore_lines) + "\n")
    Path(project_dir, _CONFIG_FILENAME).write_text(_wrangler_jsonc(name, engine, d1_database_id))


def _cf_env() -> dict[str, str]:
    """The subprocess env for wrangler: the current env plus the two CF vars
    wrangler reads itself (``CLOUDFLARE_API_TOKEN`` + ``CLOUDFLARE_ACCOUNT_ID``).
    We pass the full ``os.environ`` through (so PATH / HOME / bun caches resolve)
    and let wrangler pick up the CF creds from it. The vars are NOT logged."""
    return dict(os.environ)


async def deploy_workers(
    site_id: str, project_dir: str, *, engine: str = "ripple", d1_database_id: str | None = None
) -> str:
    """Deploy a Paw Site as a regular Worker on the free workers.dev tier.

    Writes the recipe files (``.assetsignore`` + ``wrangler.jsonc``) into the
    already-built project, then runs ``wrangler deploy`` (PAW_CF_WRANGLER_CMD, default
    ``bunx wrangler@4.101.0``) with the CF creds in the env, and returns the deployed
    ``https://<name>.<subdomain>.workers.dev`` URL (parsed from wrangler's stdout,
    falling back to constructing it from PAW_CF_WORKERS_SUBDOMAIN).

    HE-4 — ``engine`` selects the config shape. html deploys as an ASSETS-ONLY Worker
    (no ``main`` / ``_worker.js``, ``assets.directory == "."``, and an ``.assetsignore``
    that keeps the config out of the served tree); ripple/svelte deploy the SvelteKit
    Cloudflare worker unchanged. The default (``"ripple"``) preserves the exact prior
    behaviour for every existing caller — svelte resolves to the same
    ``.svelte-kit/cloudflare`` output.

    ``d1_database_id`` (DYNAMIC ripple/svelte sites) binds the site's per-tenant D1 as
    ``DB``, so a dynamic site can serve its live data WITHOUT a Workers-for-Platforms
    dispatch namespace (a paid add-on). The D1 must already exist and be migrated — the
    provision job does both before calling this.

    The project MUST already carry the engine's static output
    (``static_output_rel(engine)`` — generator.build() emits it before this is called).
    On a non-zero wrangler exit this raises ``Internal`` with the stderr tail so the
    failure surfaces as a clean 5xx envelope, not an opaque crash."""
    name = _worker_name(site_id)
    if not _WORKER_NAME_RE.match(name):  # defensive — _worker_name always sanitizes
        raise ValidationError(
            "sites.workers_bad_name",
            f"Computed an invalid worker name from site id {site_id!r}.",
        )
    _write_deploy_files(project_dir, name, engine, d1_database_id)

    # ``--config`` is REQUIRED, not cosmetic: a dynamic project dir also holds the
    # generator's wrangler.toml (whose queue producers reference queues that do not
    # exist on this path), and wrangler must not pick it up.
    argv = [*_wrangler_argv(), "deploy", "--config", _CONFIG_FILENAME]
    logger.info(
        "sites.workers: deploying %s (engine=%s d1=%s) via %s",
        name,
        engine,
        bool(d1_database_id),
        argv,
    )
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
