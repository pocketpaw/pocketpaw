# src/pocketpaw/lens_checkins.py — scheduled-automation check-ins to paw-lens.
#
# paw-lens tracks every scheduled automation as a "monitor" from Sentry-style
# check-ins: ``in_progress`` when a run starts, ``ok`` or ``error`` when it ends,
# POSTed to ``{lens_api_url}/v1/checkins`` with the ``X-Lens-Token`` header. The
# first check-in for an unknown monitor registers it, schedule included. Slug is
# ``<kind>:<id>``.
#
# ``automation_run`` wraps one run. It also stamps ``paw.workspace_id`` /
# ``paw.automation.kind`` / ``paw.automation.id`` (observability.baggage, a
# ContextVar, not OTel baggage) on every span the run opens. A cancelled run
# posts no final check-in. ``monitored_job`` is the APScheduler form:
# wrap the job function once where it is handed to ``add_job``.
#
# Invariants: a check-in NEVER raises into the job and never blocks it (posts
# run as background tasks with a 1 s timeout); no URL = zero requests; the job's
# own exception is re-raised unchanged; the token is never logged. Lives in OSS
# core because reminders, intentions and heartbeats run here; ee wraps it in
# ``pocketpaw_ee.cloud._core.periodic``.
"""Fire-and-forget paw-lens check-ins plus run span attribution."""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import datetime
from typing import Any

import httpx

from pocketpaw.observability import baggage

logger = logging.getLogger(__name__)

TIMEOUT_S = 1.0
MAX_ERROR_CHARS = 500

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
_pending: set[asyncio.Task[None]] = set()
#: Tests inject an ``httpx.MockTransport`` here.
_transport: httpx.AsyncBaseTransport | None = None


def _endpoint() -> tuple[str, str] | None:
    """(checkins URL, token), or None when paw-lens is not configured."""
    try:
        from pocketpaw.config import get_settings

        settings = get_settings()
        base = (settings.lens_api_url or "").strip().rstrip("/")
        token = settings.lens_api_token or ""
    except Exception:  # noqa: BLE001 — bad settings must not reach the job
        return None
    return (f"{base}/v1/checkins", token) if base else None


