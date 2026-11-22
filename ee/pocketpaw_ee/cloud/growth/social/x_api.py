# ee/pocketpaw_ee/cloud/growth/social/x_api.py — the one capped, read-only door
# to the X API for Growth › Social. ``x_read(workspace_id, path, params, meter=)``
# GETs ``https://api.x.com`` + path with the deployment's bearer token
# (``POCKETPAW_GROWTH_X_BEARER_TOKEN``) and returns the JSON object, or None.
#
# Metering. X bills per post returned, not per call, so the meter counts posts.
# ``meter`` picks the daily budget: ``GROWTH_X_READS`` (``growth_x_read_cap``,
# the post-metrics sweep, the default) or ``GROWTH_X_REPLY_READS``
# (``growth_x_reply_read_cap``, the reply-card search). Every call first claims
# one unit with ``metering.try_spend`` (fail closed: the reads are
# platform-paid). After a 200, the posts past the first (``len(data) - 1``) are
# added with ``metering.charge``, past the cap too: X has already billed them,
# and landing them on the meter makes the caller's next read fail closed. The
# response already fetched is still returned. No token, the cap reached, an
# HTTP error, a timeout or a non-object body all log one line and return None;
# nothing here raises. The token never reaches a log line. A claimed read is
# not refunded when the call fails, so the meter over-counts rather than under.
#
# Read-only on purpose: this module only ever issues GET. Nothing in Growth
# writes to X. Tests replace the reader through ``set_x_read_fn``.

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

import httpx

from pocketpaw_ee.cloud.metering.domain import DailyMeter

logger = logging.getLogger(__name__)

X_API_BASE = "https://api.x.com"
X_TIMEOUT_SECONDS = 10.0
TOKEN_ENV = "POCKETPAW_GROWTH_X_BEARER_TOKEN"

# (workspace_id, path, params, *, meter=DailyMeter.GROWTH_X_READS) -> JSON object or None.
XReadFn = Callable[..., Awaitable["dict | None"]]


async def x_read(
    workspace_id: str,
    path: str,
    params: dict | None = None,
    *,
    meter: DailyMeter = DailyMeter.GROWTH_X_READS,
) -> dict | None:
    """GET one X API path for ``workspace_id``, metered per post on ``meter``.
    None when off, capped or failed."""
    from pocketpaw_ee.cloud.metering import service as metering

    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        logger.info("x read skipped: %s is not set", TOKEN_ENV)
        return None
    if meter == DailyMeter.GROWTH_X_REPLY_READS:
        cap = metering.growth_x_reply_read_cap()
    else:
        cap = metering.growth_x_read_cap()
    day = metering.today()
    if not await metering.try_spend(
        subject_type="workspace",
        subject_id=workspace_id,
        meter=meter,
        cap=cap,
        day=day,
    ):
        logger.warning("x read refused: workspace=%s is at today's X read cap", workspace_id)
        return None
    try:
        async with httpx.AsyncClient(timeout=X_TIMEOUT_SECONDS) as http:
            resp = await http.get(
                f"{X_API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("x read failed: %s %s", path, type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("x read failed: %s answered %d", path, resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("x read failed: %s returned no JSON", path)
        return None
    if not isinstance(data, dict):
        return None
    posts = data.get("data")
    if isinstance(posts, list) and len(posts) > 1:
        await metering.charge(
            subject_type="workspace",
            subject_id=workspace_id,
            meter=meter,
            amount=len(posts) - 1,
            day=day,
        )
    return data


_X_READ_FN: XReadFn | None = None


def set_x_read_fn(fn: XReadFn | None) -> None:
    global _X_READ_FN
    _X_READ_FN = fn


def resolve_x_read_fn() -> XReadFn:
    return _X_READ_FN or x_read


__all__ = ["XReadFn", "resolve_x_read_fn", "set_x_read_fn", "x_read"]
