# ee/pocketpaw_ee/cloud/belt/headless.py — the HEADLESS develop runner.
# Created: 2026-06-13 (feat/belt-headless-exec).
#
# Updated: 2026-09-12 (headless gate) — this runner now puts its produced diff
#   through the MECHANICAL gate before attaching it. It did not before, and the
#   asymmetry ran the wrong way: ``verify_diff`` had exactly ONE call site,
#   ``belt_propose_change``, the INTERACTIVE station — where a human is already
#   in the loop watching the run. This path, the mandate-driven autonomous one
#   where nobody is watching at all, wrote the model's diff straight onto the
#   queued Action. So "a human at the Instinct gate only ever approves VERIFIED
#   work" was true of the station and false here.
#
#   The call sits between the base_branch check and ``_attach_diff``, and it is
#   the SAME ``cloud.belt.verify.gate_diff`` the station calls (extracted from
#   that handler in this change, not re-implemented — one gate, or it drifts).
#   Two stages come with it: ``verify`` (emitted inside the helper, only when a
#   check genuinely runs) and ``gate`` (emitted here, only once the diff is
#   really on the row).
#
#   FAIL-CLOSED, in the only shape this path has. The station refuses the
#   propose and hands the failure text to the agent that can fix it; there is no
#   agent here and the Action already exists, so a red verdict lands on the
#   state the run was ALREADY in — queued, ``station_pending``, no diff — with
#   the failing check names on ``headless_error`` and the full output in the
#   log. No diff is attached, no ``gate`` stage is emitted, and a human sees no
#   proposal rather than an unverified one. ``passed`` / ``no_checks`` /
#   ``disabled`` attach as before and carry the same ``verification`` blob key
#   the station writes, so the Tray reads one evidence field either way.
#
#   What this does NOT change: the runner still never raises (a gate that blows
#   up is caught and lands on the same safe state), still never approves, and
#   still leaves every produced diff PENDING at the human gate.
#
# Updated: 2026-09-12 (feat/belt-entity-events, stage slice) — ``run`` now emits
#   the two stages it genuinely reaches, via ``service.emit_belt_stage``:
#   ``orient`` once the queued blob is validated and the ``DevelopRequest`` is
#   built (the runner has resolved what it is working on), then ``develop``
#   immediately before ``develop_fn`` is awaited (the long step, reported while
#   it happens rather than after). Both carry the REAL ``action_id`` — this is
#   the only develop path that has one, because the queued Action was filed by a
#   mandate before the runner ever saw it, so the transitions patch a run card
#   that already exists. ``status`` stays ``"queued"``: the Action row IS still
#   queued until a diff comes back, and the stage moving is not the status
#   moving. Best-effort and non-fatal, like every other belt emit — a dead bus
#   cannot fail a develop run. No new dependency, no new state: the path is
#   linear, so the forward-only guard is just ``prev`` threaded between the two
#   calls.
#
# Updated: 2026-06-13 (PR #1464 review) — store the produced diff VERBATIM (only
#   normalizing a single trailing newline) instead of the leading/trailing-
#   stripped value: stripping a real diff's trailing newline corrupts it for
#   ``git apply``. Emptiness is still decided on the stripped value, so a
#   whitespace-only diff stays safely queued. Also: dropped the dead ``_calls``
#   field, and added a best-effort ``headless_diff_attached`` audit-log entry at
#   diff attachment — the first point LLM-produced content enters the Instinct
#   store without a human typing it, so an operator trail is worth keeping.
#
# WHAT THIS CLOSES — the mandate→belt path was NOT autonomous. An approved
# mandate plan task became a QUEUED ``code_change`` Instinct Action
# (``station_pending=True``, NO diff) filed by ``mandates.executor.
# StationTaskDispatcher``, and a HUMAN then had to open the interactive ``/belt``
# chat surface to PRODUCE the diff. This module removes the human from PRODUCING
# the diff — and ONLY from that. The per-diff human approval gate is preserved:
# the runner leaves the action PENDING, carrying a real diff awaiting the
# Instinct gate exactly as a human-driven ``belt_propose_change`` would.
#
# THE SHAPE:
#   * ``DevelopFn`` — an injectable async callable ``(DevelopRequest) ->
#     DevelopResult``. It is the LLM develop loop (the genuine external boundary,
#     the analogue of ``GhCliPrOpener`` / ``PrOpener`` in ``belt/executor.py``).
#     Tests inject a deterministic fake that returns a canned diff — code under
#     test NEVER calls a real LLM or spawns a real agent. Production wires the
#     real develop loop here (a follow-up; the runner is agnostic to it).
#   * ``HeadlessDevelopRunner.run(action_id)`` — reads the queued ``code_change``
#     blob, calls the ``DevelopFn`` for a diff, puts that diff through the
#     mechanical gate, then back-writes the diff + base_branch + the gate's
#     verdict onto the blob, CLEARS ``station_pending``, and mints a
#     Decision-Graph ``correlation_id`` so the gate closes the chain on approve.
#     The action stays PENDING. NEVER raises — a ``DevelopFn`` failure, an empty
#     diff, or a red verification leaves the run SAFE (still queued, no diff)
#     and records a note.
#   * ``HeadlessTaskDispatcher`` — a ``TaskDispatcher`` (the mandates seam) that
#     files the queued run via the existing ``StationTaskDispatcher`` and then
#     runs the headless runner on it, so one dispatch turns an approved plan task
#     into a real pending diff. Additive + selectable via
#     ``POCKETPAW_MANDATE_DISPATCHER=headless`` — the interactive ``station`` and
#     announce-only ``bus`` dispatchers are untouched.
#
# WHY back-write the SAME action rather than file a fresh one: the queued run is
# already the row the console Runs tab reads and the belt gate would execute.
# Populating its diff in place keeps one durable run record per task (provenance
# to the mandate shift stays on the blob) and reuses the EXACT applyable shape
# the belt executor expects (``base_branch`` + ``diff`` + cleared
# ``station_pending`` — see ``belt/executor.py`` schema-2 guard). The direct-SQL
# blob update mirrors ``belt/executor.py::_persist_run_result`` and the MCP
# server's ``_persist_chain_ids`` — the same pattern, no new store method.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from pocketpaw_ee.cloud.belt.service import emit_belt_stage

