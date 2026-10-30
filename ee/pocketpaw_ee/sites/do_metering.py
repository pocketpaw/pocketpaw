# ee/pocketpaw_ee/sites/do_metering.py: the two cluster sweeps that keep project
# sites' Durable Objects in check after deploy (both no-ops unless
# ``PAW_SITES_DURABLE_OBJECTS`` is on; scheduled in ``extensions._sweeps``).
#
#   * ``sweep_do_usage``: at most once per ``PAW_SITES_DO_METERING_MINUTES`` (60),
#     reads today's DO usage per site script from Cloudflare GraphQL analytics
#     (requests, active time, stored bytes; one query per dataset, keyed by namespace
#     id, mapped back to scripts through the namespaces list), stores it on the Site
#     (``do_usage``, 35 days) and sets ``do_throttled`` while today's requests are past
#     ``PAW_SITES_DO_DAILY_REQUESTS_FREE`` / ``_PAID`` and ``do_suspended`` past
#     ``PAW_SITES_DO_SUSPEND_FACTOR`` (3) times that. EVERY run pushes both current
#     values to the live script (``PAW_DO_THROTTLED`` / ``PAW_DO_SUSPENDED``, through
#     ``durable_objects.set_platform_vars_live``), so a publish that uploaded a stale
#     value is corrected within one run; the flags are stored only once a push
#     worked. After ``PAW_SITES_DO_METERING_FAILURES_ALERT`` (6) failed reads in a row
#     it logs an ERROR naming the likely cause and raises ``DoMeteringUnhealthy`` so
#     the sweep's paw-lens monitor shows red (``metering_status`` has the detail).
#     FAILS OPEN: a request-count read that
#     fails changes nothing (never throttles on missing data); a duration or storage
#     read that fails only leaves those numbers at 0.
#   * ``sweep_do_teardowns``: retries the ``SiteDoTeardown`` rows the delete cascade
#     queues when a DO teardown did not finish: forced delete + namespace check (no
#     stub upload, the script is already deleted), exponential backoff, and after
#     ``PAW_SITES_DO_TEARDOWN_MAX_ATTEMPTS`` an error log and state ``operator``.
#
# The GraphQL dataset and field names follow the Durable Objects analytics docs
# (developers.cloudflare.com/durable-objects/observability/metrics-and-analytics/);
# ``scripts/sites_do_spike.py`` introspects them on a real account.
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pocketpaw_ee.sites import durable_objects

logger = logging.getLogger(__name__)

DAILY_REQUESTS_ENV = {
    False: "PAW_SITES_DO_DAILY_REQUESTS_FREE",
    True: "PAW_SITES_DO_DAILY_REQUESTS_PAID",
}
DEFAULT_DAILY_REQUESTS = {False: 100_000, True: 3_000_000}
METERING_MINUTES_ENV = "PAW_SITES_DO_METERING_MINUTES"
DEFAULT_METERING_MINUTES = 60
SUSPEND_FACTOR_ENV = "PAW_SITES_DO_SUSPEND_FACTOR"
DEFAULT_SUSPEND_FACTOR = 3
FAILURE_ALERT_ENV = "PAW_SITES_DO_METERING_FAILURES_ALERT"
DEFAULT_FAILURE_ALERT = 6
TEARDOWN_MAX_ATTEMPTS_ENV = "PAW_SITES_DO_TEARDOWN_MAX_ATTEMPTS"
DEFAULT_TEARDOWN_MAX_ATTEMPTS = 6
USAGE_DAYS_KEPT = 35
_NAMESPACE_CHUNK = 200

_QUERY = """query DoUsage($account: string!, $date: Date!, $namespaces: [string!]) {
  viewer {
    accounts(filter: {accountTag: $account}) {
      rows: %s(limit: 10000, filter: {date: $date, namespaceId_in: $namespaces}) {
        %s
        dimensions { namespaceId }
      }
    }
  }
}"""
REQUESTS_QUERY = _QUERY % ("durableObjectsInvocationsAdaptiveGroups", "sum { requests }")
DURATION_QUERY = _QUERY % ("durableObjectsPeriodicGroups", "sum { activeTime }")
STORAGE_QUERY = _QUERY % ("durableObjectsStorageGroups", "max { storedBytes }")

_state: dict[str, Any] = {}


class DoMeteringUnhealthy(RuntimeError):
    """Raised by the usage sweep after too many failed analytics reads in a row, so
    the sweep's paw-lens monitor records a failure an operator sees."""


def _reset() -> None:
    _state.update(last_usage_run=None, failures=0, last_error="", last_success=None)


_reset()


def metering_status() -> dict[str, Any]:
    """This process's metering health: consecutive failed reads, the last error and
    the last good read (UTC). Resets on restart."""
    return {
        "consecutive_failures": _state["failures"],
        "last_error": _state["last_error"],
        "last_success": _state["last_success"],
    }


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        logger.warning("sites.do: %s=%r is not an int; using %d", name, raw, default)
        return default


