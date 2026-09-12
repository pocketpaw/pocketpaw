# ee/pocketpaw_ee/sites/pause.py — taking a site off the internet WITHOUT destroying
# it, and putting it back.
#
# Created 2026-09-12 (sites lifecycle wave 4 chunk 14, feat/sites-pause).
#
# PAUSE IS THE DELETE CASCADE'S SERVING HALF WITH THE RECLAIMS LEFT OUT. It runs
# ``delete_cascade.run_cascade`` with a subset of its steps rather than a second
# implementation, so the two can never drift on which delete a wfp site needs, how a
# provider failure is classified into ``"<step>:<cause>"``, or what the ledger means.
#
#   pause   = revoke, routes, hostnames, script
#   delete  = billing, revoke, routes, hostnames, script, d1, r2, records, + the row
#
# The whole feature is the gap between those two lines: nothing a pause does destroys
# data. The D1 keeps its rows, the bucket keeps its images, the leads, the artifacts,
# the pocket, the concierge agent and the Site document all survive untouched.
#
# WHY DELETING THE WORKER IS UNAVOIDABLE, and therefore why resume is not free. A
# site's primary address is its own Worker's ``<name>.<subdomain>.workers.dev`` URL
# (``workers_deploy.worker_name``). There is no API on our Cloudflare client that
# disables that subdomain, so as long as the script is uploaded the page is on the
# public internet. Removing the routes alone would take the CUSTOM domains dark and
# leave the site fully readable at its workers.dev address — a pause that does not
# pause. So the script goes, and resume has to put it back by REDEPLOYING, which is
# the one genuinely expensive half of this feature and the one honest caveat in it.
#
# RESUME IS TWO PHASES, AND THAT IS FORCED BY CLOUDFLARE, not chosen:
#
#   phase 1 (the service's ``resume_site``, synchronous) — re-mint the signed key,
#           hand back the billing clock, mark the row ``resuming``, and enqueue the
#           site's ordinary republish. The republish re-uploads the Worker.
#   phase 2 (``restore_serving``, called from the deploy's completion) — re-create the
#           worker routes and the custom hostnames.
#
# The phases cannot be merged. ``create_worker_route`` names a script, and Cloudflare
# rejects a route naming a script that does not exist — so every route must be written
# AFTER the deploy lands, and the deploy is a build-lane job measured in minutes. A
# single-phase resume would either block an HTTP request on a build or write routes
# that fail, and the second failure is the silent kind: the row would read live with
# every custom domain dark.
#
# WHAT RESUME CANNOT HAND BACK INSTANTLY, said here rather than discovered by a
# customer. Deleting a Cloudflare-for-SaaS custom hostname surrenders its TLS
# certificate. Re-creating it starts validation from the beginning, so a custom domain
# returns as ``pending`` and is dark for as long as Cloudflare takes to re-issue —
# minutes at best, and longer if the customer's DNS has drifted in the meantime. The
# site's own workers.dev URL has no such delay. This asymmetry is why
# ``restore_serving`` records the hostnames it re-created as pending rather than
# announcing the site fully live.
#
# BILLING PAUSES BY DEFERRAL, NEVER BY CANCELLATION, and that is the load-bearing
# billing decision in this file. The rail is the workspace CREDITS rail
# (``billing_rail`` / ``_CREDITS_RAIL``); there is no payment gateway and this package
# is forbidden from naming one (``tests/cloud/sites/test_no_gateway_in_sites.py``).
# ``renewal_sweeper`` charges rows whose ``subscription_status == "active"``, so the
# two obvious options were:
#
#   * clear the status, the way ``_stop_billing`` does for a delete. That stops the
#     money and destroys the subscription — on the credits rail there is nothing to
#     re-activate, so resume would have to BUY the tier again. A pause that charges
#     you to undo it is not reversible, which is the only thing pause is for.
#   * leave the status alone and let it renew. The customer pays full price for a site
#     serving nothing.
#
# Neither. The sweeper SKIPS a paused row, and resume advances ``renewal_date`` by
# exactly the time the site was dark. The subscription survives intact, no charge
# lands for dark days, and the customer's renewal lands where it always would have,
# pushed along by the length of the pause.

