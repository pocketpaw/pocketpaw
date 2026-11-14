# ee/pocketpaw_ee/sites/generation.py — the ``Site.generation`` flag: is the agent
# run that CREATED this site still working on it?
#
# A create tool mints the draft Site and returns, but the agent keeps building in
# the same chat run for minutes. Without a server-side signal a reload in that
# window shows the bare template, so the create tools' shared mint stamps
# ``generation="generating"`` plus the chat session (``generation_session_id``).
#
# Values: ``generating`` (a run in that session is active), ``waiting`` (the
# session's newest run ended on an unanswered ``ask_user``), ``ready`` (it ended
# otherwise), None (never tracked: every site no create tool made).
#
# Two cases are NOT proof the build is over, so they keep the stored value:
#   * a newest run that is ``cancelled`` / ``interrupted`` and ended less than
#     ``SUPERSEDE_GRACE_S`` ago. A follow-up message cancels the run in flight
#     before its successor exists; past the window (a plain Stop, a dead worker)
#     it settles like any other ending;
#   * a newest run whose reply's ask is ANSWERED. The answer is persisted before
#     the next run is created, so that run is about to exist.
#
# Two writers settle it, both conditional on the stored value so a race only
# loses a no-op: ``settle_for_session`` after a completed / failed run
# (``chat.runs.service``), and ``resolve`` on the list / by-pocket status reads,
# which self-heals everything else. The reads only look up rows that are
# generating/waiting and not deployed, batched: zero extra queries when none are.
# The settle also writes deployed rows, so a site published mid-build does not
# keep a stale value that every later run re-fetches.
#
# Invariants: this flag is independent of ``status`` / ``deployed`` / ``build_*``;
# nothing in publish, serving, previews, billing or the build lane reads it, and a
# deployed site always reports None on the wire (``wire_fields``). Everything here
# is best-effort: a failure leaves the stored value and never fails a read.

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.chat.runs.domain import SessionRunState

logger = logging.getLogger(__name__)

# How long a cancelled / interrupted newest run is given for its successor to
# appear before it counts as the end of the build.
SUPERSEDE_GRACE_S = 60
_SUPERSEDABLE = ("cancelled", "interrupted")

Generation = Literal["generating", "waiting", "ready"]

GENERATING: Generation = "generating"
WAITING: Generation = "waiting"
READY: Generation = "ready"
_UNSETTLED = (GENERATING, WAITING)


def _needs_lookup(doc: _SiteDoc, include_deployed: bool) -> bool:
    return (
        getattr(doc, "generation", None) in _UNSETTLED
        and bool(getattr(doc, "generation_session_id", None))
        and (include_deployed or not getattr(doc, "deployed", False))
    )


def wire_fields(doc: _SiteDoc | None) -> tuple[Generation | None, str | None]:
    """``(generation, generation_session_id)`` as a client should see them.

    A deployed site reports neither, whatever is stored, so a live site can never
    be shown as building."""
    if doc is None or getattr(doc, "deployed", False):
        return None, None
    return getattr(doc, "generation", None), getattr(doc, "generation_session_id", None)


async def _latest_session_runs(
    workspace_id: str, session_ids: list[str]
) -> dict[str, SessionRunState]:
    from pocketpaw_ee.cloud.chat.runs import service as runs_service

    return await runs_service.latest_session_runs(workspace_id, session_ids)


async def _ask_states(message_ids: list[str]) -> dict[str, bool]:
    if not message_ids:
        return {}
    from pocketpaw_ee.cloud.chat import message_service

    return await message_service.ask_states_for_messages(message_ids)


def _next_value(run: SessionRunState | None, asks: dict[str, bool]) -> Generation | None:
    """What the flag should read given the session's newest run; None = keep it."""
    from pocketpaw_ee.cloud.chat.runs.service import ACTIVE_RUN_STATUSES

    if run is None:
        return READY
    if run.status in ACTIVE_RUN_STATUSES:
        return GENERATING
    if run.status in _SUPERSEDABLE and run.ended_at is not None:
        if (datetime.now(UTC) - run.ended_at).total_seconds() < SUPERSEDE_GRACE_S:
            return None  # a follow-up may be about to start the next run
    answered = asks.get(run.assistant_message_id) if run.assistant_message_id else None
    if answered is None:
        return READY
    if answered:
        return None  # the answer lands before its run is created
    return WAITING


async def resolve(
    workspace_id: str, docs: Iterable[_SiteDoc], *, include_deployed: bool = False
) -> None:
    """Bring each unsettled doc's ``generation`` up to date, in memory and in Mongo.

    One run read and at most one message read for the whole batch, and nothing at
    all when no doc is generating/waiting. Deployed rows are skipped unless
    ``include_deployed`` (the wire hides their value either way)."""
    pending = [doc for doc in docs if _needs_lookup(doc, include_deployed)]
    if not pending:
        return
    try:
        from pocketpaw_ee.cloud.chat.runs.service import ACTIVE_RUN_STATUSES

        latest = await _latest_session_runs(
            workspace_id, sorted({str(doc.generation_session_id) for doc in pending})
        )
        asks = await _ask_states(
            [
                run.assistant_message_id
                for run in latest.values()
                if run.status not in ACTIVE_RUN_STATUSES and run.assistant_message_id
            ]
        )
    except Exception:  # noqa: BLE001 — a stale flag beats a failed gallery read
        logger.warning("sites.generation: resolve lookup failed", exc_info=True)
        return

    collection = _SiteDoc.get_pymongo_collection()
    for doc in pending:
        new = _next_value(latest.get(str(doc.generation_session_id)), asks)
        old = doc.generation
        if new is None or new == old:
            continue
        doc.generation = new
        try:
            await collection.update_one(
                {"_id": doc.id, "generation": old}, {"$set": {"generation": new}}
            )
        except Exception:  # noqa: BLE001 — the next read retries the write-back
            logger.warning("sites.generation: write-back failed for %s", doc.id, exc_info=True)


async def settle_for_session(workspace_id: str, session_id: str) -> None:
    """Settle the sites a session's run was generating, after that run ended.

    One indexed read (``generation_session_id``) when the session created no site,
    which is nearly every run."""
    if not session_id:
        return
    docs = await _SiteDoc.find(
        {
            "generation_session_id": session_id,
            "workspace": workspace_id,
            "generation": {"$in": list(_UNSETTLED)},
        }
    ).to_list()
    await resolve(workspace_id, docs, include_deployed=True)
