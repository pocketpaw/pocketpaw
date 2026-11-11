# ee/pocketpaw_ee/versions/instinct_executor.py — the apply-on-approve /
# discard-on-reject executor for the Branch primitive's artifact-change merge gate.
#
# The Instinct router dispatches here when an approved / rejected Action carries an
# ``_artifact_change`` blob (the peer of the pocket-write bridge, the belt
# code-change executor and the external-action executor). Artifact-generic over
# (scope_type, scope_id). The router owns the tenant gate
# (``_assert_artifact_change_workspace``, a 403 before any mutation) and the
# Decision-Graph chain emits; this module owns the state transitions and the deploy.
#
#   * execute_approved_change(action, human_event_id=None, confirm_destructive=False)
#     APPROVE = MERGE: publish ``to_version_id`` (moves the published pointer; the
#     row is NOT also marked merged, which would erase that pointer in the
#     single-row model), then for a pocket/site scope deploy through
#     ``sites.service.publish_pocket``, then mark the Action executed or failed.
#     The blob's ``destructive_migrations`` (recorded and pinned at propose time)
#     become the deploy's ``confirm_destructive_migrations`` ONLY when the caller
#     passes ``confirm_destructive=True``, which the single-Action approve route
#     does and bulk approve does not: a migration that deletes data is approved one
#     card at a time, by someone who saw it.
#     The publish gate runs BEFORE the merge: blocking findings (placeholders, made-up
#     numbers, broken forms) fail the Action with the owner-worded refusal and record
#     them on the blob as ``publish_blocked``; nothing is promoted or deployed.
#     ``confirm_placeholders=True`` (an individual approve's "publish anyway") skips it.
#   * approve_gate(store, action, ...) — the same gate for the single-Action approve
#     route, run BEFORE the Action flips to approved: a refusal is returned (the
#     route raises it as 409 ``sites.publish_blocked``) and the card stays pending,
#     so "Publish anyway" approves that same card. Publish anyway counts only when
#     it echoes the refusal's ``findings_digest``. A pass returns the content hash it
#     checked; the route hands it to the executor as ``gated_hash``, which skips its
#     own check only while the draft still has that hash. A STALE card is refused
#     there first (409 ``sites.publish_review_stale``, the card failed).
#     A STALE card is refused before the gate: the pocket's content no longer matches
#     the ``reviewed_content_hash`` the card was filed for (or, on a card without one,
#     a newer draft superseded ``to_version_id``). The Action fails with
#     ``sites.publish_review_stale`` and ``publish_stale`` on the blob; nothing is
#     promoted or deployed, and the owner files a new review of the current draft.
#   * discard_rejected_change(action) — REJECT = DISCARD: every draft above the
#     published pointer is discarded in one pass (``discard_all_drafts``); the
#     published pointer is untouched and nothing deploys. Best effort.
#
# Both lazy-import the versions + sites services so the instinct package keeps no
# module-top dependency on either. Neither raises: failures land on the Action.
from __future__ import annotations

import logging
from typing import Any, NamedTuple

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


class GateResult(NamedTuple):
    """What the publish gate decided: the refusal (None to go ahead) and the
    content hash it checked ("" when the pocket could not be read)."""

    refusal: Any
    content_hash: str


def _content_hash(pocket: Any) -> str:
    from pocketpaw_ee.sites import verify

    if pocket is None:
        return ""
    try:
        return str(verify.publish_content_hash(pocket) or "")
    except Exception:  # noqa: BLE001 — no hash: nothing can be skipped on it
        return ""


def _confirmed_migration_keys(blob: dict[str, Any]) -> list[str]:
    """The ``name:sha256`` keys of the destructive migrations the card showed."""
    found = blob.get("destructive_migrations")
    if not isinstance(found, list):
        return []
    return sorted({str(d["key"]) for d in found if isinstance(d, dict) and d.get("key")})