"""Pause a site off the internet, and resume it — the reversible answer to delete."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.sites.delete_cascade import (
    OUTCOME_DONE,
    OUTCOME_SKIPPED,
    STEP_HOSTNAMES,
    STEP_REVOKE,
    STEP_ROUTES,
    STEP_SCRIPT,
    CascadeStepFailed,
    _classify,
    run_cascade,
)

logger = logging.getLogger(__name__)

#: The delete cascade's serving half, in the cascade's own order. A SUBSET and never a
#: reordering — see ``run_cascade``'s docstring on why every subset of that order is
#: still correct and a reshuffle would not be.
PAUSE_STEPS: tuple[str, ...] = (STEP_REVOKE, STEP_ROUTES, STEP_HOSTNAMES, STEP_SCRIPT)

#: The field pause writes its ledger to. NOT ``delete_ledger`` — see the Site model's
#: comment on ``pause_ledger`` for the live bug sharing one would create.
PAUSE_LEDGER_FIELD = "pause_ledger"

#: Resume's own ledger keys, prefixed so they cannot collide with a pause step's in the
#: same field. A failed resume keeps them, so the retry skips what it already restored.
STEP_RESUME_ROUTES = "resume:routes"
STEP_RESUME_HOSTNAMES = "resume:hostnames"

#: ``lifecycle_state`` values. ``live`` is the default on every pre-existing row.
STATE_LIVE = "live"
STATE_PAUSING = "pausing"
STATE_PAUSED = "paused"
STATE_RESUMING = "resuming"

#: The states that mean a pause or a resume is MID-FLIGHT. A request arriving while one
#: of these holds is answered with the attempt in progress rather than starting a
#: second one, the same way ``DELETE_IN_FLIGHT_STATUSES`` guards a delete.
IN_FLIGHT_STATES = frozenset({STATE_PAUSING, STATE_RESUMING})


async def run_pause(*, site: Any, deps: Any, save: Any) -> None:
    """Take the site off the internet. Destroys nothing.

    Raises :class:`CascadeStepFailed` at the first failing step, leaving every earlier
    step recorded in ``pause_ledger`` so a re-run resumes rather than repeats — the
    same contract the delete cascade has, because it is literally the same loop.
    """
    await run_cascade(
        site=site,
        deps=deps,
        save=save,
        steps=PAUSE_STEPS,
        ledger_field=PAUSE_LEDGER_FIELD,
    )


def remint_signed_key(site: Any) -> str:
    """Give the site a NEW capture key, and return it. Never the old one back.

    A pause revoked the key by clearing it (``_revoke_key``), and the old value is
    gone from the row — so this cannot restore it and should not want to. A key that
    survived a pause would still be embedded in whatever copies of the old page are
    cached out there, and re-honouring it would mean a site that was off the internet
    for a month accepts ingest signed by a key from before it went dark.

    Same mint as every other place this product makes one (``site_key_<token>``), so a
    resumed site's key is indistinguishable from a freshly published one's. The
    redeploy that resume enqueues is what carries the new key into the page, which is
    the other reason resume cannot skip the deploy even for a site with no domains.
    """
    site.signed_key = f"site_key_{secrets.token_urlsafe(24)}"
    site.revoked = False
    return str(site.signed_key)


def deferred_renewal_date(site: Any, *, now: datetime | None = None) -> datetime | None:
    """``renewal_date`` pushed forward by exactly how long the site was paused.

    THE POINT IS THAT THE CUSTOMER IS NOT CHARGED FOR DARK DAYS, and equally that they
    do not get free ones. A site paused for nine days resumes with nine days added to
    its renewal; the subscription itself was never cancelled, so nothing has to be
    re-bought.

    ``None`` in, ``None`` out — a free site has no renewal to move. A row with no
    ``paused_at`` returns the date UNCHANGED rather than guessing at a window, because
    the failure directions are not symmetric: guessing long gives away service and
    guessing short bills for darkness.
    """
    current = getattr(site, "renewal_date", None)
    started = getattr(site, "paused_at", None)
    if current is None or started is None:
        return current
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:  # pragma: no cover - defensive
        reference = reference.replace(tzinfo=UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    dark = reference - started
    if dark.total_seconds() <= 0:
        # A clock that went backwards, or a resume in the same instant as the pause.
        # Moving the date backwards would bill early, so it does not move.
        return current
    return current + dark


async def restore_serving(*, site: Any, deps: Any, save: Any) -> dict[str, str]:
    """PHASE 2 of a resume: put the routes and the custom hostnames back.

    Called from the publish's completion path, because both of these name a Worker
    script and Cloudflare rejects a route naming a script that does not exist — so
    neither can run until the redeploy has landed. See this module's header.

    Records into ``pause_ledger`` under the ``resume:`` keys, so a resume interrupted
    between the two Cloudflare calls skips the half it finished. Returns the ledger it
    wrote, which is what lets a caller report "there was nothing here" separately from
    "I put it back".

    THE HOSTNAMES COME BACK PENDING, not live. A re-created custom hostname starts TLS
    validation over, so the domain is dark until Cloudflare re-issues — the one thing
    a resume cannot hand back instantly, and the reason the caller must not announce a
    fully live site on the strength of this returning.
    """
    ledger: dict[str, str] = dict(getattr(site, PAUSE_LEDGER_FIELD, None) or {})

    async def record(step: str, outcome: str) -> None:
        ledger[step] = outcome
        setattr(site, PAUSE_LEDGER_FIELD, dict(ledger))
        await save(site)

    # HOSTNAME FIRST, INVERTING THE CASCADE'S ORDER. The teardown takes the route
    # away before the hostname because that is the order that makes every partial
    # failure LESS live. Putting a site back has the opposite requirement, so the
    # binding that merely resolves is created before the route that actually serves;
    # a hostname with no route reaches the fallback origin, which is a dead end
    # rather than somebody else's site.
    for step, runner in (
        (STEP_RESUME_HOSTNAMES, _restore_hostnames),
        (STEP_RESUME_ROUTES, _restore_routes),
    ):
        if step in ledger:
            continue
        try:
            outcome = await runner(site=site, deps=deps)
        except CascadeStepFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            logger.warning("sites.resume step %s failed: %s", step, exc, exc_info=True)
            raise CascadeStepFailed(step, _classify(exc)) from exc
        await record(step, outcome)
    return ledger


async def _restore_hostnames(*, site: Any, deps: Any) -> str:
    """Re-create the Cloudflare-for-SaaS custom hostnames the pause surrendered.

    FIRST of the two, inverting the cascade's order on purpose. The cascade takes the
    route away before the hostname because that is the order that makes every partial
    failure LESS live; putting a site back has the opposite requirement, so the
    binding that merely resolves is created before the route that actually serves. A
    hostname with no route reaches the fallback origin, which is a dead end rather
    than somebody else's site.

    Each one comes back ``pending``: the certificate went with the old binding and
    validation starts over. Writing the real status rather than optimistically
    restoring ``live`` is what keeps the Domains panel honest while the cert is
    re-issued — it already polls ``get_hostname_status`` for exactly this state.
    """
    from pocketpaw_ee.cloud.billing import site_plans

    domains = list(getattr(site, "domains", None) or [])
    if not domains:
        return OUTCOME_SKIPPED
    # The SAME tier resolution ``add_domain`` does, and for the same reason its
    # comment gives: a tier the catalog does not recognise resolves to an empty set
    # and provisions nothing, rather than handing back WAF and edge-cache controls
    # nobody billed the site for. A resume must not upgrade a site by accident.
    plan = site_plans.site_scoped_tier(getattr(site, "plan_tier", None))
    features = set(plan.cloudflare_features) if plan else set()
    for domain in domains:
        created = await deps.cloudflare.create_custom_hostname(domain.hostname, features=features)
        domain.cf_hostname_id = created.id
        domain.cname_target = created.cname_target
        # The REAL mapped status, not an assumed "pending". Cloudflare decides how
        # far along a re-created hostname is, and a hardcoded value here would be
        # wrong in whichever direction the account's settings make it wrong.
        domain.status = created.status.value
    return OUTCOME_DONE


async def _restore_routes(*, site: Any, deps: Any) -> str:
    """Re-point every custom domain's worker route at the redeployed script.

    ``_route_target`` is asked what the site ACTUALLY has, exactly as
    ``_delete_script`` asks ``deploy_target`` rather than the configured mode — a wfp
    site is not route-addressable at all, and writing a route naming a script
    Cloudflare cannot find is how a resume reports success over a dark domain.
    """
    from pocketpaw_ee.sites import service as sites_service

    domains = list(getattr(site, "domains", None) or [])
    script = sites_service._route_target(site)
    if not domains or not script:
        return OUTCOME_SKIPPED
    for domain in domains:
        domain.cf_route_id = await deps.cloudflare.create_worker_route(
            pattern=sites_service._route_pattern(domain.hostname), script=script
        )
    return OUTCOME_DONE


__all__ = [
    "IN_FLIGHT_STATES",
    "PAUSE_LEDGER_FIELD",
    "PAUSE_STEPS",
    "STATE_LIVE",
    "STATE_PAUSED",
    "STATE_PAUSING",
    "STATE_RESUMING",
    "STEP_RESUME_HOSTNAMES",
    "STEP_RESUME_ROUTES",
    "deferred_renewal_date",
    "remint_signed_key",
    "restore_serving",
    "run_pause",
]
