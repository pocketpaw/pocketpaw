# ee/pocketpaw_ee/cloud/growth/followups.py — the daily follow-up sweep that
# closes the /growth outbound cycle (G-7).
#
# A draft that went out N days ago and got no reply produces a SECOND draft —
# a short nudge — which is filed straight back into the Instinct tray through
# the EXISTING ``_growth_send`` propose path. Nothing here approves anything
# and nothing here sends: the sweep's terminal state is a ``proposed`` draft
# plus a pending Action a human decides on, exactly like a first touch typed
# by hand. That is the whole point of the slice — the loop closes without the
# gate opening.
#
# Sweep rules, per (workspace, prospect, channel) thread:
#   * the thread's most recent SENT draft must be older than
#     ``GROWTH_FOLLOWUP_DELAY_DAYS`` (default 4),
#   * the prospect must not be ``replied`` (they answered — stop) or ``dead``
#     (already retired), and no draft in the thread may be ``replied``,
#   * no follow-up may already be OPEN in the thread (draft / proposed /
#     approved) — that one is the human's move, not ours. This is also what
#     makes the sweep idempotent: the follow-up it just filed blocks the next
#     pass,
#   * at most ``GROWTH_FOLLOWUP_MAX`` (default 2) follow-ups per thread. On
#     the pass where a capped thread comes due again, the prospect is retired
#     to ``dead`` instead — we stop touching them.
#
# WHO PROPOSES: a cron has no user, but the gate's execute-time RBAC re-check
# (``executor._proposer_still_authorized``) resolves a REAL user, so a
# "system"-proposed follow-up would be approvable and then fail closed at
# dispatch. The sweep therefore inherits the human who proposed the thread's
# last send — read off that draft's own ``_growth_send`` Action. Unresolvable
# proposer → the thread is SKIPPED, because a follow-up nobody can execute is
# worse than none.
#
# Time is INJECTED (``now=``) rather than read from the clock inside the loop,
# so the tests freeze it instead of sleeping.
#
# Created 2026-07-27 (feat/growth-g7): new module.
# Updated 2026-07-28 (feat/growth-projects): a prospect may be just a domain, so
# ``render_followup`` no longer assumes a company. The body already degraded
# (``there`` / ``your team``); the generated subject now drops the dash instead
# of sending "Following up —", which reads as a template that failed to render.
# Also: the sweep now GROUPS BY CLIENT. Threads are partitioned by (workspace,
# project) before they are worked, so one client's follow-ups run together and
# go out under that client's sender identity, and the pass logs a per-client
# created count instead of one undifferentiated number. The partition is over
# the same collapsed thread map the flat loop walked — one thread, one
# prospect, one project — so every thread is still visited exactly once and the
# open-follow-up idempotency guard is untouched. Grouping changes the order the
# work happens in, not how much of it happens.

"""Daily follow-up sweep for the /growth outbound engine."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# The arq job name the cron entry registers under on the ``growth`` queue.
# Explicit + dotted (like ``GROWTH_DISPATCH_JOB_NAME``) so the wire name
# survives a rename of the Python function.
GROWTH_FOLLOWUP_SWEEP_JOB_NAME = "growth.followup_sweep"

# Config defaults. Both are read from the environment at CALL time (not
# import) so a redeploy-free change and the tests' monkeypatching both work.
DEFAULT_FOLLOWUP_DELAY_DAYS = 4
DEFAULT_FOLLOWUP_MAX = 2

# How many sent drafts one pass looks at. The scan is oldest-first, so a
# backlog over this cap sheds the NEWEST sends (the ones furthest from being
# due) and the next daily pass picks them up.
SENT_SCAN_LIMIT = 500
# How far back the proposer lookup reads a workspace's Instinct actions.
ACTION_SCAN_LIMIT = 500

# A follow-up in one of these statuses is still the human's move — the sweep
# must not stack a second one on top of it.
_OPEN_STATUSES = frozenset({"draft", "proposed", "approved"})
# A rejected follow-up was refused by a human; it doesn't burn a cap slot.
_UNCOUNTED_STATUSES = frozenset({"rejected"})
# Prospect statuses the sweep never touches.
_TERMINAL_PROSPECT_STATUSES = frozenset({"replied", "dead"})


def _int_env(name: str, default: int, *, minimum: int) -> int:
    """Read a small positive integer from the environment, fail-soft.

    A missing, blank, non-numeric or out-of-range value logs and falls back to
    the default — a typo'd env var must not take the outbound loop down.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("growth: %s=%r is not an integer — using %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning(
            "growth: %s=%d is below the %d minimum — using %d", name, value, minimum, default
        )
        return default
    return value


