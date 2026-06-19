# ee/pocketpaw_ee/versions/instinct_executor.py
# Created: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — the
# apply-on-approve / discard-on-reject executor for the Branch primitive's
# artifact-change merge gate.
#
# Updated 2026-06-19 (P2a — discard-all-drafts): discard_rejected_change now
# clears ALL drafts above the published pointer in one pass via
# versions_service.discard_all_drafts (passing the blob's branch), instead of
# reverting only the single candidate ``to_version_id``. Fixes "discard needs N
# clicks": a pocket edited N times accumulated N draft rows, and reverting only
# the latest left the others standing so the unpublished-changes bar never
# cleared. The published pointer is still left untouched (a reject never moves
# Live).
#
# This is the executor the Instinct router dispatches to when an approved /
# rejected Action carries an ``_artifact_change`` blob (the peer of the
# pocket-write bridge, the belt code-change executor, and the external-action
# executor). It is artifact-GENERIC over (scope_type, scope_id) and keeps the
# router thin — the router owns the tenant gate (``_assert_artifact_change_
# workspace``, a 403 before any mutation) + the Decision-Graph chain emits; this
# module owns the actual MERGE / DISCARD state transitions + the deploy trigger.
#
# Contract (mirrors the belt executor):
#   * execute_approved_change(action, human_event_id=None) — APPROVE = MERGE:
#       1. publish the reviewed candidate (``to_version_id``) → moves the
#          published pointer via versions.publish(). This IS the merge. In the
#          single-row derived-pointer model (versions/models.py) published/merged
#          are mutually exclusive, so we do NOT also mark this row "merged" — that
#          would erase the published pointer. ``service.mark_merged`` exists for a
#          branch-based flow where the published row lives separately on main
#          (BP-4 / a producer path), not for this in-place promote.
#       2. for pocket/site scope, trigger the deploy via BP-2's
#          ``sites.service.publish_pocket(...)`` so Live reflects the new
#          published version;
#       3. mark the Action ``executed`` (success) or ``failed`` (any error) on
#          the Instinct store, so The Tray shows the outcome.
#   * discard_rejected_change(action) — REJECT = DISCARD: flip the candidate
#       (``to_version_id``) status="reverted" so it leaves the draft pointer; the
#       PUBLISHED pointer is left UNTOUCHED (a rejection never moves what is
#       live). No deploy. Best-effort store nudge — the router already closed the
#       chain on the reject path.
#
# Both lazy-import the versions + sites services so the instinct package keeps no
# module-top dependency on either (same discipline as the other executors), and
# the deploy trigger is itself wrapped so a deploy failure marks the Action
# failed rather than crashing the approve response (the router's call is also
# best-effort).
#
# TODO(BP-4): revert/discard semantics deepen — BP-4 owns reverting the published
# pointer to a prior snapshot + the Journal history projection. BP-3 only
# promotes the candidate on approve and abandons it on reject.
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Scope types whose merge also triggers a site deploy. A "pocket" artifact is the
# source pocket behind a Paw Site (BP-2 keys site versions on scope_type="pocket"),
# and a "site" scope is treated the same — both publish the pocket as a site so
# Live reflects the newly published version. Other scope types (e.g. a future
# "dashboard") merge the version pointer but do NOT deploy.
_DEPLOYABLE_SCOPES = {"pocket", "site"}


def artifact_change_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_artifact_change`` blob on an Action, or ``None``.

    The blob is the merge-gate payload a Branch-primitive producer stores under
    ``Action.parameters._artifact_change``:
    ``{scope_type, scope_id, branch, from_version_id, to_version_id, workspace,
    user_id}``. The router has its own ``_artifact_change_blob`` accessor (it
    mirrors the other blob accessors there); this one lets the executor read the
    blob without importing the router. Anything not a dict is "no artifact
    change".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_artifact_change")
    return blob if isinstance(blob, dict) else None


