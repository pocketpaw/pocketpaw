# ee/pocketpaw_ee/sites/transfer.py — moving one published site from the workspace
# that owns it to another one, without taking it off the internet.
#
# Created 2026-09-12 (sites lifecycle wave 3, feat/sites-transfer).
#
# TRANSFER IS A RE-KEY, NOT A REDEPLOY, AND THAT IS THE WHOLE DESIGN. A Paw Site is
# reached through a Cloudflare Worker whose SCRIPT NAME is the Site document's
# ``_id``, and served at ``https://<that same id>.<sites domain>``. So the one thing
# a transfer must not do is change that id: doing so renames the live Worker, moves
# the public URL, and orphans every custom-domain route still pointing at the old
# script. What moves is OWNERSHIP — which workspace the records belong to — and
# nothing on Cloudflare is touched at all.
#
# THAT COLLIDES WITH THE STABLE-IDENTITY RULE, AND THE COLLISION IS THE SUBTLE BUG
# THIS MODULE EXISTS TO CLOSE. ``service._live_object_id`` derives the Site ``_id``
# deterministically from ``(workspace, pocket_id)`` — PERF-1, the fix that stopped
# one pocket accumulating fourteen Site rows. After a transfer the workspace half of
# that pair has changed, so the NEXT publish in the destination derives a DIFFERENT
# id: it inserts a SECOND Site document, uploads a SECOND Worker, and serves it at a
# SECOND URL, while every custom domain keeps resolving to the first one. The site
# silently forks, and the half the customer is editing is not the half their domain
# points at.
#
# The fix is ``Site.identity_workspace``: the workspace the id was MINTED from,
# stamped only by a transfer. ``service._resolve_live_site_oid`` prefers the row's
# real id when that stamp says the row has moved, and falls back to the derivation
# for every other row on earth — so a site that has never been transferred takes a
# byte-identical path and the PERF-1 dedupe invariant is untouched.
#
# WHY TWO PHASES. A one-shot ``POST /sites/{id}/transfer {destination}`` authorises
# only the SENDER. That is a hole, not a shortcut: it lets anyone who owns a site
# push it — with its leads, its transcripts and its custom domains — into any
# workspace whose id they can name, and the receiving tenant gets records it never
# asked for. An offer the destination must ACCEPT puts a consenting party on both
# ends of the boundary, which is the only thing that makes this a transfer rather
# than a write into someone else's tenancy.
#
# THE STEP ORDER HERE IS NOT THE DELETE CASCADE'S. Nothing is destroyed, so the
# failure to design against is not "half torn down" but SPLIT OWNERSHIP — the
# records of one site sitting in two tenants at once. Two properties govern it:
#
#   1. the pocket and the Site move TOGETHER, in one step, because the guard that
#      stops a pocket delete stranding a live site finds the site through the
#      POCKET's workspace. A window with those two apart re-opens exactly the orphan
#      wave 2 closed;
#   2. the customer's DATA moves after the ownership record, never before, so the
#      destination is never holding leads for a site it does not yet own.
"""Workspace-to-workspace site transfer: the preconditions, and the ordered re-key."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ledger keys, in execution order. Named rather than positional for the reason
# ``delete_cascade`` names its own: a step inserted later must not silently
# renumber a half-finished transfer's record of itself.
STEP_OWNERSHIP = "ownership"
STEP_RECORDS = "records"
STEP_ASSETS = "assets"
STEP_FINALIZE = "finalize"

TRANSFER_STEPS: tuple[str, ...] = (
    STEP_OWNERSHIP,
    STEP_RECORDS,
    STEP_ASSETS,
    STEP_FINALIZE,
)

OUTCOME_DONE = "done"
OUTCOME_SKIPPED = "nothing-to-do"
# The public assets are NOT copied to the destination's prefix, and this records
# that as a deliberate outcome rather than leaving the step looking unrun. See
# ``_move_assets`` for why copying them would be worse than leaving them.
OUTCOME_ASSETS_RETAINED = "retained-at-source-prefix"

# ``Site.transfer_status`` values. There is no terminal success value for the same
# reason the delete cascade has none: a finished transfer returns the row to
# ``none``, and the fact that it happened is carried by ``transferred_at``.
STATUS_NONE = "none"
STATUS_OFFERED = "offered"
STATUS_IN_FLIGHT = "in_flight"
STATUS_FAILED = "failed"

# The states an accept may run over. ``offered`` is the ordinary one; the other two
# are RESUMES, and leaving them out is what makes a ledger decorative.
#
# A failed transfer has a half-written ledger and is precisely the case the ledger
# exists for, so refusing it would mean ownership stayed split across two tenants
# with no way to finish the move. And ``in_flight`` is here for the crash that
# leaves nothing to clear it: a process that dies mid-transfer pins the row there
# forever, and a one-way door is a worse failure than a redundant pass. This is the
# same asymmetry the delete cascade's single-flight comment argues for — a repeat
# run costs one idempotent sweep over a ledger that skips finished steps, while a
# stuck guard costs the transfer permanently.
#
# A repeat is safe because every step is idempotent and the ledger turns a
# re-entry into a skip. A COMPLETED transfer is not here: success returns the row
# to ``none`` and clears ``transfer_to_workspace``, so the offer cannot be replayed.
RESUMABLE_STATUSES: tuple[str, ...] = (STATUS_OFFERED, STATUS_IN_FLIGHT, STATUS_FAILED)

# A delete that has not started is the only delete state a transfer may run over.
# Anything else means a cascade is queued or mid-flight, and handing a tenant a site
# that is being torn down under them is not a transfer.
_DELETE_TERMINAL = ("none", "")


class TransferRefused(Exception):
    """A precondition said no. Carries a stable code plus a sentence for the user.

    A code AND a message, rather than either alone: the frontend needs to branch on
    the reason (a blocked workspace setting is a different button than an unpaid
    balance), and the person reading the toast needs to know what to do next.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TransferStepFailed(Exception):
    """One step failed. Carries ``"<step>:<cause>"`` for ``Site.transfer_reason``."""

    def __init__(self, step: str, cause: str) -> None:
        self.step = step
        self.cause = cause
        super().__init__(f"{step}:{cause}")

    @property
    def reason(self) -> str:
        return f"{self.step}:{self.cause}"


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
#
# Checked when the offer is MADE and again when it is ACCEPTED, deliberately. An
# offer can sit for days, and every one of these facts can change while it does —
# the site can be published onto a paid tier, a delete can be queued, an admin can
# switch transfers off. Checking only at offer time would let a stale offer carry a
# site past a guard that had since closed.