def followup_delay_days() -> int:
    """Days of silence after a send before its follow-up comes due."""
    return _int_env("GROWTH_FOLLOWUP_DELAY_DAYS", DEFAULT_FOLLOWUP_DELAY_DAYS, minimum=1)


def followup_max() -> int:
    """Maximum follow-ups per (prospect, channel) before the prospect is retired."""
    return _int_env("GROWTH_FOLLOWUP_MAX", DEFAULT_FOLLOWUP_MAX, minimum=0)


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

# PLACEHOLDER COPY — deliberately dumb, deliberately in code. One short,
# non-pushy nudge per channel that references the original touch so the
# recipient has context without a re-pitch. The /growth crew skill replaces
# this with real per-prospect copy; until then a human still reads every word
# in The Tray before it can go anywhere, so a bland nudge is safe and an
# unreviewed clever one would not be.
_FOLLOWUP_TEMPLATES: dict[str, str] = {
    "email": (
        "Hi {name},\n\n"
        "Following up on my note about {company} — no worries if the timing is off.\n\n"
        "If it's useful I can send over the short version, otherwise I'll leave you to it.\n\n"
        "Original message:\n{quoted}"
    ),
    "linkedin": (
        "Hi {name} — circling back on my earlier note about {company}. "
        "Happy to drop the short version if it's useful, otherwise no worries at all."
    ),
    "whatsapp": (
        "Hi {name}, following up on my earlier message about {company}. "
        "No rush — happy to send the short version if it's useful."
    ),
}
# Any channel we have no template for falls back to this.
_FOLLOWUP_FALLBACK = (
    "Hi {name} — following up on my earlier message about {company}. "
    "No worries if the timing is off."
)
# How much of the original touch gets quoted back in the email nudge.
_QUOTE_LIMIT = 500


def render_followup(
    *, channel: str, prospect_name: str, prospect_company: str, first_touch: dict[str, Any]
) -> tuple[str | None, str]:
    """Render the follow-up's ``(subject, body)`` from the original touch.

    ``subject`` is email-only (the DTO refuses one on the other channels) and
    is the original's, ``Re:``-prefixed — so the nudge threads under the first
    touch in the recipient's client instead of arriving as a new conversation.
    """
    template = _FOLLOWUP_TEMPLATES.get(channel, _FOLLOWUP_FALLBACK)
    original_body = str(first_touch.get("body") or "")
    quoted = original_body[:_QUOTE_LIMIT].strip()
    body = template.format(
        name=prospect_name or "there",
        company=prospect_company or "your team",
        quoted=quoted,
    )

    subject: str | None = None
    if channel == "email":
        original_subject = str(first_touch.get("subject") or "").strip()
        if original_subject:
            subject = (
                original_subject
                if original_subject.lower().startswith("re:")
                else f"Re: {original_subject}"
            )
        else:
            # A prospect can be just a domain, so the company may be empty.
            # Drop the dash rather than sending "Following up —", which reads
            # like a template that failed to render.
            company = prospect_company.strip()
            subject = f"Following up — {company}" if company else "Following up"
        subject = subject[:200]
    return subject, body


# ---------------------------------------------------------------------------
# Proposer resolution
# ---------------------------------------------------------------------------


