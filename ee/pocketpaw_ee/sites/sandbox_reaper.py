# ee/pocketpaw_ee/sites/sandbox_reaper.py — finds and deletes site build / verify
# sandboxes that no caller holds an id for any more, so they stop holding the Daytona
# org's memory cap (10 GiB; a build box is 4 GiB, a verify box 2 GiB).
#
# Three layers, all keyed on things a sandbox carries on Daytona's side rather than on
# our process state, because the leak cases are exactly the ones where that state died:
#
#   * ``teardown_unreturned`` — run by ``daytona_runner.run_build`` and
#     ``browser_check.run_standalone`` when create raised or was cancelled before it
#     returned an id. The SDK registers the box before it waits for ``started``, so the
#     box can exist while our ``sandbox_id`` is None. Looks the box up by its unique
#     name and deletes it. Shielded, so the cancel that got us here cannot cancel the
#     delete too.
#   * ``delete_job_leftovers`` — run before a create that carries a ``paw_job`` label.
#     An arq re-run of the same job (a worker killed by a redeploy, then retried) finds
#     the first attempt's box by that label and deletes it before making a second.
#   * ``reap_orphan_build_sandboxes`` + ``reap_cron`` — the backstop, an arq cron on the
#     site-build worker (``build_worker.WorkerSettings.cron_jobs``). Deletes any
#     ``paw-build-*`` / ``paw-verify-*`` box older than the longest sites job budget
#     plus a margin, in any state, including one stuck in ``creating`` that Daytona's
#     own auto-stop never touches. Matches by NAME so boxes created before labels
#     existed are reaped too. Never touches workspace VMs (``paw-ws-*``) or other names.
#
# Label values are bounded by ``label_value`` (<= 63 chars, Kubernetes charset) both
# where they are stamped and where they are filtered or matched, so a long arq job id
# never fails a create and lookups still find the box.
#
# Every function here swallows and logs Daytona errors: cleanup must never replace the
# exception (or result) of the build it is cleaning up after.
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from arq import cron

logger = logging.getLogger(__name__)

#: Name prefixes of the sandboxes this module may delete. Workspace VMs (``paw-ws-``)
#: and Code Mode sandboxes are deliberately absent: they hold user state.
REAPABLE_PREFIXES: tuple[str, ...] = ("paw-build-", "paw-verify-")

#: Added to the longest sites job timeout before a box counts as orphaned. Under an
#: hour on purpose: the job is long dead by then, and the box is holding quota.
REAP_MARGIN_SECONDS = 15 * 60

#: Label every lane stamps; the value names the lane (``site-build`` / ``site-verify``).
LANE_LABEL = "paw_lane"
#: Label the build jobs stamp with their arq job id.
JOB_LABEL = "paw_job"

# Cleanup tasks that outlived a second cancel of their caller. Held so the event loop
# does not garbage-collect a running delete.
_background: set[asyncio.Task[Any]] = set()


#: Longest label value sent to Daytona. Kubernetes' limit, assumed in case Daytona
#: enforces it: a rejected label would fail every create, which is worse than a leak.
LABEL_VALUE_MAX = 63
_LABEL_VALUE_OK = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_LABEL_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_LABEL_TAIL_HEX = 12


def label_value(value: str) -> str:
    """A label value Kubernetes-style validation accepts (<= 63 chars, ``[A-Za-z0-9._-]``,
    alphanumeric at both ends).

    A value that already passes is returned as-is. Anything else becomes a readable
    prefix plus a sha256 tail of the ORIGINAL value, so it is deterministic (a filter
    built from the same input matches the stamped label) and idempotent (a bounded
    value passes through unchanged). Every label we stamp or filter on goes through it.
    """
    value = str(value)
    if _LABEL_VALUE_OK.match(value):
        return value
    tail = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_LABEL_TAIL_HEX]
    room = LABEL_VALUE_MAX - _LABEL_TAIL_HEX - 1
    prefix = _LABEL_BAD_CHARS.sub("-", value)[:room]
    prefix = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", prefix)
    return f"{prefix}-{tail}" if prefix else tail


def lane_labels(lane: str, labels: dict[str, str] | None) -> dict[str, str]:
    """The labels a create sends: the lane label, with the caller's labels over it,
    every value bounded by :func:`label_value`."""
    merged = {LANE_LABEL: lane, **(labels or {})}
    return {k: label_value(v) for k, v in merged.items()}


async def _find_and_delete(client: Any, name: str) -> str | None:
    finder = getattr(client, "find_sandbox_by_name", None)
    if finder is None:
        return None
    try:
        info = await finder(name)
        if info is None:
            return None
        await client.delete_sandbox(info.id)
    except Exception as exc:  # noqa: BLE001 — cleanup never masks the build's own error
        logger.warning("sandbox_reaper: could not tear down unreturned box %s (%s)", name, exc)
        return None
    logger.info("sandbox_reaper: deleted box %s (%s) that create never returned", name, info.id)
    return str(info.id)