def check_can_offer(
    *,
    site: Any,
    actor_user_id: str,
    source_settings: Any,
    destination_workspace_id: str,
) -> None:
    """Refuse the offer unless the SOURCE side is entitled to make it.

    ``source_settings`` is the source workspace's ``WorkspaceSettings``. Passed in
    rather than read here so this stays a pure function over the facts.
    """
    dest = (destination_workspace_id or "").strip()
    if not dest:
        raise TransferRefused("transfer.no_destination", "Name the workspace to move this site to.")
    if dest == (getattr(site, "workspace", "") or ""):
        raise TransferRefused(
            "transfer.same_workspace",
            "That site already belongs to this workspace.",
        )

    # OWNER ONLY, matching Netlify and the wave-1 delete guard beside it. This is
    # deliberately NOT widened to workspace admins: the permission layers in this
    # codebase are known to disagree with one another, and the honest place to
    # settle that is its own audit, not the first feature that needs an answer.
    if (getattr(site, "owner", "") or "") != actor_user_id:
        raise TransferRefused(
            "transfer.not_owner",
            "Only the person who owns this site can move it to another workspace.",
        )

    # An admin may forbid transfers OUT entirely. A site leaving a workspace takes
    # its leads and its transcripts with it, so this is a data-egress control and
    # belongs to the workspace, not to the person clicking the button.
    if getattr(source_settings, "site_transfers_allowed", True) is False:
        raise TransferRefused(
            "transfer.blocked_by_workspace",
            "This workspace does not allow sites to be moved out of it. "
            "An admin can change that in workspace settings.",
        )

    if (getattr(site, "delete_status", "none") or "none") not in _DELETE_TERMINAL:
        raise TransferRefused(
            "transfer.deleting",
            "This site is being deleted. It cannot be moved.",
        )

    status = getattr(site, "transfer_status", STATUS_NONE) or STATUS_NONE
    if status == STATUS_OFFERED:
        raise TransferRefused(
            "transfer.already_offered",
            "This site has already been offered to a workspace. "
            "Cancel that offer before making another.",
        )
    if status == STATUS_IN_FLIGHT:
        raise TransferRefused(
            "transfer.in_flight",
            "This site is being moved right now.",
        )

    _check_not_paying(site)


