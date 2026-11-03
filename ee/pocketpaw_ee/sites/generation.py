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
# Two writers settle it, both conditional on the stored value so a race only
# loses a no-op: ``settle_for_session`` from every terminal run transition
# (``chat.runs.service``), and ``resolve`` on the list / by-pocket status reads,
# which self-heals a row whose run ended without the hook firing (a swept run, a
# crashed worker). ``resolve`` only looks up rows that are generating/waiting and
# not deployed, batched: zero extra queries when none are.
#
# Invariants: this flag is independent of ``status`` / ``deployed`` / ``build_*``;
# nothing in publish, serving, previews, billing or the build lane reads it, and a
# deployed site always reports None on the wire (``wire_fields``). Everything here
# is best-effort: a failure leaves the stored value and never fails a read.

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Literal

from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

logger = logging.getLogger(__name__)

Generation = Literal["generating", "waiting", "ready"]

GENERATING: Generation = "generating"
WAITING: Generation = "waiting"
READY: Generation = "ready"
_UNSETTLED = (GENERATING, WAITING)


def _needs_lookup(doc: _SiteDoc) -> bool:
    return (
        getattr(doc, "generation", None) in _UNSETTLED
        and bool(getattr(doc, "generation_session_id", None))
        and not getattr(doc, "deployed", False)
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
) -> dict[str, tuple[str, str | None]]:
    from pocketpaw_ee.cloud.chat.runs import service as runs_service

    return await runs_service.latest_session_runs(workspace_id, session_ids)


async def _unanswered_asks(message_ids: list[str]) -> set[str]:
    if not message_ids:
        return set()
    from pocketpaw_ee.cloud.chat import message_service

    return await message_service.unanswered_ask_message_ids(message_ids)


async def resolve(workspace_id: str, docs: Iterable[_SiteDoc]) -> None:
    """Bring each unsettled doc's ``generation`` up to date, in memory and in Mongo.

    One run read and at most one message read for the whole batch, and nothing at
    all when no doc is generating/waiting."""
    pending = [doc for doc in docs if _needs_lookup(doc)]
    if not pending:
        return
    try:
        from pocketpaw_ee.cloud.chat.runs.service import ACTIVE_RUN_STATUSES

        latest = await _latest_session_runs(
            workspace_id, sorted({str(doc.generation_session_id) for doc in pending})
        )
        ended_reply_ids = [
            msg_id
            for status, msg_id in latest.values()
            if status not in ACTIVE_RUN_STATUSES and msg_id
        ]
        waiting_ids = await _unanswered_asks(ended_reply_ids)
    except Exception:  # noqa: BLE001 — a stale flag beats a failed gallery read
        logger.warning("sites.generation: resolve lookup failed", exc_info=True)
        return

    collection = _SiteDoc.get_pymongo_collection()
    for doc in pending:
        run = latest.get(str(doc.generation_session_id))
        new: Generation
        if run is not None and run[0] in ACTIVE_RUN_STATUSES:
            new = GENERATING
        elif run is not None and run[1] and run[1] in waiting_ids:
            new = WAITING
        else:
            new = READY
        old = doc.generation
        if new == old:
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
    await resolve(workspace_id, docs)