logger = logging.getLogger(__name__)

# Kept in sync with ``belt/executor.py`` and the agent MCP server's literals.
# Duplicated (not imported) so this module has no hard dependency on the
# agent-side MCP module — same OSS/EE discipline the rest of the belt subsystem
# uses for these literals.
_CODE_CHANGE_PARAM_KEY = "_code_change"
_CODE_CHANGE_SCHEMA = 2


@dataclass(frozen=True)
class DevelopRequest:
    """Everything the develop loop needs to produce a diff for one task.

    Built from the queued ``code_change`` blob. ``task`` is the human-readable
    task text (title + why) the station agent would have picked up; ``repo`` is
    the mandate's bound repo path; ``base_branch`` is the blob's base (often
    empty for a queued run — the develop loop / the result decides it)."""

    task: str
    summary: str
    repo: str
    base_branch: str
    workspace_id: str
    mandate_id: str = ""
    shift_no: int = 0


@dataclass(frozen=True)
class DevelopResult:
    """The output of one headless develop run — a unified diff plus the branch
    it bases on. ``files_changed`` is optional metadata for the run feed."""

    diff: str
    base_branch: str
    summary: str = ""
    files_changed: int = 0


class DevelopFn(Protocol):
    """Injectable develop loop — the genuine external boundary (the LLM session
    that turns a task into a diff). Tests inject a deterministic fake; production
    wires the real loop. Async so the real implementation can await an LLM."""

    async def __call__(self, request: DevelopRequest) -> DevelopResult: ...


