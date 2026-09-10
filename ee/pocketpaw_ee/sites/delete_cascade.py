# ee/pocketpaw_ee/sites/delete_cascade.py — the ordered, idempotent teardown of one
# published site.
#
# Created 2026-09-09 (sites lifecycle wave 1 chunk 3, feat/sites-delete-cascade).
#
# THE ORDER IS THE DESIGN. Every step here can fail, and the sequence is chosen so
# that a failure at ANY point leaves the site LESS live than before and never still
# charging:
#
#   1. billing off   — a failed teardown must never keep charging the customer;
#   2. auth off      — the signed key dies next, at near-zero cost and before
#                      anything irreversible, so lead ingest and the concierge stop
#                      even if every later step fails;
#   3. serving off   — routes, hostnames, then the Worker script itself;
#   4. reclaim       — D1, R2, artifacts: the things that cost money once nothing
#                      serves;
#   5. records       — the dependent rows;
#   6. the Site doc  — LAST, and not by convention.
#
# The Site document carries the ledger (``delete_ledger``), so it is the only thing
# that knows which steps finished. Deleting it earlier would make every subsequent
# failure both unresumable and invisible — the ordering is a consequence of where
# the ledger lives.
#
# EVERY STEP IS SKIPPED IF THE LEDGER ALREADY RECORDS IT. That is what makes a
# resume a skip rather than a retry, which matters because several steps are
# idempotent in fact but not in spirit: cancelling a subscription twice, or
# re-running a purge, is not free even when it is safe.
#
# WHAT THIS DELIBERATELY DOES NOT DO: it does not take the export. The export is a
# PRECONDITION resolved before the cascade is ever enqueued, because an export taken
# inside the cascade could fail after the first destructive step has already run.
"""The delete cascade: tear one site down, in an order that survives failure."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ledger keys, in execution order. Named rather than positional so a step inserted
# later cannot silently renumber a half-finished cascade's record of itself.
STEP_BILLING = "billing"
STEP_REVOKE = "revoke"
STEP_ROUTES = "routes"
STEP_HOSTNAMES = "hostnames"
STEP_SCRIPT = "script"
STEP_D1 = "d1"
STEP_R2 = "r2"
STEP_RECORDS = "records"

CASCADE_STEPS: tuple[str, ...] = (
    STEP_BILLING,
    STEP_REVOKE,
    STEP_ROUTES,
    STEP_HOSTNAMES,
    STEP_SCRIPT,
    STEP_D1,
    STEP_R2,
    STEP_RECORDS,
)

# Recorded in the ledger when a step had nothing to do — a static site's D1, a site
# with no custom domains, a free site's subscription. Distinct from "done" on
# purpose: "there was nothing here" and "I removed it" are different facts, and an
# operator reading a stalled cascade needs to tell them apart.
OUTCOME_DONE = "done"
OUTCOME_SKIPPED = "nothing-to-do"
# A row that predates the credits rail. Its local renewal is stopped like any
# other, but it may still hold a subscription on the payment gateway that this
# package is structurally forbidden from touching (see
# tests/cloud/sites/test_no_gateway_in_sites.py) — so an operator has to close that
# out of band. Recorded rather than raised: refusing would trap the customer's site
# over a charge this code could not stop either way, and leaving it silent would
# bill them for a site that no longer exists.
OUTCOME_LEGACY_RAIL = "legacy-rail-needs-operator"


class CascadeStepFailed(Exception):
    """One step failed. Carries ``"<step>:<cause>"`` for ``Site.delete_reason``."""

    def __init__(self, step: str, cause: str) -> None:
        self.step = step
        self.cause = cause
        super().__init__(f"{step}:{cause}")

    @property
    def reason(self) -> str:
        return f"{self.step}:{self.cause}"


# The rail a site's money runs on. "credits" is the only one this product charges
# on now; anything else is a row from before 2026-09-05.
_CREDITS_RAIL = "credits"


async def run_cascade(
    *,
    site: Any,
    deps: Any,
    save: Any,
) -> None:
    """Tear the site down in order, recording each step in ``site.delete_ledger``.

    ``deps`` supplies the side effects (cancel_subscription, cloudflare, assets,
    purge_records) so the ordering and the ledger can be tested without Cloudflare,
    a gateway, or a database. ``save`` persists the ledger after every step — a
    ledger only written at the end would record nothing about the crash it exists
    to survive.

    Raises :class:`CascadeStepFailed` at the first failing step, leaving every
    earlier step recorded so a re-run resumes rather than repeats.
    """
    ledger: dict[str, str] = dict(site.delete_ledger or {})

    async def record(step: str, outcome: str) -> None:
        ledger[step] = outcome
        site.delete_ledger = dict(ledger)
        await save(site)

    for step in CASCADE_STEPS:
        if step in ledger:
            # Already done on an earlier attempt. Skipping is the point of the
            # ledger; re-running would re-charge purges and re-cancel subscriptions.
            continue
        try:
            outcome = await _run_step(step, site=site, deps=deps)
        except CascadeStepFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            logger.warning("sites.delete step %s failed: %s", step, exc, exc_info=True)
            raise CascadeStepFailed(step, _classify(exc)) from exc
        await record(step, outcome)


def _classify(exc: Exception) -> str:
    """A short, machine-readable cause. Never raw provider text.

    ``delete_reason`` is surfaced, so it carries a fixed token exactly the way
    ``build_reason`` does. A stderr string here would be the one place a Cloudflare
    or driver message reaches a customer.
    """
    name = type(exc).__name__
    return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_") or "error"


async def _run_step(step: str, *, site: Any, deps: Any) -> str:
    if step == STEP_BILLING:
        return await _stop_billing(site=site, deps=deps)
    if step == STEP_REVOKE:
        return await _revoke_key(site=site, deps=deps)
    if step == STEP_ROUTES:
        return await _delete_routes(site=site, deps=deps)
    if step == STEP_HOSTNAMES:
        return await _delete_hostnames(site=site, deps=deps)
    if step == STEP_SCRIPT:
        return await _delete_script(site=site, deps=deps)
    if step == STEP_D1:
        return await _delete_d1(site=site, deps=deps)
    if step == STEP_R2:
        return await _purge_assets(site=site, deps=deps)
    if step == STEP_RECORDS:
        return await _purge_records(site=site, deps=deps)
    raise CascadeStepFailed(step, "unknown_step")


async def _stop_billing(*, site: Any, deps: Any) -> str:
    """FIRST, so a teardown that fails later never keeps charging the customer.

    STOPPING THE MONEY IS A LOCAL WRITE, NOT A REMOTE CALL. A paid site is charged
    against the workspace's own credit balance, and ``renewal_sweeper`` selects the
    rows it charges on ``subscription_status == "active"`` — so clearing that status
    is precisely what ends the charging. There is nothing to cancel anywhere else:
    the gateway rails were deleted on 2026-09-05, and this package may not even name
    them (``tests/cloud/sites/test_no_gateway_in_sites.py`` asserts it against the
    source, because a reintroduced call would build its own client and sail past any
    injected double).

    A LEGACY ROW STILL GETS ITS LOCAL RENEWAL STOPPED, and is then flagged. Its
    money may run on the old rail, which this code cannot reach and must not try to;
    an operator closes that. Saying so in the ledger is the difference between a
    known follow-up and a customer billed for a site that no longer exists.
    """
    status = getattr(site, "subscription_status", "none")
    if status in ("none", "cancelled"):
        # Not currently paying for anything, so there is nothing to stop.
        return OUTCOME_SKIPPED

    # The local stop, which is the whole mechanism on the credits rail.
    site.subscription_status = "none"
    site.renewal_date = None

    rail = (getattr(site, "billing_rail", "") or "").strip()
    if rail and rail != _CREDITS_RAIL:
        logger.warning(
            "sites.delete: site %s bills on the legacy %r rail; its local renewal is "
            "stopped but any charge on the old rail must be closed by an operator",
            getattr(site, "id", "?"),
            rail,
        )
        return OUTCOME_LEGACY_RAIL
    return OUTCOME_DONE


async def _revoke_key(*, site: Any, deps: Any) -> str:
    """SECOND, and the cheapest meaningful step in the cascade.

    Clearing the signed key stops lead ingest and the concierge immediately. It is
    reversible-ish, costs one write, and happens before anything irreversible — so
    even a cascade that fails at the very next step has already closed the surface
    that accepts data from the public internet.
    """
    if not (getattr(site, "signed_key", "") or ""):
        return OUTCOME_SKIPPED
    site.signed_key = ""
    site.revoked = True
    return OUTCOME_DONE


async def _delete_routes(*, site: Any, deps: Any) -> str:
    routes = [d for d in (getattr(site, "domains", None) or []) if getattr(d, "cf_route_id", "")]
    if not routes:
        return OUTCOME_SKIPPED
    for domain in routes:
        await deps.cloudflare.delete_worker_route(domain.cf_route_id)
    return OUTCOME_DONE


async def _delete_hostnames(*, site: Any, deps: Any) -> str:
    hosts = [d for d in (getattr(site, "domains", None) or []) if getattr(d, "cf_hostname_id", "")]
    if not hosts:
        return OUTCOME_SKIPPED
    for domain in hosts:
        await deps.cloudflare.delete_custom_hostname(domain.cf_hostname_id)
    return OUTCOME_DONE


async def _delete_script(*, site: Any, deps: Any) -> str:
    """THE STEP THAT ACTUALLY STOPS THE PAGE BEING SERVED.

    Which delete to call is decided by ``deploy_target`` — what the last successful
    deploy ACTUALLY did — and not by the configured mode, because that field's own
    comment lists the several ways the two disagree. Sending a wfp site's delete to
    the account-level path (or the reverse) would 404, and 404 is success for these
    calls, so the cascade would record a completed teardown over a live Worker.
    """
    script = (getattr(site, "script_name", "") or "").strip()
    target = (getattr(site, "deploy_target", "") or "").strip()
    if not script or target in ("", "local"):
        # Never deployed, or deployed to the local static server, which owns no
        # Cloudflare Worker to remove.
        return OUTCOME_SKIPPED
    if target == "wfp":
        await deps.cloudflare.delete_worker(script)
    else:
        await deps.cloudflare.delete_account_script(script)
    return OUTCOME_DONE


async def _delete_d1(*, site: Any, deps: Any) -> str:
    """IRREVERSIBLE, and the step the forced export exists for.

    Runs only after the site has stopped serving, so a failure here leaves a
    database that costs money rather than one still reachable from the internet.
    """
    db_id = (getattr(site, "d1_database_id", "") or "").strip()
    if not db_id:
        return OUTCOME_SKIPPED
    await deps.cloudflare.delete_database(db_id)
    return OUTCOME_DONE


async def _purge_assets(*, site: Any, deps: Any) -> str:
    if deps.assets is None:
        # No public rail configured on this deployment, so there is nothing on a
        # bucket to remove. Distinct from an adapter that cannot list, which
        # ``PublicAssetStore.purge`` raises on rather than reporting zero.
        return OUTCOME_SKIPPED
    await deps.assets.purge(workspace_id=site.workspace, pocket_id=site.pocket_id)
    return OUTCOME_DONE


async def _purge_records(*, site: Any, deps: Any) -> str:
    """The dependent rows: leads, transcripts, briefs, counters.

    NOT the Site document — that is deleted by the caller after this returns, so the
    ledger survives every step it records.
    """
    await deps.purge_records(workspace_id=site.workspace, site_id=str(site.id))
    return OUTCOME_DONE