def daily_request_ceiling(*, paid: bool) -> int:
    """Requests per day before a site is throttled; 0 or less means no ceiling."""
    return _int_env(DAILY_REQUESTS_ENV[paid], DEFAULT_DAILY_REQUESTS[paid])


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ------------------------------------------------------------------ usage


@dataclass(frozen=True)
class ScriptUsage:
    requests: int = 0
    active_time: int = 0  # microseconds, as Cloudflare reports activeTime
    stored_bytes: int = 0


def _rows(data: Any) -> list[dict]:
    accounts = ((data or {}).get("viewer") or {}).get("accounts") or []
    rows = (accounts[0] or {}).get("rows") if accounts else None
    return [r for r in rows or [] if isinstance(r, dict)]


async def _per_namespace(cf: Any, query: str, day: date, namespaces: list[str], pick) -> dict:
    out: dict[str, int] = {}
    for i in range(0, len(namespaces), _NAMESPACE_CHUNK):
        variables = {
            "account": cf.account_id,
            "date": day.isoformat(),
            "namespaces": namespaces[i : i + _NAMESPACE_CHUNK],
        }
        for row in _rows(await cf.query_graphql(query, variables)):
            ns = (row.get("dimensions") or {}).get("namespaceId")
            if ns:
                out[ns] = out.get(ns, 0) + int(pick(row) or 0)
    return out


async def read_usage(cf: Any, scripts: Iterable[str], day: date) -> dict[str, ScriptUsage]:
    """Usage per script for ``day``. Raises when the namespaces list or the request
    counts cannot be read; duration and storage failures are logged and read as 0."""
    wanted = set(scripts)
    ns_script = {
        str(r["id"]): str(r["script"])
        for r in await cf.list_durable_object_namespaces()
        if isinstance(r, dict) and r.get("id") and r.get("script") in wanted
    }
    namespaces = sorted(ns_script)
    totals = {s: {"requests": 0, "active_time": 0, "stored_bytes": 0} for s in wanted}
    if namespaces:
        reads = (
            ("requests", REQUESTS_QUERY, lambda r: (r.get("sum") or {}).get("requests"), True),
            (
                "active_time",
                DURATION_QUERY,
                lambda r: (r.get("sum") or {}).get("activeTime"),
                False,
            ),
            (
                "stored_bytes",
                STORAGE_QUERY,
                lambda r: (r.get("max") or {}).get("storedBytes"),
                False,
            ),
        )
        for key, query, pick, required in reads:
            try:
                per_ns = await _per_namespace(cf, query, day, namespaces, pick)
            except Exception as exc:
                if required:
                    raise
                logger.warning("sites.do: %s usage unreadable, recorded as 0: %s", key, exc)
                continue
            for ns, value in per_ns.items():
                if ns in ns_script:  # only what we asked for
                    totals[ns_script[ns]][key] += value
    return {s: ScriptUsage(**v) for s, v in totals.items()}


async def sweep_do_usage(*, cf: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Meter today's DO usage per site and set or lift ``do_throttled``."""
    out = {"sites": 0, "throttled": 0, "released": 0}
    if not durable_objects.enabled():
        return out
    now = now or datetime.now(UTC)
    last = _state["last_usage_run"]
    interval = timedelta(minutes=max(_int_env(METERING_MINUTES_ENV, DEFAULT_METERING_MINUTES), 1))
    if last is not None and now - last < interval:
        return out
    _state["last_usage_run"] = now

    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.delete_cascade import _script_ref

    sites = await Site.find({"do_classes.0": {"$exists": True}}).to_list()
    scripts = {}
    for site in sites:
        if (getattr(site, "delete_status", "none") or "none") != "none":
            continue
        script, target = _script_ref(site)
        if script:
            scripts[script] = (site, target)
    if not scripts:
        return out
    day = now.date()
    try:
        cf = cf or sites_service._cf_client()
        usage = await read_usage(cf, scripts, day)
    except Exception as exc:  # noqa: BLE001 - fail open: never throttle on missing data
        _state["failures"] += 1
        _state["last_error"] = str(exc)[:300]
        logger.warning("sites.do: usage read failed, nothing changed: %s", exc)
        alert = max(_int_env(FAILURE_ALERT_ENV, DEFAULT_FAILURE_ALERT), 1)
        if _state["failures"] >= alert:
            logger.error(
                "sites.do: DO usage metering has failed %d times in a row (last: %s). "
                "Nothing is throttled on missing data. Likely cause: PAW_CF_API_TOKEN "
                "lacks the Account Analytics: Read scope, or a GraphQL field changed "
                "(run scenario D of scripts/sites_do_spike.py).",
                _state["failures"],
                _state["last_error"],
            )
            raise DoMeteringUnhealthy(_state["last_error"]) from exc
        return out
    _state["failures"], _state["last_error"], _state["last_success"] = 0, "", now

    key = day.isoformat()
    cutoff = (day - timedelta(days=USAGE_DAYS_KEPT)).isoformat()
    for script, (site, target) in scripts.items():
        used = usage.get(script, ScriptUsage())
        history = {d: v for d, v in (site.do_usage or {}).items() if d > cutoff}
        history[key] = {
            "requests": used.requests,
            "active_time": used.active_time,
            "stored_bytes": used.stored_bytes,
        }
        ceiling = daily_request_ceiling(paid=sites_service._site_paid(site))
        throttled = ceiling > 0 and used.requests > ceiling
        factor = max(_int_env(SUSPEND_FACTOR_ENV, DEFAULT_SUSPEND_FACTOR), 1)
        suspended = ceiling > 0 and used.requests > ceiling * factor
        try:
            # Every run, not only on a flip: a publish may have uploaded a stale value.
            await durable_objects.set_platform_vars_live(
                cf,
                script,
                target=target,
                values={
                    durable_objects.THROTTLED_VAR: "1" if throttled else "0",
                    durable_objects.SUSPENDED_VAR: "1" if suspended else "0",
                },
            )
        except Exception as exc:  # noqa: BLE001 - fail open, retried next sweep
            logger.warning(
                "sites.do: could not push the DO flags to %s (%s); will retry", script, exc
            )
            await site.set({"do_usage": history})
            out["sites"] += 1
            continue
        if suspended and not site.do_suspended:
            logger.error(
                "sites.do: site %s passed %d DO requests today (%dx its ceiling %d); suspended",
                site.id,
                used.requests,
                factor,
                ceiling,
            )
        if throttled and not site.do_throttled:
            out["throttled"] += 1
            logger.warning(
                "sites.do: site %s passed %d DO requests today (ceiling %d); throttled",
                site.id,
                used.requests,
                ceiling,
            )
        elif site.do_throttled and not throttled:
            out["released"] += 1
            logger.info("sites.do: site %s is under its DO ceiling again", site.id)
        await site.set({"do_usage": history, "do_throttled": throttled, "do_suspended": suspended})
        out["sites"] += 1
    return out


# --------------------------------------------------------------- teardowns


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(10 * 2 ** max(attempts - 2, 0), 24 * 60))