@dataclass
class HeadlessDevelopRunner:
    """Turns a QUEUED ``code_change`` run into a real PENDING diff, headlessly.

    Takes an injectable ``DevelopFn`` so tests pass a canned-diff fake and the
    runner never calls a real LLM. ``run(action_id)`` never raises — every
    failure path, a red mechanical verification included, leaves the queued run
    SAFE (no diff, ``station_pending`` intact) and records a note on the blob.
    The produced diff is left PENDING: the per-diff human gate is preserved (the
    runner never approves or executes), and it is now a VERIFIED diff that gate
    sees — the same ``gate_diff`` the interactive station runs."""

    develop_fn: DevelopFn

    async def run(self, action_id: str, *, workspace_id: str | None = None) -> str:
        """Produce a diff for a queued ``code_change`` action and attach it.

        Returns the action id (a run reference) in every case — success or
        handled failure. Never raises: a develop-loop crash, an empty diff, or a
        FAILED mechanical verification leaves the run queued and records
        ``headless_error`` on the blob so a human can still drive the station or
        the dispatcher can retry.

        ``workspace_id`` scopes the store: this runs on a background dispatch
        path (no ``current_workspace`` ContextVar), so the caller threads the
        tenant in. Under ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` a missing one
        fail-closes — the blob (which also carries it) can't be read until the
        store is open, so the explicit arg is the only safe source here."""
        from pocketpaw.stores import get_instinct_store

        store = get_instinct_store(workspace_id=workspace_id or None)
        action = await store.get_action(action_id)
        if action is None:
            logger.warning("headless: action %s not found — nothing to develop", action_id)
            return action_id

        blob = (getattr(action, "parameters", None) or {}).get(_CODE_CHANGE_PARAM_KEY)
        if not isinstance(blob, dict):
            logger.warning("headless: action %s carries no _code_change blob", action_id)
            return action_id

        # Only a QUEUED run (station_pending, no diff) is ours to develop. A run
        # that already carries a diff is left alone (idempotent / re-entrant
        # safe) — never overwrite an existing proposed diff.
        if not blob.get("station_pending") or (blob.get("diff") or "").strip():
            logger.info(
                "headless: action %s is not a queued station run (or already has a "
                "diff) — skipping",
                action_id,
            )
            return action_id

        request = DevelopRequest(
            task=str(blob.get("task") or ""),
            summary=str(blob.get("summary") or ""),
            repo=str(blob.get("repo") or ""),
            base_branch=str(blob.get("base_branch") or ""),
            workspace_id=str(blob.get("workspace_id") or ""),
            mandate_id=str(blob.get("mandate_id") or ""),
            shift_no=int(blob.get("shift_no") or 0),
        )

        # STAGE: orient. The runner has resolved WHICH run it is developing —
        # the action loaded, the blob validated as a genuinely queued run, the
        # task / repo / base branch read off it. That is this path's orientation
        # step, and it is the last point before the develop loop takes over for
        # what may be several minutes.
        #
        # This is the ONE path that can carry a real ``action_id``: the queued
        # Action already exists (a mandate filed it), so the console's run card
        # exists too and these transitions patch a REAL row. The interactive
        # station cannot — see ``belt/service.py``'s module comment.
        #
        # ``status`` stays "queued" because the Action row genuinely still is:
        # ``station_pending`` is only cleared once a diff comes back. The stage
        # is what moved, not the lifecycle status, and conflating them would
        # claim progress the store does not have.
        #
        # No forward-only bookkeeping beyond threading ``prev`` through: this
        # path is strictly linear (orient, then develop, exactly once each).
        ws_id = request.workspace_id or workspace_id or ""
        stage = await emit_belt_stage(
            workspace_id=ws_id,
            stage="orient",
            prev=None,
            action_id=action_id,
            status="queued",
        )

        # STAGE: develop. The develop loop is about to run — from here the run
        # is producing changes. Emitted BEFORE the await, not after, because the
        # whole point is to report the long step while it is happening.
        await emit_belt_stage(
            workspace_id=ws_id,
            stage="develop",
            prev=stage,
            action_id=action_id,
            status="queued",
        )

        try:
            result = await self.develop_fn(request)
        except Exception as exc:  # noqa: BLE001 — the develop loop must not crash dispatch
            logger.warning(
                "headless: develop loop failed for action %s — leaving the run queued",
                action_id,
                exc_info=True,
            )
            await self._note_failure(store, action_id, f"headless develop failed: {exc}")
            return action_id

        raw_diff = result.diff or ""
        if not raw_diff.strip():
            # A whitespace-only (or empty) diff is a no-op failure, not an
            # applyable run. Leave the queued run untouched (a human can still
            # drive the station). We test EMPTINESS on the stripped value but
            # never STORE the stripped value — stripping a real diff's trailing
            # newline corrupts it for ``git apply`` (the executor reads the diff
            # verbatim into ``git apply <file>``).
            logger.warning(
                "headless: develop loop produced an empty diff for action %s — "
                "leaving the run queued",
                action_id,
            )
            await self._note_failure(store, action_id, "headless develop produced an empty diff")
            return action_id

        # Store the diff VERBATIM, only guaranteeing a single trailing newline so
        # ``git apply`` can parse the final hunk line. Leading/internal content is
        # never altered.
        diff = raw_diff if raw_diff.endswith("\n") else raw_diff + "\n"

        base_branch = (result.base_branch or request.base_branch or "").strip()
        if not base_branch:
            logger.warning(
                "headless: develop loop returned no base_branch for action %s — "
                "leaving the run queued",
                action_id,
            )
            await self._note_failure(store, action_id, "headless develop returned no base_branch")
            return action_id

        # STAGE: verify, then THE MECHANICAL GATE — the same ``gate_diff`` the
        # interactive station calls, so this path cannot drift open while that
        # one stays shut. It emits the ``verify`` stage itself (only when a check
        # genuinely runs) and reads the enable flag / timeout / per-repo commands
        # from settings once, here as there.
        #
        # This is where the gate matters MOST: no human is driving this run, so
        # an unverified diff would reach the Instinct gate with nothing behind it
        # but a model's word. Imported inside the call like every other cross-
        # module reach in this file — the verifier pulls in the executor, and a
        # background dispatch path should not carry that at import time.
        try:
            from pocketpaw_ee.cloud.belt.verify import failed_check_names, gate_diff

            verification, refusal = await gate_diff(
                repo=request.repo,
                base_branch=base_branch,
                diff=diff,
                workspace_id=ws_id,
                # The real Action id — this path HAS one (see the orient emit
                # above). No ``run_id``: there is no chat stream behind a
                # headless run. ``status`` is still "queued" because the row is:
                # the diff is not attached until the gate passes.
                action_id=action_id,
                status="queued",
            )
        except Exception as exc:  # noqa: BLE001 — a gate that explodes fails CLOSED
            # ``verify_diff`` documents that it never raises, and ``gate_diff``
            # only adds a settings read and a swallowed emit on top. This is the
            # belt-and-braces: the runner's contract is that it NEVER raises, so
            # even a broken verifier has to land on the safe state rather than
            # escape into the dispatcher.
            logger.warning(
                "headless: the mechanical gate raised for action %s — leaving the run queued",
                action_id,
                exc_info=True,
            )
            await self._note_failure(store, action_id, f"headless verification errored: {exc}")
            return action_id

        if refusal is not None:
            # FAIL CLOSED, the only way this path can: there is no agent here to
            # hand the failure back to and re-propose, and the Action already
            # exists. So we land on the state the run was ALREADY in — queued,
            # station_pending, no diff — plus the failing check names on the
            # blob. Nothing is attached, no ``gate`` stage is emitted, and a
            # human at the Tray sees no proposal at all rather than an
            # unverified one. The full check output goes to the log because
            # nothing else on this path would ever read it.
            names = failed_check_names(verification)
            logger.warning(
                "headless: verification FAILED for action %s (%s) — the diff was "
                "NOT attached and the run stays queued.\n%s",
                action_id,
                names,
                refusal,
            )
            await self._note_failure(store, action_id, f"headless verification failed ({names})")
            return action_id

        # Back-write the produced diff onto the SAME action's blob — clearing
        # ``station_pending`` and minting a Decision-Graph ``correlation_id`` so
        # the run is the EXACT applyable shape the belt gate expects. The action
        # stays PENDING: the per-diff human gate is preserved.
        attached = await self._attach_diff(
            store,
            action_id,
            diff=diff,
            base_branch=base_branch,
            summary=result.summary or request.summary,
            files_changed=result.files_changed,
            verification=verification,
        )
        if not attached:
            # The write failed; ``_attach_diff`` already recorded the note and
            # left the run queued. Emitting ``gate`` here would tell the console
            # a diff is waiting for review when none was stored.
            return action_id

        # STAGE: gate. The diff is attached and PENDING — it is now genuinely at
        # the human Instinct gate, the same place ``belt_propose_change`` reports
        # from when it files a proposal. ``status="proposed"`` is what the runs
        # read model derives for this row (a pending code_change with a diff, see
        # ``service._derive_status_stage``), not a guess. ``prev="verify"``: gate
        # is later than both verify and develop, so the forward-only guard passes
        # whether or not the gate was enabled, and nothing has to be threaded
        # back out of the helper.
        await emit_belt_stage(
            workspace_id=ws_id,
            stage="gate",
            prev="verify",
            action_id=action_id,
            status="proposed",
        )
        logger.info(
            "headless: produced a diff for action %s (base %s) — verification %s, "
            "now a real pending code_change awaiting the Instinct gate",
            action_id,
            base_branch,
            verification.get("status"),
        )
        return action_id

    async def _attach_diff(
        self,
        store: Any,
        action_id: str,
        *,
        diff: str,
        base_branch: str,
        summary: str,
        files_changed: int,
        verification: dict[str, Any],
    ) -> bool:
        """Populate the queued blob with the produced diff and clear
        ``station_pending``. Direct-SQL blob update — the SAME pattern as
        ``belt/executor.py::_persist_run_result`` and the MCP server's
        ``_persist_chain_ids`` (no new store method). The schema stays 2 so the
        belt executor's schema guard passes. Best-effort but loud: a write
        failure records a note and leaves the run queued, never applyable.

        Returns whether the diff is actually ON the row. The caller emits the
        ``gate`` stage off this: a swallowed write failure must not be reported
        to the console as a proposal waiting for review."""
        import json as _json

        import aiosqlite

        try:
            action = await store.get_action(action_id)
            if action is None:
                return False
            params = dict(getattr(action, "parameters", None) or {})
            blob = params.get(_CODE_CHANGE_PARAM_KEY)
            if not isinstance(blob, dict):
                return False
            blob = dict(blob)
            # Provenance for the audit trail below (read before mutating).
            workspace_id = str(blob.get("workspace_id") or "")
            mandate_id = str(blob.get("mandate_id") or "")
            blob["diff"] = diff
            blob["base_branch"] = base_branch
            blob["summary"] = summary
            blob["files_changed"] = files_changed
            # The run is no longer a queued placeholder — it is an applyable diff.
            blob["station_pending"] = False
            blob["schema"] = _CODE_CHANGE_SCHEMA
            # Mint a chain correlation id if the queued blob never had one (the
            # StationTaskDispatcher files queued runs without one). The belt
            # executor reads it off the blob to close the Decision-Graph chain on
            # approve; a fresh id is the headless-produced run's chain anchor.
            if not blob.get("correlation_id"):
                blob["correlation_id"] = str(uuid4())
            blob.setdefault("proposed_event_id", None)
            # The mechanical gate's verdict — the SAME key, in the same shape,
            # that ``belt_propose_change`` writes, so a human in the Tray reads
            # one evidence field regardless of which path produced the change.
            # Optional key, no schema bump (the executor's guard tests ``schema``
            # for equality).
            blob["verification"] = verification
            # Provenance — record that this diff was produced headlessly.
            blob["headless"] = True
            blob.pop("headless_error", None)
            params[_CODE_CHANGE_PARAM_KEY] = blob

            async with aiosqlite.connect(store._db_path) as db:
                await db.execute(
                    "UPDATE instinct_actions SET parameters = ?,"
                    " updated_at = datetime('now') WHERE id = ?",
                    (_json.dumps(params), action_id),
                )
                await db.commit()
        except Exception:  # noqa: BLE001 — a write failure must not crash dispatch
            logger.warning(
                "headless: failed to attach diff to action %s — leaving it queued",
                action_id,
                exc_info=True,
            )
            await self._note_failure(
                store, action_id, "headless failed to persist the produced diff"
            )
            return False

        # Audit trail — this is the FIRST place LLM-produced content enters the
        # Instinct store without a human typing it, so leave an operator trail of
        # "headless diff attached, awaiting the gate". Written AFTER the attach
        # commit so the trail never claims an attach that didn't land, and kept
        # best-effort (the store's ``log`` raises ``AuditChainError`` loudly on a
        # ledger failure — that must not undo the attach or crash dispatch).
        try:
            from pocketpaw.instinct.models import AuditCategory

            await store.log(
                actor="agent:belt-headless",
                event="headless_diff_attached",
                description=(
                    f"Headless develop attached a diff to {action_id} "
                    f"(base {base_branch}, {files_changed} file(s)) — awaiting the "
                    "per-diff Instinct gate"
                ),
                action_id=action_id,
                pocket_id=workspace_id or None,
                category=AuditCategory.DECISION,
                workspace_id=workspace_id or None,
                context={
                    "mandate_id": mandate_id,
                    "base_branch": base_branch,
                    "files_changed": files_changed,
                    "headless": True,
                    # What was mechanically proven before the row went to a
                    # human — the operator trail is the wrong place to omit it.
                    "verification": verification.get("status"),
                },
            )
        except Exception:  # noqa: BLE001 — the audit trail is best-effort here
            logger.warning(
                "headless: audit log for headless_diff_attached failed for action %s "
                "(the diff IS attached) — operator trail missing this entry",
                action_id,
                exc_info=True,
            )
        return True

    async def _note_failure(self, store: Any, action_id: str, reason: str) -> None:
        """Record a headless-develop failure ON the blob WITHOUT making the run
        applyable. The run STAYS queued (``station_pending=True``, no diff) so a
        human can still drive the station or the dispatcher can retry — we never
        approve or fail the Action out from under the human gate. Best-effort."""
        import json as _json

        import aiosqlite

        try:
            action = await store.get_action(action_id)
            if action is None:
                return
            params = dict(getattr(action, "parameters", None) or {})
            blob = params.get(_CODE_CHANGE_PARAM_KEY)
            if not isinstance(blob, dict):
                return
            blob = dict(blob)
            # Keep the run SAFE: still queued, no diff. Only annotate the failure.
            blob["station_pending"] = True
            blob["diff"] = ""
            blob["headless_error"] = reason
            params[_CODE_CHANGE_PARAM_KEY] = blob
            async with aiosqlite.connect(store._db_path) as db:
                await db.execute(
                    "UPDATE instinct_actions SET parameters = ?,"
                    " updated_at = datetime('now') WHERE id = ?",
                    (_json.dumps(params), action_id),
                )
                await db.commit()
        except Exception:  # noqa: BLE001 — never crash on the failure-note path
            logger.debug("headless: failed to record headless_error note", exc_info=True)