async def _proposer_index(workspace_id: str) -> dict[str, str]:
    """Map ``draft_id -> proposer user id`` for one workspace's gated sends.

    Read off each ``_growth_send`` Action's own blob. Newest-first, so a draft
    that somehow carries two proposals resolves to the most recent one. Read
    ONLY — the sweep never writes to Instinct outside the propose path. Any
    failure yields an empty map, which makes every thread in that workspace
    skip rather than file an unexecutable follow-up.
    """
    try:
        from pocketpaw.stores import get_instinct_store
        from pocketpaw_ee.cloud.growth.propose import GROWTH_SEND_PARAM_KEY

        store = get_instinct_store(workspace_id=workspace_id or None)
        actions = await store.list_actions(
            pocket_id=workspace_id, workspace_id=workspace_id, limit=ACTION_SCAN_LIMIT
        )
    except Exception:  # noqa: BLE001 — an unreadable tray must not crash the sweep
        logger.warning(
            "growth: could not read the Instinct tray for workspace %s — skipping its "
            "follow-ups this pass",
            workspace_id,
            exc_info=True,
        )
        return {}

    index: dict[str, str] = {}
    for action in actions:
        blob = (getattr(action, "parameters", None) or {}).get(GROWTH_SEND_PARAM_KEY)
        if not isinstance(blob, dict):
            continue
        draft_id = str(blob.get("draft_id") or "")
        requested_by = str(blob.get("requested_by") or "")
        if draft_id and requested_by and draft_id not in index:
            index[draft_id] = requested_by
    return index


def _system_context(workspace_id: str, user_id: str, now: datetime) -> Any:
    """A RequestContext standing in for the human whose thread this continues.

    The propose path is written against a request envelope; the sweep has no
    request, so it mints one carrying the inherited proposer. Everything
    downstream (the Action's trigger, its assignee, the execute-time RBAC
    re-check) then resolves to a real user — which is exactly what makes the
    follow-up approvable in The Tray.
    """
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id=f"growth-followup-{uuid4().hex[:12]}",
        scope=ScopeKind.WORKSPACE,
        started_at=now,
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _latest_sent_per_thread(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict]:
    """Collapse the sent-draft scan to the newest send per thread.

    The delay is measured from the LAST touch, not the first — otherwise a
    thread that already had a follow-up would come due again immediately.
    """
    threads: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["workspace_id"]), str(row["prospect_id"]), str(row["channel"]))
        current = threads.get(key)
        row_sent = _as_utc(row.get("sent_at"))
        if current is None:
            threads[key] = row
            continue
        current_sent = _as_utc(current.get("sent_at"))
        if row_sent is not None and (current_sent is None or row_sent > current_sent):
            threads[key] = row
    return threads


# One thread, resolved far enough to know which client it belongs to:
# ``((prospect_id, channel), latest_sent_row, prospect)``.
_DueThread = tuple[tuple[str, str], dict[str, Any], Any]


async def _group_due_threads(
    growth_service: Any,
    threads: dict[tuple[str, str, str], dict[str, Any]],
    *,
    cutoff: datetime,
    counters: dict[str, int],
) -> dict[tuple[str, str | None], list[_DueThread]]:
    """Partition the due threads by ``(workspace, project)``.

    WHY GROUP AT ALL. A follow-up goes out under the sender identity of the
    client whose project owns the prospect (``growth/connector.py``). Working a
    client's threads together is what makes that legible in the logs and gives
    the pass a per-client "n proposed" line an operator can read — the sweep
    stops being one undifferentiated queue for an agency running eight clients.

    WHY THIS IS NOT N SWEEPS. The partition is over the SAME collapsed thread
    map the single loop used to walk. A thread has one prospect, a prospect has
    at most one project, so every thread lands in exactly one group and is
    visited exactly once. The open-follow-up idempotency guard is untouched and
    stays where it was, inside the per-thread body: grouping changes the ORDER
    the threads are worked, never how many times.

    The cheap checks stay in front of the expensive one, exactly as before: a
    thread that isn't due yet is skipped WITHOUT a prospect read, so a backlog
    of not-yet-due threads costs no more than it did.
    """
    groups: dict[tuple[str, str | None], list[_DueThread]] = {}
    for (workspace_id, prospect_id, channel), latest in threads.items():
        counters["threads"] += 1
        try:
            sent_at = _as_utc(latest.get("sent_at"))
            if sent_at is None or sent_at > cutoff:
                counters["skipped"] += 1
                continue

            prospect = await growth_service.get_prospect_system(workspace_id, prospect_id)
            if prospect.status in _TERMINAL_PROSPECT_STATUSES:
                counters["skipped"] += 1
                continue
        except Exception:  # noqa: BLE001 — one unreadable thread, not a dead pass
            counters["skipped"] += 1
            logger.exception(
                "growth: follow-up sweep could not resolve prospect %s on '%s' (workspace=%s)",
                prospect_id,
                channel,
                workspace_id,
            )
            continue

        project_id = getattr(prospect, "project_id", None)
        groups.setdefault((workspace_id, project_id), []).append(
            ((prospect_id, channel), latest, prospect)
        )
    return groups


