# ee/pocketpaw_ee/cloud/belt/headless.py — the HEADLESS develop runner.
#
# An approved mandate plan task becomes a QUEUED ``code_change`` Instinct Action
# (``station_pending=True``, no diff) via ``mandates.executor.
# StationTaskDispatcher``. This module removes the human from PRODUCING the diff —
# and only from that: the produced diff stays PENDING the per-diff Instinct gate,
# exactly as a human-driven ``belt_propose_change`` would.
#
#   * ``DevelopFn`` — injectable async ``(DevelopRequest) -> DevelopResult``; the
#     LLM develop loop is the external boundary. Production wires
#     ``belt/develop_station.ClaudeCodeDevelop`` via ``set_production_develop_fn``
#     at app startup (``POCKETPAW_MANDATE_DISPATCHER=headless`` +
#     ``POCKETPAW_FACTORY_DEVELOP=claude``); tests inject a canned-diff fake.
#   * ``HeadlessDevelopRunner.run(action_id)`` — reads the queued blob (task,
#     summary, repo, base, mandate provenance, ``recipe``), calls the DevelopFn,
#     then back-writes diff + base_branch onto the SAME action, clears
#     ``station_pending`` and mints a Decision-Graph ``correlation_id``. Never
#     raises: a DevelopFn error (or empty diff / no base) leaves the run queued
#     and records the reason (secrets redacted) as ``headless_error`` on the
#     blob, where the console and digest read it. The diff is stored verbatim
#     (only a trailing newline is ensured — stripping corrupts it for
#     ``git apply``). A best-effort ``headless_diff_attached`` audit entry marks
#     LLM content entering the store.
#   * ``HeadlessTaskDispatcher`` — the mandates ``TaskDispatcher`` that files the
#     queued run via ``StationTaskDispatcher`` then runs the runner on it. The
#     production dispatcher (``resolve_headless_dispatcher``) runs the develop in
#     the BACKGROUND, one at a time (``_DEVELOP_LOCK``), because plan approval
#     dispatches inside the approve request and a develop takes minutes; tests
#     construct it inline (``background=False``).
#
# Blob writes go through ``InstinctStore.update_parameters`` (same pattern as
# ``belt/executor.py::_persist_run_result``).

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

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
    # A charter recipe name: run that deterministic command instead of an LLM
    # develop. "" = ordinary develop work.
    recipe: str = ""


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
    failure path leaves the queued run SAFE (no diff, ``station_pending`` intact)
    and records a note on the blob. The produced diff is left PENDING: the
    per-diff human gate is preserved (the runner never approves or executes)."""

    develop_fn: DevelopFn

    async def run(self, action_id: str, *, workspace_id: str | None = None) -> str:
        """Produce a diff for a queued ``code_change`` action and attach it.

        Returns the action id (a run reference) in every case — success or
        handled failure. Never raises: a develop-loop crash or an empty diff
        leaves the run queued and records ``headless_error`` on the blob so a
        human can still drive the station or the dispatcher can retry.

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
            recipe=str(blob.get("recipe") or ""),
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

        # Back-write the produced diff onto the SAME action's blob — clearing
        # ``station_pending`` and minting a Decision-Graph ``correlation_id`` so
        # the run is the EXACT applyable shape the belt gate expects. The action
        # stays PENDING: the per-diff human gate is preserved.
        await self._attach_diff(
            store,
            action_id,
            diff=diff,
            base_branch=base_branch,
            summary=result.summary or request.summary,
            files_changed=result.files_changed,
        )
        logger.info(
            "headless: produced a diff for action %s (base %s) — now a real "
            "pending code_change awaiting the Instinct gate",
            action_id,
            base_branch,
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
    ) -> None:
        """Populate the queued blob with the produced diff and clear
        ``station_pending``. Store-API blob update — the SAME pattern as
        ``belt/executor.py::_persist_run_result`` and the MCP server's
        ``persist_chain_ids`` (no new store method). The schema stays 2 so the
        belt executor's schema guard passes. Best-effort but loud: a write
        failure records a note and leaves the run queued, never applyable."""

        try:
            action = await store.get_action(action_id)
            if action is None:
                return
            params = dict(getattr(action, "parameters", None) or {})
            blob = params.get(_CODE_CHANGE_PARAM_KEY)
            if not isinstance(blob, dict):
                return
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
            # Provenance — record that this diff was produced headlessly.
            blob["headless"] = True
            blob.pop("headless_error", None)
            params[_CODE_CHANGE_PARAM_KEY] = blob

            await store.update_parameters(action_id, params)
        except Exception:  # noqa: BLE001 — a write failure must not crash dispatch
            logger.warning(
                "headless: failed to attach diff to action %s — leaving it queued",
                action_id,
                exc_info=True,
            )
            await self._note_failure(
                store, action_id, "headless failed to persist the produced diff"
            )
            return

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
                },
            )
        except Exception:  # noqa: BLE001 — the audit trail is best-effort here
            logger.warning(
                "headless: audit log for headless_diff_attached failed for action %s "
                "(the diff IS attached) — operator trail missing this entry",
                action_id,
                exc_info=True,
            )

    async def _note_failure(self, store: Any, action_id: str, reason: str) -> None:
        """Record a headless-develop failure ON the blob WITHOUT making the run
        applyable. The run STAYS queued (``station_pending=True``, no diff) so a
        human can still drive the station or the dispatcher can retry — we never
        approve or fail the Action out from under the human gate. The reason is
        redacted (``security.redact``): it can quote check or model output, and
        the blob is readable by anyone who can read the run. Best-effort."""
        from pocketpaw.security.redact import redact_output

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
            blob["headless_error"] = redact_output(reason)
            params[_CODE_CHANGE_PARAM_KEY] = blob
            await store.update_parameters(action_id, params)
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
    # True = return as soon as the queued run is filed and develop it in a
    # background task (serialized by ``_DEVELOP_LOCK``). The production
    # dispatcher sets it; the inline default keeps tests deterministic.
    background: bool = False

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
        if not self.background:
            await self.runner.run(run_ref, workspace_id=workspace_id)
            return run_ref

        async def _develop() -> None:
            # ponytail: one develop at a time per process (16 GB box, heavy
            # checks); per-repo locks if factory throughput ever matters.
            async with _DEVELOP_LOCK:
                await self.runner.run(run_ref, workspace_id=workspace_id)

        task = asyncio.create_task(_develop(), name=f"belt-headless-develop-{run_ref}")
        _BACKGROUND_DEVELOPS.add(task)
        task.add_done_callback(_BACKGROUND_DEVELOPS.discard)
        return run_ref


# Background develops (strong refs so the loop can't drop them) and the lock
# that serializes them.
_BACKGROUND_DEVELOPS: set[asyncio.Task[None]] = set()
_DEVELOP_LOCK = asyncio.Lock()


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
    return HeadlessTaskDispatcher(
        runner=HeadlessDevelopRunner(develop_fn=_PRODUCTION_DEVELOP_FN), background=True
    )


__all__ = [
    "DevelopFn",
    "DevelopRequest",
    "DevelopResult",
    "HeadlessDevelopRunner",
    "HeadlessTaskDispatcher",
    "resolve_headless_dispatcher",
    "set_production_develop_fn",
]