@dataclass
class HeadlessTaskDispatcher:
    """A ``TaskDispatcher`` (the mandates dispatch seam) that files a queued
    ``code_change`` run via the existing ``StationTaskDispatcher`` and then runs
    the headless runner on it — so an approved plan task becomes a real PENDING
    diff in one dispatch, with NO human in the diff-producing loop. The per-diff
    human gate is preserved: the produced diff is left pending the Instinct gate.

    Additive + selectable. The interactive ``station`` (human-driven) and the
    announce-only ``bus`` dispatchers in ``mandates/executor.py`` are untouched.
    A develop failure degrades gracefully: the queued run survives (a human can
    still drive the station), so a headless miss never loses the task."""

    runner: HeadlessDevelopRunner

    async def dispatch(
        self,
        *,
        workspace_id: str,
        mandate_id: str,
        shift_no: int,
        plan_action_id: str,
        index: int,
        task: dict[str, Any],
    ) -> str:
        from pocketpaw_ee.cloud.mandates.executor import StationTaskDispatcher

        # 1. File the queued run exactly as the station dispatcher does (real
        #    code_change Action, station_pending, no diff, run-feed event).
        run_ref = await StationTaskDispatcher().dispatch(
            workspace_id=workspace_id,
            mandate_id=mandate_id,
            shift_no=shift_no,
            plan_action_id=plan_action_id,
            index=index,
            task=task,
        )
        # 2. Produce the diff headlessly and attach it (the run becomes a real
        #    pending diff). Never raises — a miss leaves the queued run for a
        #    human to drive. Thread the workspace so the runner's store is scoped
        #    to the tenant (no ContextVar on this background path — ISO).
        await self.runner.run(run_ref, workspace_id=workspace_id)
        return run_ref


