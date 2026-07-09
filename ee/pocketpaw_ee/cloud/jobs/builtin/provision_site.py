# ee/pocketpaw_ee/cloud/jobs/builtin/provision_site.py
# Created: 2026-07-09 (feat/dp0-provision-job, DP0-3) — the durable job that takes a
# Dynamic Paw Site from spec to live. A dynamic site can't reach a database until
# this runs: it CREATES the per-tenant Cloudflare D1, BUILDS the site with the real
# database id baked into the emitted wrangler.toml, APPLIES the D1 migration, DEPLOYS
# the Worker with a D1 binding, and MARKS the Site doc provisioned.
#
# Each step is create-before-build ordered and idempotent/resumable:
#   a. load the Site + the pocket's rippleSpec; fail-closed tenancy re-check (the
#      worker already re-checks, this is defense-in-depth) — no dynamic site/spec
#      fails cleanly.
#   Updated 2026-07-09 (fix/provision-cf-client-wedge) — building the CF client moved
#   INSIDE the try. It reads the Cloudflare env and raises when unconfigured; raised
#   outside the try it skipped ``mark_provision_failed`` and left the Site stuck in
#   ``provision_status="provisioning"``, which the publish path's single-flight guard
#   reads as "already in flight" — so every retry no-oped and the site could never be
#   published again. Now an unconfigured Cloudflare fails the job cleanly and the site
#   lands in ``failed``, which a re-publish resets and re-dispatches.
#
#   b. create_database GUARDED on Site.d1_database_id: reuse an already-stored id;
#      else create it and persist the id IMMEDIATELY (status stays ``provisioning``)
#      so a retry reuses the same D1 and never orphans a second one.
#   c. build the site through the sites-service seam, passing the real d1 id.
#   d. apply the D1 migration via the wrangler-migrate helper against the built
#      project dir.
#   e. put_worker with the D1 binding (same dynamic deploy as _deploy_site_doc).
#   f. finalize the Site doc (provisioned + deployed + url); on ANY failure in b–f
#      mark provision_status="failed" (the id already persisted) and re-raise so the
#      worker marks the job failed and un-hangs the button.
#   g. return a STATE-ONLY partial for the builder pocket's UI (the writeback path
#      accepts only ``state``).
#
# The Beanie Site reads/writes funnel through ``sites.service`` seams (the
# import-linter keeps this builtin off the Beanie doc); the build + deploy mechanics
# reuse ``_deploy_site_doc``'s helpers rather than duplicating put_worker/build.

"""Built-in ``provision_site`` job: stand up a Dynamic Paw Site's D1 data plane."""

from __future__ import annotations

from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import d1_migrate
from pocketpaw_ee.sites import service as sites_service


class ProvisionError(RuntimeError):
    """A clean, PII-free provision failure. The worker surfaces ``str(exc)`` into
    broadcast state, so these messages are fixed, safe strings — never raw external
    text or tenant data."""


class ProvisionSiteJob:
    """Provision a Dynamic Paw Site's D1 data plane and take it live.

    Runs under the workspace service identity. Returns a STATE-ONLY partial spec —
    the WORKER owns the ``<action>_status`` flag, so this returns only its domain
    state (the provision outcome), never a status key."""

    name = "provision_site"

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        # a. Fail-closed tenancy re-check (mirrors worker.py) — the Site-doc seams
        # fetch by (workspace, pocket) and trust the workspace we hand them, so
        # re-assert the pocket actually lives in this workspace BEFORE any create /
        # build / deploy / write.
        pocket_workspace = await pockets_service.get_pocket_workspace(pocket_id)
        if pocket_workspace != workspace_id:
            raise ProvisionError("tenancy mismatch: pocket is not in the job's workspace")

        ripple_spec = await pockets_service.get_pocket_ripple_spec(workspace_id, pocket_id)
        if not ripple_spec:
            raise ProvisionError("no rippleSpec for pocket — cannot provision a dynamic site")

        site = await sites_service.load_provision_site(workspace_id, pocket_id)
        if site is None:
            raise ProvisionError("no Site doc to provision — publish the site first")

        site_id = str(site.id)

        try:
            # Building the CF client READS the Cloudflare env and raises when it is
            # unconfigured, so it belongs INSIDE the try: an escaping raise here would
            # skip ``mark_provision_failed`` and strand the Site in ``provisioning``,
            # where ``_provision_dynamic_site``'s single-flight guard no-ops every
            # future publish (site permanently unpublishable).
            cf = sites_service.provision_cf_client()

            # b. create_database GUARDED on the stored id. Reuse an existing D1;
            # else create one and persist the id IMMEDIATELY (status stays
            # ``provisioning``) so a retry reuses it — never orphan a second D1.
            existing_id = (getattr(site, "d1_database_id", "") or "").strip()
            if existing_id:
                d1_database_id = existing_id
            else:
                d1_database_id = await cf.create_database(d1_migrate.database_name(site_id))
                await sites_service.persist_provision_d1_id(site, d1_database_id)

            # c. Build with the real d1 id baked into the emitted wrangler.toml.
            project_dir, bundle = await sites_service.build_provision_bundle(
                site=site,
                ripple_spec=ripple_spec,
                d1_database_id=d1_database_id,
            )

            # d. Apply the D1 migration against the built project (wrangler reads the
            # baked-in database_id from the toml — no --database-id flag).
            await d1_migrate.apply_migrations(site_id, project_dir)

            # e. Deploy the Worker with its D1 binding (same as _deploy_site_doc's
            # dynamic bind).
            await cf.put_worker(
                script_name=site_id,
                bundle=bundle,
                bindings=sites_service.provision_d1_bindings(d1_database_id),
            )

            # f. Mark the Site doc provisioned + live.
            await sites_service.finalize_provisioned_site(
                site, url=sites_service.provision_site_url(site_id)
            )
        except Exception:
            # Any failure in b–f: mark failed (the d1 id, if created, is already
            # persisted so a retry reuses it) and re-raise so the worker marks the
            # job failed and writes the failed-state marker that un-hangs the button.
            await sites_service.mark_provision_failed(site)
            raise

        # g. State-only partial for the builder pocket's UI (writeback accepts only
        # ``state``; the worker stamps the generic ``<action>_status`` flag itself).
        return {"state": {"provision_status": "done"}}


__all__ = ["ProvisionSiteJob", "ProvisionError"]