def _check_not_paying(site: Any) -> None:
    """Refuse to move a site somebody is currently paying for.

    THE MONEY DOES NOT MOVE WITH THE SITE, AND IT CANNOT. A paid site is charged
    against the SOURCE workspace's own credit balance — ``renewal_sweeper`` selects
    the rows it charges on ``subscription_status == "active"`` and debits the wallet
    of whatever workspace the row names. Re-key the row and the next renewal silently
    starts charging the DESTINATION's balance for a plan its members never bought;
    leave the row's billing fields behind and the site keeps the tier it has not paid
    for. Both are wrong, and neither is discoverable by the person it happens to.

    So the tier is dropped to the free floor BEFORE the move, by the person who is
    paying for it, as an explicit act. Blocking is the honest failure: it is one
    extra step for the sender and it is the only version of this that cannot
    mis-charge a stranger.

    This package cannot reach a payment gateway to settle it either way — those rails
    were removed on 2026-09-05 and ``tests/cloud/sites/test_no_gateway_in_sites.py``
    asserts their absence against the source text. Refusing is not a limitation of
    that; it is the correct behaviour on the credits rail regardless.
    """
    if (getattr(site, "subscription_status", "none") or "none") == "active":
        raise TransferRefused(
            "transfer.site_is_paid",
            "This site is on a paid plan billed to this workspace. Move it down to "
            "the free plan first, then transfer it — the new workspace can upgrade "
            "it again from its own balance.",
        )


def check_can_accept(
    *,
    site: Any,
    accepting_user_id: str,
    accepting_workspace_id: str,
    member_workspace_ids: tuple[str, ...] | list[str],
) -> None:
    """Refuse the accept unless the DESTINATION side is entitled to take it.

    ``member_workspace_ids`` is every workspace the accepting user actually belongs
    to, read from their own User record. The check is against THAT rather than
    against the request's workspace header: the header says which tenant the caller
    is asking to act as, and a caller who is not a member of it must not be able to
    accept a site into it by naming it.
    """
    status = getattr(site, "transfer_status", STATUS_NONE) or STATUS_NONE
    if status not in RESUMABLE_STATUSES:
        raise TransferRefused(
            "transfer.not_offered",
            "There is no open offer for this site.",
        )

    offered_to = (getattr(site, "transfer_to_workspace", "") or "").strip()
    if not offered_to or offered_to != (accepting_workspace_id or "").strip():
        # Not "forbidden" — NOT FOUND, because saying "that site was offered
        # somewhere else" confirms the site exists to a tenant with no business
        # knowing it does.
        raise TransferRefused(
            "transfer.not_offered",
            "There is no open offer for this site.",
        )

    if accepting_workspace_id not in tuple(member_workspace_ids or ()):
        raise TransferRefused(
            "transfer.not_a_member",
            "You are not a member of the workspace this site was offered to.",
        )

    if (getattr(site, "delete_status", "none") or "none") not in _DELETE_TERMINAL:
        raise TransferRefused(
            "transfer.deleting",
            "This site is being deleted. It cannot be moved.",
        )

    # Re-checked at accept, not only at offer: a site can be published onto a paid
    # tier while the offer sits unanswered, and accepting it then would hand the
    # destination a plan the source is still being charged for.
    _check_not_paying(site)

    if not accepting_user_id:
        raise TransferRefused("transfer.no_actor", "Sign in to accept this site.")