# ---------------------------------------------------------------------------
# Production wiring seam — the develop loop.
# ---------------------------------------------------------------------------
#
# The real LLM develop loop is a follow-up; this hook lets it be wired without
# touching ``mandates/executor.py`` again. A deploy that wires a production
# ``DevelopFn`` sets ``_PRODUCTION_DEVELOP_FN`` (via ``set_production_develop_fn``)
# and ``POCKETPAW_MANDATE_DISPATCHER=headless`` then selects the autonomous path.
# Until one is wired, ``resolve_headless_dispatcher`` returns ``None`` and the
# mandates executor falls back to the queued-run station dispatcher — a deploy is
# never left filing un-developable runs, and code under test NEVER reaches a real
# LLM (tests construct ``HeadlessTaskDispatcher`` with a fake ``DevelopFn``
# directly, bypassing this seam).
_PRODUCTION_DEVELOP_FN: DevelopFn | None = None


def set_production_develop_fn(fn: DevelopFn | None) -> None:
    """Wire (or clear) the production develop loop the headless dispatcher uses.

    Called once at app wiring time by a deploy that ships a real develop loop.
    Tests do NOT use this — they inject a fake ``DevelopFn`` into
    ``HeadlessDevelopRunner`` / ``HeadlessTaskDispatcher`` directly."""
    global _PRODUCTION_DEVELOP_FN
    _PRODUCTION_DEVELOP_FN = fn


def resolve_headless_dispatcher() -> HeadlessTaskDispatcher | None:
    """Build the headless dispatcher IF a production develop loop is wired.

    Returns ``None`` when no ``DevelopFn`` is wired so the mandates executor can
    degrade to the queued-run station dispatcher (a human can still drive the
    run). This keeps the autonomous path strictly opt-in and never silently
    files runs that nothing can develop."""
    if _PRODUCTION_DEVELOP_FN is None:
        return None
    return HeadlessTaskDispatcher(runner=HeadlessDevelopRunner(develop_fn=_PRODUCTION_DEVELOP_FN))


__all__ = [
    "DevelopFn",
    "DevelopRequest",
    "DevelopResult",
    "HeadlessDevelopRunner",
    "HeadlessTaskDispatcher",
    "resolve_headless_dispatcher",
    "set_production_develop_fn",
]