async def _retract(growth_service: Any, workspace_id: str, draft_id: str) -> None:
    """Reject a follow-up draft whose proposal never made it to the tray.

    Best-effort — the caller is already unwinding, so a failure here just gets
    logged. ``draft → rejected`` is a legal edge and terminal, so the retracted
    row neither blocks the thread nor burns a cap slot.
    """
    try:
        await growth_service.gate_transition(workspace_id, draft_id, "rejected")
    except Exception:  # noqa: BLE001 — cleanup on an error path
        logger.warning(
            "growth: could not retract un-proposed follow-up draft %s — it will "
            "block this thread until a human clears it",
            draft_id,
            exc_info=True,
        )


async def followup_sweep(ctx: dict[str, Any], *, now: datetime | None = None) -> dict[str, int]:
    """Propose a follow-up for every outbound thread that went quiet (G-7).

    Registered as a daily arq cron on the ``growth`` queue. ``ctx`` is arq's
    job context (unused — the sweep resolves everything it needs itself);
    ``now`` is the injectable clock the tests freeze.

    Returns a counter dict for the worker log: how many threads were looked
    at, how many follow-ups were proposed, how many prospects were retired,
    and how many threads were skipped (not due, replied, already open, or no
    resolvable proposer). NEVER raises — one bad thread must not take the
    whole daily pass down.

    Work is GROUPED BY CLIENT (``_group_due_threads``) — one project's threads
    together, so a follow-up goes out under that client's sender identity and
    the pass logs a per-client line. The grouping is a partition of the same
    thread map the flat loop walked, so every thread is still visited exactly
    once and the open-follow-up idempotency guard is unaffected.
    """
    from pocketpaw_ee.cloud.growth import service as growth_service
    from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest

    now = _as_utc(now) or datetime.now(UTC)
    delay_days = followup_delay_days()
    max_followups = followup_max()
    cutoff = now - timedelta(days=delay_days)

    counters = {"threads": 0, "created": 0, "retired": 0, "skipped": 0}
    proposer_cache: dict[str, dict[str, str]] = {}
    # How many follow-ups each client got this pass — the log line an agency
    # operator actually reads. Not part of the returned counters, which the
    # worker's contract fixes at four keys.
    per_project: defaultdict[tuple[str, str | None], int] = defaultdict(int)

    try:
        sent_rows = await growth_service.list_sent_drafts_for_followup(limit=SENT_SCAN_LIMIT)
    except Exception:  # noqa: BLE001 — a failed scan is a no-op pass, not a crash
        logger.exception("growth: follow-up sweep could not read sent drafts")
        return counters

    groups = await _group_due_threads(
        growth_service, _latest_sent_per_thread(sent_rows), cutoff=cutoff, counters=counters
    )

    for (workspace_id, project_id), members in groups.items():
        for (prospect_id, channel), latest, prospect in members:
            try:
                thread = await growth_service.list_channel_drafts(
                    workspace_id, prospect_id, channel
                )
                if any(d["status"] == "replied" for d in thread):
                    # The draft-level reply signal, for a prospect row that
                    # hasn't caught up yet. Same verdict: they answered, stop
                    # nudging.
                    counters["skipped"] += 1
                    continue

                followups = [d for d in thread if d["variant"] == "follow_up"]
                if any(d["status"] in _OPEN_STATUSES for d in followups):
                    # One is already waiting on a human — including the one this
                    # sweep filed on its last pass. THIS IS THE IDEMPOTENCY
                    # GUARD, and grouping does not touch it: a thread has one
                    # prospect, so it lands in exactly one group and is visited
                    # exactly once per pass. Grouping changes the ORDER threads
                    # are worked, never how many times.
                    counters["skipped"] += 1
                    continue

                counted = [d for d in followups if d["status"] not in _UNCOUNTED_STATUSES]
                if len(counted) >= max_followups:
                    await growth_service.mark_prospect_dead(workspace_id, prospect_id)
                    counters["retired"] += 1
                    logger.info(
                        "growth: prospect %s retired to dead after %d follow-ups on '%s' "
                        "with no reply (workspace=%s project=%s)",
                        prospect_id,
                        len(counted),
                        channel,
                        workspace_id,
                        project_id or "-",
                    )
                    continue

                if workspace_id not in proposer_cache:
                    proposer_cache[workspace_id] = await _proposer_index(workspace_id)
                fallback = thread[0] if thread else latest
                first_touch = next((d for d in thread if d["variant"] == "first_touch"), fallback)
                index = proposer_cache[workspace_id]
                proposer = index.get(str(latest["id"])) or index.get(str(first_touch["id"]))
                if not proposer:
                    logger.warning(
                        "growth: no proposer resolvable for draft %s — skipping its follow-up "
                        "(an unapprovable proposal is worse than none)",
                        latest["id"],
                    )
                    counters["skipped"] += 1
                    continue

                subject, body = render_followup(
                    channel=channel,
                    prospect_name=prospect.name,
                    prospect_company=prospect.company,
                    first_touch=first_touch,
                )
                draft = await growth_service.create_followup_draft(
                    workspace_id,
                    prospect_id,
                    CreateDraftRequest(
                        channel=channel,  # type: ignore[arg-type]
                        subject=subject,
                        body=body,
                        variant="follow_up",
                        demo_url=first_touch.get("demo_url"),
                    ),
                )
                # The EXISTING gate path: files the ``_growth_send`` Action and
                # flips the draft draft→proposed. It stops there — approval and
                # dispatch stay the human's, exactly as for a first touch.
                try:
                    await growth_service.propose_send(
                        _system_context(workspace_id, proposer, now), draft.id
                    )
                except Exception:
                    # Retract the draft we just created. Left in ``draft`` status
                    # it would count as an OPEN follow-up and block this thread on
                    # every future pass — a silent, permanent stall. ``rejected``
                    # is terminal, doesn't burn a cap slot, and lets the next pass
                    # try again cleanly.
                    await _retract(growth_service, workspace_id, draft.id)
                    raise
                counters["created"] += 1
                per_project[(workspace_id, project_id)] += 1
                logger.info(
                    "growth: follow-up %d/%d proposed for prospect %s on '%s' "
                    "(workspace=%s project=%s)",
                    len(counted) + 1,
                    max_followups,
                    prospect_id,
                    channel,
                    workspace_id,
                    project_id or "-",
                )
            except Exception:  # noqa: BLE001 — one bad thread must not stop the pass
                counters["skipped"] += 1
                logger.exception(
                    "growth: follow-up sweep failed for prospect %s on '%s' "
                    "(workspace=%s project=%s)",
                    prospect_id,
                    channel,
                    workspace_id,
                    project_id or "-",
                )

    logger.info(
        "growth.followup_sweep threads=%d created=%d retired=%d skipped=%d "
        "projects=%d (delay=%dd max=%d)",
        counters["threads"],
        counters["created"],
        counters["retired"],
        counters["skipped"],
        len(groups),
        delay_days,
        max_followups,
    )
    for (workspace_id, project_id), created in sorted(
        per_project.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        logger.info(
            "growth.followup_sweep workspace=%s project=%s created=%d",
            workspace_id,
            project_id or "-",
            created,
        )
    return counters


__all__ = [
    "GROWTH_FOLLOWUP_SWEEP_JOB_NAME",
    "followup_delay_days",
    "followup_max",
    "followup_sweep",
    "render_followup",
]