def _get_client() -> httpx.AsyncClient:
    """One shared client per event loop; rebuilt if the loop changed."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=TIMEOUT_S, transport=_transport)
        _client_loop = loop
    return _client


def _suppressed() -> Any:
    """Keep the check-in POST out of the traces (instrument_httpx would span it)."""
    try:
        import logfire
    except ImportError:
        return nullcontext()
    return logfire.suppress_instrumentation()


async def _post(url: str, token: str, body: dict[str, Any], after: Any = None) -> None:
    if after is not None:
        with suppress(BaseException):
            await after
    try:
        with _suppressed():
            resp = await asyncio.wait_for(
                _get_client().post(url, json=body, headers={"X-Lens-Token": token}),
                TIMEOUT_S,
            )
        if resp.status_code >= 400:
            logger.debug("lens check-in %s rejected: %s", body["monitor"], resp.status_code)
    except BaseException as exc:  # noqa: BLE001 — paw-lens down is never the job's problem
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.debug("lens check-in %s failed: %s", body["monitor"], type(exc).__name__)


def _spawn(coro: Awaitable[None]) -> asyncio.Task[None]:
    task = asyncio.ensure_future(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def flush(timeout: float = 2.0) -> None:
    """Wait up to ``timeout`` for in-flight check-ins. Shutdown and tests. Never raises."""
    try:
        if _pending:
            await asyncio.wait(set(_pending), timeout=timeout)
    except Exception:  # noqa: BLE001 — shutdown must not trip on telemetry
        logger.debug("lens check-in flush failed", exc_info=True)


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    try:
        from pocketpaw.logging_setup import _scrub

        text = _scrub(text)
    except Exception:  # noqa: BLE001
        pass
    return text[:MAX_ERROR_CHARS]


# A known Monday..Sunday week, used to ask an APScheduler field which weekdays it matches.
_WEEK = [datetime(2024, 1, d) for d in range(1, 8)]


def _crontab_dow(field: Any) -> str:
    """APScheduler day_of_week field → numeric crontab day_of_week.

    APScheduler counts 0=Monday..6=Sunday, crontab 0=Sunday..6=Saturday, so the
    field is evaluated (names, ranges, lists, steps alike) to the weekdays it
    really fires on, then written as a crontab list. A wrapped crontab range
    (fri-sun) is invalid, which is why this emits a list, never a range.
    """
    if str(field) == "*":
        return "*"
    days = {
        d.weekday()
        for d in _WEEK
        if any(expr.get_next_value(d, field) == d.weekday() for expr in field.expressions)
    }
    if len(days) == 7:
        return "*"
    return ",".join(str(n) for n in sorted((d + 1) % 7 for d in days))


def schedule_from_trigger(trigger: Any) -> dict[str, Any] | None:
    """APScheduler trigger → check-in ``schedule``. None for one-shot triggers."""
    fields = getattr(trigger, "fields", None)
    if fields:
        by_name = {f.name: f for f in fields}
        parts = [
            str(by_name[k]) if k in by_name else "*" for k in ("minute", "hour", "day", "month")
        ]
        dow = by_name.get("day_of_week")
        parts.append(_crontab_dow(dow) if dow is not None else "*")
        return {"crontab": " ".join(parts)}
    interval = getattr(trigger, "interval", None)
    if interval is not None and hasattr(interval, "total_seconds"):
        return {"interval_seconds": int(interval.total_seconds())}
    return None


@asynccontextmanager
async def automation_run(
    kind: str,
    id: str,
    *,
    schedule: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    timezone: str | None = None,
    checkin_margin_s: int | None = None,
    max_runtime_s: int | None = None,
) -> AsyncIterator[None]:
    """Check in around one automation run and stamp its spans with ``paw.*`` attributes."""
    if workspace_id is None:
        from pocketpaw.stores import current_workspace

        workspace_id = current_workspace.get()
    workspace_id = workspace_id or None
    endpoint = _endpoint()
    base: dict[str, Any] = {
        "monitor": f"{kind}:{id}",
        "checkin_id": uuid.uuid4().hex,
        "kind": kind,
    }
    for key, value in (
        ("schedule", schedule),
        ("timezone", timezone),
        ("checkin_margin_s", checkin_margin_s),
        ("max_runtime_s", max_runtime_s),
        ("workspace_id", workspace_id),
    ):
        if value is not None:
            base[key] = value

    started = _spawn(_post(*endpoint, {**base, "status": "in_progress"})) if endpoint else None
    status: str | None
    status, error = "ok", None
    try:
        with baggage(
            **{
                "paw.workspace_id": workspace_id,
                "paw.automation.kind": kind,
                "paw.automation.id": id,
            }
        ):
            yield
    except Exception as exc:
        status, error = "error", _error_text(exc)
        raise
    except BaseException:
        # Cancellation (shutdown), KeyboardInterrupt, GeneratorExit: not a failed
        # run. Post nothing; paw-lens times the run out via max_runtime if set.
        status = None
        raise
    finally:
        if endpoint and status is not None:
            body = {**base, "status": status}
            if error:
                body["error"] = error
            _spawn(_post(*endpoint, body, after=started))


def monitored_job(
    func: Callable[..., Awaitable[Any]],
    *,
    kind: str,
    id: str,
    trigger: Any = None,
    workspace_id: str | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap an APScheduler async job function so each fire is one ``automation_run``."""
    schedule = schedule_from_trigger(trigger)

    @functools.wraps(func)
    async def _job(*args: Any, **kwargs: Any) -> Any:
        async with automation_run(kind, id, schedule=schedule, workspace_id=workspace_id):
            return await func(*args, **kwargs)

    _job.__lens_monitor__ = (kind, id)  # type: ignore[attr-defined]
    return _job