async def teardown_unreturned(client: Any, name: str) -> str | None:
    """Delete the box named ``name`` that a failed or cancelled create left behind.

    Returns the deleted id, or None when there was nothing to delete. Runs the lookup
    and delete in a shielded task: called from an ``except`` that is handling a
    ``CancelledError``, so a second cancel must not abandon the delete half-way. If it
    does arrive, the delete carries on in the background and the cancel propagates.
    """
    task = asyncio.ensure_future(_find_and_delete(client, name))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return await asyncio.shield(task)


async def delete_job_leftovers(client: Any, labels: dict[str, str]) -> list[str]:
    """Delete boxes an earlier attempt of the same arq job left behind.

    Only acts when ``labels`` carries a ``paw_job`` and the client can filter by label.
    A job id is in flight on one worker at a time, so a box already labelled with it
    belongs to an attempt whose worker died.
    """
    job = labels.get(JOB_LABEL)
    lane = labels.get(LANE_LABEL)
    if not job or not lane:
        return []
    # The same bound the create stamped with, so filter and match agree with the label.
    job, lane = label_value(job), label_value(lane)
    try:
        found = await client.list_sandboxes(labels={JOB_LABEL: job, LANE_LABEL: lane})
    except Exception as exc:  # noqa: BLE001 — a client without label filtering, or Daytona down
        logger.info("sandbox_reaper: no leftover check for job %s (%s)", job, exc)
        return []
    deleted: list[str] = []
    for info in found:
        if (getattr(info, "labels", None) or {}).get(JOB_LABEL) != job:
            continue  # the filter is the server's; trust only an exact match
        try:
            await client.delete_sandbox(info.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sandbox_reaper: leftover %s of job %s not deleted (%s)", info.id, job, exc
            )
            continue
        logger.info("sandbox_reaper: deleted leftover %s of re-run job %s", info.id, job)
        deleted.append(str(info.id))
    return deleted


def default_max_age_seconds() -> int:
    """The longest sites job budget plus :data:`REAP_MARGIN_SECONDS`."""
    from pocketpaw_ee.sites.build_job import (
        site_build_job_timeout_seconds,
        site_preview_job_timeout_seconds,
    )

    longest = max(site_build_job_timeout_seconds(), site_preview_job_timeout_seconds())
    return longest + REAP_MARGIN_SECONDS


def _parse_created(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def reap_orphan_build_sandboxes(
    client: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> list[str]:
    """Delete ``paw-build-*`` / ``paw-verify-*`` boxes older than ``max_age_seconds``.

    Any state counts, ``creating`` included. A box whose age cannot be read is left
    alone. A failed delete is logged and skipped. Returns the deleted ids.
    """
    now = now or datetime.now(UTC)
    max_age = default_max_age_seconds() if max_age_seconds is None else max_age_seconds
    deleted: list[str] = []
    for info in await client.list_sandboxes():
        name = str(getattr(info, "name", "") or "")
        if not name.startswith(REAPABLE_PREFIXES):
            continue
        created = _parse_created(getattr(info, "created_at", None))
        if created is None or (now - created).total_seconds() <= max_age:
            continue
        try:
            await client.delete_sandbox(info.id)
        except Exception as exc:  # noqa: BLE001 — one stuck box must not stop the sweep
            logger.warning(
                "sandbox_reaper: delete of orphan %s (%s) failed (%s)", name, info.id, exc
            )
            continue
        logger.info(
            "sandbox_reaper: deleted orphan %s (%s, state=%s, created %s)",
            name,
            info.id,
            getattr(info, "state", "?"),
            created.isoformat(),
        )
        deleted.append(str(info.id))
    return deleted


async def reap_build_sandboxes_job(ctx: dict[str, Any]) -> int:
    """arq cron body: reap orphans with the process's Daytona client. No-op when
    Daytona is not configured. Returns how many boxes it deleted."""
    from pocketpaw_ee.cloud.daytona.client import get_daytona_client

    client = get_daytona_client()
    if client is None:
        return 0
    try:
        deleted = await reap_orphan_build_sandboxes(client)
    except Exception as exc:  # noqa: BLE001 — list failed; the next tick tries again
        logger.warning("sandbox_reaper: sweep failed (%s)", exc)
        return 0
    if deleted:
        logger.warning("sandbox_reaper: reaped %d orphaned build sandbox(es)", len(deleted))
    return len(deleted)


#: Every 10 minutes, one run across a scaled-out worker fleet (``unique``).
reap_cron = cron(
    reap_build_sandboxes_job,
    name="sites_build_sandbox_reaper",
    minute={0, 10, 20, 30, 40, 50},
    unique=True,
    run_at_startup=False,
    timeout=300,
)


__all__ = [
    "JOB_LABEL",
    "LANE_LABEL",
    "LABEL_VALUE_MAX",
    "REAPABLE_PREFIXES",
    "REAP_MARGIN_SECONDS",
    "default_max_age_seconds",
    "delete_job_leftovers",
    "label_value",
    "lane_labels",
    "reap_build_sandboxes_job",
    "reap_cron",
    "reap_orphan_build_sandboxes",
    "teardown_unreturned",
]