# ---------------------------------------------------------------------------
# The re-key
# ---------------------------------------------------------------------------


async def run_transfer(
    *,
    site: Any,
    destination_workspace_id: str,
    accepting_user_id: str,
    deps: Any,
    save: Any,
) -> None:
    """Move the site to ``destination_workspace_id``, recording each step.

    ``deps`` supplies the side effects (``move_pocket``, ``move_records``, ``now``)
    so the order and the ledger are testable without Mongo. ``save`` persists after
    every step — a ledger written only at the end records nothing about the crash it
    exists to survive.

    Raises :class:`TransferStepFailed` at the first failing step, leaving every
    earlier step recorded so a re-run resumes rather than repeats.
    """
    ledger: dict[str, str] = dict(getattr(site, "transfer_ledger", None) or {})

    async def record(step: str, outcome: str) -> None:
        ledger[step] = outcome
        site.transfer_ledger = dict(ledger)
        await save(site)

    for step in TRANSFER_STEPS:
        if step in ledger:
            continue
        try:
            outcome = await _run_step(
                step,
                site=site,
                destination=destination_workspace_id,
                accepting_user_id=accepting_user_id,
                deps=deps,
            )
        except TransferStepFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            logger.warning("sites.transfer step %s failed: %s", step, exc, exc_info=True)
            raise TransferStepFailed(step, _classify(exc)) from exc
        await record(step, outcome)


def _classify(exc: Exception) -> str:
    """A short, machine-readable cause — never raw driver or provider text.

    ``transfer_reason`` is surfaced to the customer, so it carries a fixed token the
    same way ``build_reason`` and ``delete_reason`` do.
    """
    name = type(exc).__name__
    return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_") or "error"


async def _run_step(
    step: str,
    *,
    site: Any,
    destination: str,
    accepting_user_id: str,
    deps: Any,
) -> str:
    if step == STEP_OWNERSHIP:
        return await _move_ownership(
            site=site, destination=destination, accepting_user_id=accepting_user_id, deps=deps
        )
    if step == STEP_RECORDS:
        return await _move_records(site=site, destination=destination, deps=deps)
    if step == STEP_ASSETS:
        return await _move_assets(site=site, deps=deps)
    if step == STEP_FINALIZE:
        return await _finalize(site=site, accepting_user_id=accepting_user_id, deps=deps)
    raise TransferStepFailed(step, "unknown_step")


async def _move_ownership(*, site: Any, destination: str, accepting_user_id: str, deps: Any) -> str:
    """FIRST, and the pocket and the Site move together inside it.

    THE PAIR IS ATOMIC BY CONSTRUCTION RATHER THAN BY TRANSACTION. The guard that
    stops ``pockets_service.delete`` stranding a live site looks the site up through
    the POCKET's workspace, so a window with the two in different tenants re-opens
    the exact orphan wave 2 closed — in that window either side can delete the pocket
    and leave a Worker serving with nothing able to reach it. Both writes happen
    here, back to back, and the ledger entry is written only once both have. A crash
    between them leaves a resumable state that this step re-enters and completes,
    because both writes are idempotent and both records are found by ``pocket_id``
    regardless of which workspace currently holds them.

    THE OLD TENANT'S PEOPLE LOSE ACCESS HERE TOO. Re-keying the workspace alone would
    move the pocket while leaving ``owner`` and ``shared_with`` pointing at users of
    the workspace it just left, and a pocket read is an ``$or`` over exactly those
    two plus visibility. Carrying them across would be a cross-tenant grant dressed
    up as a field nobody thought about.
    """
    pocket_id = getattr(site, "pocket_id", "") or ""
    if pocket_id:
        await deps.move_pocket(
            pocket_id=pocket_id,
            destination_workspace_id=destination,
            new_owner_id=accepting_user_id,
        )

    # The stamp that stops the next publish forking the site in two. Written only
    # if it is not already set: a site transferred twice was still MINTED once, and
    # overwriting this with the most recent source would make the second move
    # re-derive an id the Worker has never been called.
    if not (getattr(site, "identity_workspace", "") or ""):
        site.identity_workspace = getattr(site, "workspace", "") or ""

    site.workspace = destination
    site.owner = accepting_user_id
    return OUTCOME_DONE