async def execute_approved_change(action: Any, *, human_event_id: Any | None = None) -> None:
    """MERGE an approved artifact-change Action (BP-3).

    Promotes the candidate draft (``to_version_id``) to published, marks it
    merged, and — for a deployable scope — triggers the site deploy. Marks the
    Action ``executed`` on success or ``failed`` on any error. Never raises: the
    router's call site is best-effort and a failure must not break the approve
    response. The Action's outcome carries what happened so The Tray can show it.

    ``human_event_id`` is accepted for parity with the other executors (the
    chain causation handle); BP-3 does not yet thread it into a terminal chain
    event of its own (the router owns the chain emits for this blob), so it is
    currently unused beyond the signature contract.
    """
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()
    blob = artifact_change_blob(action)
    if blob is None:
        # Defensive — the router only calls this when the blob is present.
        return

    scope_type = str(blob.get("scope_type") or "")
    scope_id = str(blob.get("scope_id") or "")
    workspace_id = str(blob.get("workspace") or blob.get("workspace_id") or "")
    to_version_id = str(blob.get("to_version_id") or "")
    author = blob.get("user_id")

    if not (scope_type and scope_id and workspace_id and to_version_id):
        await store.mark_failed(
            action.id,
            "artifact-change merge: blob missing scope_type/scope_id/workspace/to_version_id",
        )
        return

    try:
        from pocketpaw_ee.versions import service as versions_service

        # Promote the reviewed candidate (to_version_id) to published — this IS
        # the merge: it moves the published pointer (``get_published`` derives the
        # pointer as the latest status=="published" row) to the version the human
        # accepted. The service re-asserts the version belongs to (scope,
        # workspace) (belt-and-braces under the router 403).
        #
        # NB: we deliberately do NOT also ``mark_merged`` THIS row. In the
        # single-row derived-pointer model (versions/models.py) ``published`` and
        # ``merged`` are mutually exclusive states, so flipping the just-published
        # row to ``merged`` would erase the published pointer. ``mark_merged`` is
        # for a branch-based flow where the published row lives separately on main
        # (a producer path / BP-4 may use it); the BP-3 executor's job is the
        # promote. TODO(BP-4): when revert/branch-merge semantics deepen, record
        # the accepted branch candidate as ``merged`` separately from the
        # published row.
        await versions_service.publish(
            scope_type=scope_type,
            scope_id=scope_id,
            workspace_id=workspace_id,
            version_id=to_version_id,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a failed Action, never raise
        logger.exception(
            "artifact-change merge: publish failed for %s:%s version %s",
            scope_type,
            scope_id,
            to_version_id,
        )
        await store.mark_failed(action.id, f"artifact-change merge failed: {exc}")
        return

    # 3. Deploy for a deployable scope so Live reflects the newly published
    #    version. The source pocket is ``scope_id`` (BP-2 keys site versions on
    #    the pocket). A deploy failure marks the Action failed — the version is
    #    already published (published != live, the same invariant BP-2 documents).
    deploy_note = ""
    if scope_type in _DEPLOYABLE_SCOPES:
        try:
            from pocketpaw_ee.sites import service as sites_service

            await sites_service.publish_pocket(
                workspace_id=workspace_id,
                user_id=str(author or "system"),
                pocket_id=scope_id,
            )
            deploy_note = " + deploy triggered"
        except Exception as exc:  # noqa: BLE001 — failed Action, never raise
            logger.exception(
                "artifact-change merge: deploy failed for %s:%s (version published)",
                scope_type,
                scope_id,
            )
            await store.mark_failed(
                action.id,
                f"artifact-change merged (version {to_version_id} published) "
                f"but deploy failed: {exc}",
            )
            return

    await store.mark_executed(
        action.id,
        f"merged {scope_type}:{scope_id} — published version {to_version_id}{deploy_note}",
    )


async def discard_rejected_change(action: Any) -> None:
    """DISCARD a rejected artifact-change Action (BP-3 + P2a).

    P2a — clears ALL drafts above the published pointer in ONE pass via
    ``versions_service.discard_all_drafts`` instead of reverting only the single
    candidate the Action names. A pocket edited N times accumulated N draft rows
    (one per edit); the old single-row discard reverted only ``to_version_id``, so
    ``get_draft`` stayed non-None and the unpublished-changes bar never cleared →
    "discard needs N clicks". One ``discard_all_drafts`` flips every draft above
    published to ``status="reverted"`` so the bar clears on click 1, and it
    back-handles pockets already carrying a pile of accumulated drafts.

    The PUBLISHED pointer is left untouched (a rejection must never move what is
    live). No deploy. Tenant-scoped on the blob's ``workspace`` (the same scope
    the merge gate enforces). Best-effort: the router already flipped the Action
    to ``rejected`` and closed the Decision-Graph chain on the reject path, so a
    discard failure here is logged and swallowed — it must not break the reject
    response.
    """
    blob = artifact_change_blob(action)
    if blob is None:
        return

    scope_type = str(blob.get("scope_type") or "")
    scope_id = str(blob.get("scope_id") or "")
    workspace_id = str(blob.get("workspace") or blob.get("workspace_id") or "")
    to_version_id = str(blob.get("to_version_id") or "")
    branch = str(blob.get("branch") or "main")
    if not (scope_type and scope_id and workspace_id and to_version_id):
        logger.debug("artifact-change discard: blob missing fields — nothing to discard")
        return

    try:
        from pocketpaw_ee.versions import service as versions_service

        # Clear EVERY draft above the published pointer in one pass (P2a), not just
        # the candidate the Action names — so one discard clears the bar.
        await versions_service.discard_all_drafts(
            scope_type=scope_type,
            scope_id=scope_id,
            workspace_id=workspace_id,
            branch=branch,
        )
    except Exception:  # noqa: BLE001 — discard nudge must never break the reject
        logger.warning(
            "artifact-change discard: failed to clear drafts for %s:%s "
            "(published pointer untouched)",
            scope_type,
            scope_id,
            exc_info=True,
        )