async def record_do_teardown(
    *,
    site_id: str,
    workspace: str,
    script: str,
    target: str,
    classes: Iterable[str],
    migration_tag: str | None,
    error: str,
) -> None:
    """Queue a retry for a DO teardown the delete cascade could not finish. Due at
    once; one row per (script, target)."""
    from pocketpaw_ee.cloud.models.site_do_teardown import SiteDoTeardown

    row = await SiteDoTeardown.find_one({"script": script, "target": target})
    now = datetime.now(UTC)
    if row is not None:
        await row.set({"last_error": error[:500], "state": "pending", "retry_after": now})
        return
    await SiteDoTeardown(
        site_id=site_id,
        workspace=workspace,
        script=script,
        target=target,
        classes=list(classes),
        migration_tag=migration_tag,
        last_error=error[:500],
        retry_after=now,
    ).insert()


async def sweep_do_teardowns(*, cf: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Retry due DO teardowns; past the attempt cap, hand them to an operator."""
    out = {"retried": 0, "done": 0, "operator": 0}
    if not durable_objects.enabled():
        return out
    from pocketpaw_ee.cloud.models.site_do_teardown import SiteDoTeardown

    now = now or datetime.now(UTC)
    rows = [
        r
        for r in await SiteDoTeardown.find({"state": "pending"}).to_list()
        if (_aware(r.retry_after) or now) <= now
    ]
    if not rows:
        return out
    if cf is None:
        try:
            from pocketpaw_ee.sites import service as sites_service

            cf = sites_service._cf_client()
        except Exception as exc:  # noqa: BLE001 - try again next tick
            logger.info("sites.do: teardown retries skipped (%s)", exc)
            return out
    cap = max(_int_env(TEARDOWN_MAX_ATTEMPTS_ENV, DEFAULT_TEARDOWN_MAX_ATTEMPTS), 1)
    for row in rows:
        result = await durable_objects.teardown_script(
            cf,
            row.script,
            target=row.target,
            classes=row.classes,
            migration_tag=row.migration_tag,
            tombstone=False,
        )
        if result.ok:
            await row.delete()
            out["done"] += 1
            logger.info("sites.do: Durable Objects of deleted site %s are gone", row.site_id)
            continue
        attempts = row.attempts + 1
        if attempts > cap:
            await row.set({"attempts": attempts, "last_error": result.error, "state": "operator"})
            out["operator"] += 1
            logger.error(
                "sites.do: Durable Object teardown of script %s (%s, classes %s, site %s) "
                "needs an operator after %d attempts: %s",
                row.script,
                row.target,
                ", ".join(row.classes),
                row.site_id,
                attempts,
                result.error,
            )
            continue
        await row.set(
            {
                "attempts": attempts,
                "last_error": result.error,
                "retry_after": now + _backoff(attempts),
            }
        )
        out["retried"] += 1
    return out


__all__ = [
    "DAILY_REQUESTS_ENV",
    "DoMeteringUnhealthy",
    "metering_status",
    "ScriptUsage",
    "daily_request_ceiling",
    "read_usage",
    "record_do_teardown",
    "sweep_do_teardowns",
    "sweep_do_usage",
]