async def _gate(
    store: Any,
    action: Any,
    blob: dict[str, Any],
    *,
    workspace_id: str,
    scope_id: str,
    author: str,
    confirmed: bool,
    confirm_digest: str | None = None,
    approver: str | None = None,
    can_override: bool = True,
    base_parameters: dict[str, Any] | None = None,
) -> GateResult:
    """Run the publish gate on the pocket about to go live. On blocking findings and
    no ``confirm_placeholders``: record them on the Action's stored blob as
    ``publish_blocked`` and return the :class:`~pocketpaw_ee.sites.publish_gate.
    PublishBlocked` refusal (nothing is merged, nothing deploys). A
    ``confirm_digest`` other than None must match the current findings, or the
    confirm does not count. With the approver's "publish anyway" the override is
    audited under the approver. Content that goes ahead is remembered as vetted, so
    the deploy's own gate (a machine republish) does not run twice.
    ``base_parameters`` is what gets written back (the stored proposal, never an
    approver's unreviewed edits)."""
    from pocketpaw_ee.sites import publish_gate, verify

    pocket, findings, skipped = await publish_gate.pocket_check(pocket_id=scope_id, user_id=author)
    content_hash = _content_hash(pocket)
    if confirm_digest is not None and confirm_digest != publish_gate.findings_digest(findings):
        confirmed = False
    if findings and confirmed:
        approver = str(approver or getattr(action, "approved_by", None) or author)
        await publish_gate.audit_override(
            workspace_id=workspace_id, actor_id=approver, pocket_id=scope_id, findings=findings
        )
    if not findings or confirmed:
        # A skipped or partial check vouches for nothing: the deploy then gates again.
        if pocket is not None and (not skipped or confirmed):
            try:
                publish_gate.remember_gated(scope_id, verify.publish_content_hash(pocket))
            except Exception:  # noqa: BLE001 — the deploy then gates again; harmless
                logger.warning("artifact-change merge: could not remember %s", scope_id)
        return GateResult(None, content_hash)
    refusal = publish_gate.refusal(findings, can_override=can_override)
    try:
        stored = base_parameters if base_parameters is not None else action.parameters
        parameters = dict(stored or {})
        if isinstance(parameters.get("_artifact_change"), dict):
            blob = parameters["_artifact_change"]
        parameters["_artifact_change"] = {**blob, "publish_blocked": refusal.details}
        await store.update_parameters(action.id, parameters)
    except Exception:  # noqa: BLE001 — the error text still carries every finding
        logger.warning("artifact-change merge: could not record findings on %s", action.id)
    return GateResult(refusal, content_hash)