async def _move_records(*, site: Any, destination: str, deps: Any) -> str:
    """The customer's data, AFTER the ownership record and never before.

    Leads, concierge transcripts, the design brief and the rate counter are all
    workspace-keyed rows about this site. Moving them first would put a tenant in
    possession of another tenant's captured contact details before it owned the site
    they belong to — and if the transfer then failed and was abandoned, it would keep
    them.
    """
    moved = await deps.move_records(
        site_id=str(getattr(site, "id", "")),
        source_workspace_id=getattr(site, "identity_workspace", "") or "",
        destination_workspace_id=destination,
    )
    return OUTCOME_DONE if moved else OUTCOME_SKIPPED


async def _move_assets(*, site: Any, deps: Any) -> str:
    """The public images DO NOT MOVE, and copying them would be worse than this.

    A site's images live on a world-readable bucket under
    ``public_assets.prefix_for(workspace, pocket)`` — the SOURCE workspace id is
    inside the object key. The design this implements called for copying the prefix
    to the destination and purging the source. Both halves are wrong here, for the
    same reason:

      * that key is baked into an ABSOLUTE, IMMUTABLE, year-cached public URL that
        is already sitting inside the deployed HTML AND inside the pocket's stored
        spec. Nothing rewrites either. So purging the source blanks every image on a
        live site the moment it changes hands — the one outcome a transfer must not
        produce;
      * and copying the bytes to a prefix that nothing references buys nothing. A
        republish rebuilds from the same stored spec and emits the same old URLs.

    So the objects stay exactly where they are and the site keeps rendering. What
    this step DOES is record the prefix they stayed at, because otherwise they become
    unreclaimable: the delete cascade purges ``prefix_for(site.workspace, ...)``, and
    after a transfer that names the destination — an empty prefix — while the real
    objects sit under a workspace id no surviving record mentions. Teardown reads
    this list and purges those too.

    They are not reachable by the old tenant in the meantime: every asset endpoint
    resolves the prefix from the caller's own workspace and the pocket has moved, so
    the source can no longer list or delete them.
    """
    source_ws = (getattr(site, "identity_workspace", "") or "").strip()
    pocket_id = (getattr(site, "pocket_id", "") or "").strip()
    if not source_ws or not pocket_id:
        return OUTCOME_SKIPPED

    prefix = deps.asset_prefix_for(source_ws, pocket_id)
    retained = list(getattr(site, "asset_source_prefixes", None) or [])
    if prefix not in retained:
        retained.append(prefix)
    site.asset_source_prefixes = retained
    return OUTCOME_ASSETS_RETAINED


async def _finalize(*, site: Any, accepting_user_id: str, deps: Any) -> str:
    """Clear the offer and stamp the move. LAST, so nothing reads as settled early.

    Returning ``transfer_status`` to ``none`` rather than to a terminal success value
    is the same choice ``delete_status`` makes: the row is in its ordinary state
    again, and ``transferred_at`` is what records that this happened.
    """
    site.transfer_status = STATUS_NONE
    site.transfer_to_workspace = ""
    site.transfer_offered_by = ""
    site.transfer_offered_at = None
    site.transfer_reason = None
    site.transferred_at = deps.now()
    return OUTCOME_DONE