async def _current_hash(scope_id: str, author: str) -> str:
    """The publish content hash of the pocket as it is now ("" when unreadable)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    try:
        return _content_hash(await pockets_service.get(scope_id, author))
    except Exception:  # noqa: BLE001 — unreadable: the caller re-runs the gate
        return ""


async def _publish_blocked(
    store: Any,
    action: Any,
    blob: dict[str, Any],
    *,
    workspace_id: str,
    scope_id: str,
    author: str,
    confirmed: bool,
    confirm_digest: str | None = None,
    gated_hash: str | None = None,
) -> bool:
    """:func:`_gate` for an already-approved Action: a refusal fails the Action with
    the owner-worded message (recorded with ``can_override`` false: a failed card
    cannot be approved again) and returns True. ``gated_hash`` is the content the
    approve route already passed (:func:`approve_gate`); while the draft still has
    it, the gate does not run twice."""
    if gated_hash and await _current_hash(scope_id, author) == gated_hash:
        return False
    refusal, _ = await _gate(
        store,
        action,
        blob,
        workspace_id=workspace_id,
        scope_id=scope_id,
        author=author,
        confirmed=confirmed,
        confirm_digest=confirm_digest,
        can_override=False,
    )
    if refusal is None:
        return False
    await store.mark_failed(action.id, f"{refusal.code}: {refusal.message}")
    return True


def _stale_refusal(blob: dict[str, Any]) -> Any:
    from pocketpaw_ee.sites.publish_gate import PublishReviewStale

    return PublishReviewStale(str(blob.get("to_version_id") or "") or None)


async def _review_is_stale(blob: dict[str, Any], *, scope_id: str, author: str) -> bool:
    """True when the content the card reviewed is no longer what would deploy.

    ``publish_pocket`` deploys the pocket's CURRENT content, not the ``to_version_id``
    snapshot, so approving a card filed for older content would ship edits the
    reviewer never saw and mark the wrong version published. With a
    ``reviewed_content_hash`` the pocket's current hash must equal it; without one (an
    older card, or an engine with no content hash) a ``to_version_id`` that a later
    draft superseded (or a discard dropped) is the signal. An unreadable pocket is not
    stale here: the deploy reads it again and fails on its own."""
    reviewed = str(blob.get("reviewed_content_hash") or "")
    if reviewed:
        from pocketpaw_ee.cloud.pockets import service as pockets_service
        from pocketpaw_ee.sites import verify

        try:
            pocket = await pockets_service.get(scope_id, author)
            current = verify.publish_content_hash(pocket)
        except Exception:  # noqa: BLE001 - see the docstring
            logger.warning("artifact-change merge: could not read %s to check the review", scope_id)
            return False
        if current:
            return current != reviewed
    from pocketpaw_ee.versions import service as versions_service

    try:
        row = await versions_service._scoped_row(
            scope_type=str(blob.get("scope_type") or ""),
            scope_id=scope_id,
            workspace_id=str(blob.get("workspace") or blob.get("workspace_id") or ""),
            version_id=str(blob.get("to_version_id") or ""),
        )
    except Exception:  # noqa: BLE001 - a missing version fails in the merge below
        return False
    # A later edit superseded the reviewed draft (or a discard dropped it).
    return row.status in ("superseded", "discarded")


async def _refuse_stale(
    store: Any, action: Any, blob: dict[str, Any], *, base_parameters: Any = None
) -> Any:
    """Fail the card as stale: ``publish_stale`` (the refusal's code, message and
    details) on its stored blob, ``error`` = ``<code>: <message>``. Returns the
    :class:`~pocketpaw_ee.sites.publish_gate.PublishReviewStale` refusal."""
    refusal = _stale_refusal(blob)
    try:
        stored = base_parameters if base_parameters is not None else action.parameters
        parameters = dict(stored or {})
        if isinstance(parameters.get("_artifact_change"), dict):
            blob = parameters["_artifact_change"]
        parameters["_artifact_change"] = {
            **blob,
            "publish_stale": {
                "code": refusal.code,
                "message": refusal.message,
                **refusal.details,
            },
        }
        await store.update_parameters(action.id, parameters)
    except Exception:  # noqa: BLE001 - the error text still says why
        logger.warning("artifact-change merge: could not record the stale review on %s", action.id)
    await store.mark_failed(action.id, f"{refusal.code}: {refusal.message}")
    return refusal


async def approve_gate(
    store: Any,
    action: Any,
    *,
    approver: str,
    confirm_placeholders: bool,
    confirm_digest: str | None,
    stored_parameters: dict[str, Any] | None,
) -> GateResult:
    """The publish gate for a single-Action approve, run while the card is still
    pending. ``refusal`` is the ``PublishBlocked`` (findings recorded on the stored
    blob, ``stored_parameters``) or None when the approve may go ahead, including for
    blobs that do not deploy. A Publish anyway counts only with the refusal's
    ``findings_digest`` (``confirm_digest``). ``content_hash`` is what was checked."""
    blob = artifact_change_blob(action)
    if blob is None:
        return GateResult(None, "")
    scope_type = str(blob.get("scope_type") or "")
    scope_id = str(blob.get("scope_id") or "")
    workspace_id = str(blob.get("workspace") or blob.get("workspace_id") or "")
    if scope_type not in _DEPLOYABLE_SCOPES or not scope_id:
        return GateResult(None, "")
    author = str(blob.get("user_id") or "system")
    # A card filed for content the site no longer holds: no override, the card fails.
    if await _review_is_stale(blob, scope_id=scope_id, author=author):
        refusal = await _refuse_stale(store, action, blob, base_parameters=stored_parameters)
        return GateResult(refusal, "")
    return await _gate(
        store,
        action,
        blob,
        workspace_id=workspace_id,
        scope_id=scope_id,
        author=author,
        confirmed=confirm_placeholders,
        confirm_digest=confirm_digest or "",
        approver=approver,
        base_parameters=stored_parameters,
    )


async def execute_approved_change(
    action: Any,
    *,
    human_event_id: Any | None = None,
    confirm_destructive: bool = False,
    confirm_placeholders: bool = False,
    confirm_findings_digest: str | None = None,
    gated_hash: str | None = None,
) -> None:
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

    blob = artifact_change_blob(action)
    if blob is None:
        # Defensive — the router only calls this when the blob is present. We
        # can't resolve the store's workspace without the blob either, so bail
        # before opening one.
        return

    scope_type = str(blob.get("scope_type") or "")
    scope_id = str(blob.get("scope_id") or "")
    workspace_id = str(blob.get("workspace") or blob.get("workspace_id") or "")
    # ISO: HTTP approve path (no ``current_workspace`` ContextVar) — scope the
    # store to the blob's workspace so the merge's executed/failed mark lands in
    # the tenant's file, not the shared ledger (and doesn't raise under the flag).
    store = get_instinct_store(workspace_id=workspace_id or None)
    to_version_id = str(blob.get("to_version_id") or "")
    author = blob.get("user_id")

    if not (scope_type and scope_id and workspace_id and to_version_id):
        await store.mark_failed(
            action.id,
            "artifact-change merge: blob missing scope_type/scope_id/workspace/to_version_id",
        )
        return

    # A card filed for content the pocket no longer holds is refused outright: the
    # deploy below ships the CURRENT content, which nobody reviewed.
    if scope_type in _DEPLOYABLE_SCOPES and await _review_is_stale(
        blob, scope_id=scope_id, author=str(author or "system")
    ):
        await _refuse_stale(store, action, blob)
        return

    # The publish gate BEFORE the merge: a refused publish promotes no version, so the
    # draft stays the draft and the owner can fix it and ask again.
    if scope_type in _DEPLOYABLE_SCOPES and await _publish_blocked(
        store,
        action,
        blob,
        workspace_id=workspace_id,
        scope_id=scope_id,
        author=str(author or "system"),
        confirmed=confirm_placeholders,
        confirm_digest=confirm_findings_digest,
        gated_hash=gated_hash,
    ):
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

            confirm_kw: dict[str, Any] = {}
            keys = _confirmed_migration_keys(blob) if confirm_destructive else []
            if keys:
                confirm_kw["confirm_destructive_migrations"] = keys
            await sites_service.publish_pocket(
                workspace_id=workspace_id,
                user_id=str(author or "system"),
                pocket_id=scope_id,
                # Gated above, before the merge, and remembered: exempt as vetted.
                machine_republish=True,
                **confirm_kw,
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
    published to ``status="discarded"`` so the bar clears on click 1, and it
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
