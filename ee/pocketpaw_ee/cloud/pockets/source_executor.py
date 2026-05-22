# source_executor.py — Server-side executor for pocket read-only data sources.
# Created: 2026-05-21 (RFC 04 alpha) — runs the GET "bindings" declared in a
#   pocket's `rippleSpec.sources` against the pocket's single configured
#   backend and returns the JSON results. Read-only (GET) only — write
#   bindings land in RFC 05 (write actions).
# Updated: 2026-05-21 (PR #1177 security pass) — basic auth now base64-encodes
#   the `user:pass` credential; the rate limiter is keyed per (pocket, user)
#   and guarded by an asyncio.Lock; imports the public `host_is_internal`;
#   `run_sources` writes an audit-log entry for every run.
# Updated: 2026-05-22 (RFC 05 M2a) — the SSRF/timeout/size guards extracted
#   to the shared `_http_guard.py` module: `_resolve_url`,
#   `_assert_host_external`, `_auth_headers`, `_strip_query`, the
#   `_HTTP_TIMEOUT` / `_MAX_RESPONSE_BYTES` constants, and the error class
#   (renamed `_SourceError` -> `_GuardError`). This executor now imports
#   them; `_SourceError` is kept as a `_GuardError` subclass for the read
#   executor's own per-source errors (timeouts, http_error, bad_json, …).
#   Behavior-identical — pure refactor; the read-executor tests are the
#   regression gate.
#
# SSRF BOUNDARY. The outbound-HTTP defenses now live in `_http_guard.py` —
# the ONE canonical guard module both executors import. Every defense from
# the locked security review still applies: strict base-URL re-validation,
# path-traversal rejection, same-host assertion after URL join, DNS
# rebinding check, no redirect following, tight timeouts, a 512 KB response
# cap, error-message sanitization, and a per-(pocket, user) rate limit.
#
# IMPORT-LINTER: must NOT import `pocketpaw_ee.cloud.models.*`. The executor
# receives base_url / auth / spec by parameter only — `pockets/service.py`
# owns all Beanie access.

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from pocketpaw.security.url_validators import validate_external_url_strict
from pocketpaw_ee.cloud.pockets._http_guard import (
    _HTTP_TIMEOUT,
    _MAX_RESPONSE_BYTES,
    _assert_host_external,
    _auth_headers,
    _GuardError,
    _resolve_url,
    _strip_query,
)

logger = logging.getLogger(__name__)

# --- limits / policy --------------------------------------------------------
_PER_SOURCE_TIMEOUT_S = 10.0
_RATE_LIMIT_MAX = 10  # runs per window per (pocket, user) (D16)
_RATE_LIMIT_WINDOW_S = 60.0

# Per-(pocket, user) run timestamps for the rate limiter. Keyed on both so a
# single member cannot exhaust another member's budget on a shared pocket.
# In-memory is fine for the alpha — a single process owns the run endpoint.
# M3 moves this to a shared store when refresh-cost controls land.
_run_log: dict[tuple[str, str], list[float]] = {}

# Guards the check-and-record on ``_run_log``. The read-filter-write is a
# TOCTOU race under ``asyncio.gather``; the lock makes it atomic.
_run_log_lock = asyncio.Lock()

# Default refresh policy for a source that omits ``refresh``.
_DEFAULT_REFRESH: list[Literal["pocket_open", "manual"]] = ["pocket_open"]


class SourceBinding(BaseModel):
    """One read-only data binding parsed from `rippleSpec.sources`.

    Unknown keys on a source entry are ignored — the spec may carry fields
    a later milestone reads. ``method`` is a Literal so only GET is ever
    accepted (write verbs are Milestone 2).
    """

    method: Literal["GET"] = "GET"
    path: str
    bind: str
    refresh: list[Literal["pocket_open", "manual"]] = Field(
        default_factory=lambda: _DEFAULT_REFRESH.copy()
    )


def _normalize_bind(bind: str) -> str:
    """Strip a leading ``state.`` from a bind path.

    ``state.prs`` and ``prs`` both target the ``prs`` key of pocket state.
    """
    return bind[len("state.") :] if bind.startswith("state.") else bind


async def _rate_limited(pocket_id: str, user_id: str) -> bool:
    """Return True when ``(pocket_id, user_id)`` has used its run budget.

    Records the call timestamp when it returns False (call permitted). The
    check-and-record runs under ``_run_log_lock`` so concurrent runs cannot
    race past the limit (TOCTOU under ``asyncio.gather``).
    """
    key = (pocket_id, user_id)
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_S
    async with _run_log_lock:
        stamps = [t for t in _run_log.get(key, []) if t >= window_start]
        if len(stamps) >= _RATE_LIMIT_MAX:
            _run_log[key] = stamps
            return True
        stamps.append(now)
        _run_log[key] = stamps
        return False


def _audit_source_run(
    *, actor: str, pocket_id: str, status: str, base_url: str, ran: int, errors: int
) -> None:
    """Write an audit-log entry for a source run.

    Mirrors ``pockets/service.py:_audit_backend_config`` — same audit path,
    category ``pocket_backend_config``, severity WARNING. The token is NEVER
    passed; ``base_url`` is query-stripped before it is logged. Audit
    failures must not break the run, so the call is wrapped.
    """
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.WARNING,
                actor=actor,
                action="pocket.sources.run",
                target=pocket_id,
                status=status,
                category="pocket_backend_config",
                pocket_id=pocket_id,
                base_url=_strip_query(base_url),
                ran=ran,
                errors=errors,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the run
        logger.warning("pocket source-run audit-log write failed", exc_info=True)


class _SourceError(_GuardError):
    """Internal: a per-source failure with an already-sanitized message.

    Subclasses the shared ``_GuardError`` so a single ``except _GuardError``
    catch covers both the guard primitives' rejections (extracted to
    ``_http_guard.py``) and the read executor's own per-source errors
    (timeout, http_error, bad_json, …). The guard messages say "path …";
    this read-executor subclass keeps the "source …" wording for its own
    rejections so the read-executor tests pass unchanged.
    """


def _select_sources(
    bindings: dict[str, SourceBinding],
    *,
    trigger: str | None,
    only_source: str | None,
) -> dict[str, SourceBinding]:
    """Pick which sources to run.

    ``only_source`` wins (single named source); else if ``trigger`` is set,
    every source whose ``refresh`` list contains it; else all sources.
    """
    if only_source is not None:
        if only_source in bindings:
            return {only_source: bindings[only_source]}
        return {}
    if trigger is not None:
        return {k: b for k, b in bindings.items() if trigger in b.refresh}
    return dict(bindings)


def _parse_bindings(ripple_spec: dict) -> tuple[dict[str, SourceBinding], list[dict]]:
    """Parse ``rippleSpec.sources`` into SourceBinding objects.

    Returns ``(valid_bindings, parse_errors)``. A malformed entry becomes a
    parse error rather than aborting the whole run.
    """
    raw = (ripple_spec or {}).get("sources") or {}
    bindings: dict[str, SourceBinding] = {}
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return bindings, errors
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            errors.append({"source": key, "error": "source entry must be an object"})
            continue
        try:
            bindings[key] = SourceBinding.model_validate(entry)
        except ValidationError:
            errors.append({"source": key, "error": "source entry is malformed"})
    return bindings, errors


async def _run_one(
    *,
    client: httpx.AsyncClient,
    key: str,
    binding: SourceBinding,
    base_url: str,
    headers: dict[str, str],
) -> dict:
    """Fetch a single source. Returns a ``ran`` row; raises ``_GuardError``
    (the shared guard rejections) or its ``_SourceError`` subclass (the
    read executor's own per-source failures)."""
    url = _resolve_url(base_url, binding.path)
    await _assert_host_external(urllib.parse.urlsplit(url).hostname or "")

    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        # D12 — never propagate raw exception text; log a query-stripped URL.
        logger.warning(
            "source %s: request to %s failed: %s",
            key,
            _strip_query(url),
            type(exc).__name__,
        )
        raise _SourceError("request to backend failed", code="request_failed") from exc

    # D9 — redirects are disabled on the client; treat any 3xx as an error.
    if 300 <= resp.status_code < 400:
        raise _SourceError("backend returned a redirect (not followed)", code="redirect")
    if resp.status_code >= 400:
        raise _SourceError(f"backend returned status {resp.status_code}", code="http_error")

    # D11 — reject oversized bodies; never write partial data.
    body = resp.content
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _SourceError("backend response exceeds the 512 KB limit", code="too_large")

    try:
        value = resp.json()
    except ValueError as exc:
        raise _SourceError("backend response is not valid JSON", code="bad_json") from exc

    return {"source": key, "bind": _normalize_bind(binding.bind), "value": value}


async def run_sources(
    *,
    pocket_id: str,
    user_id: str,
    ripple_spec: dict,
    base_url: str,
    auth_type: str,
    auth_header: str | None,
    token: str,
    trigger: str | None = None,
    only_source: str | None = None,
) -> dict:
    """Run the pocket's selected read-only sources and return the results.

    The result shape is::

        {"ran": [{"source", "bind", "value"}, ...],
         "errors": [{"source", "error"}, ...]}

    The executor is pure: it fetches and returns. It does NOT persist to the
    Pocket document and does NOT emit ``pocket_mutation`` — hydrated state is
    delivered in the HTTP response body of the calling route.

    ``user_id`` keys the rate limiter (per pocket *and* per user) and is the
    actor on the audit-log entry written for every run.
    """
    # D16 — per-(pocket, user) rate limit. On breach, return a source-level
    # error for every selected source without making any call.
    if await _rate_limited(pocket_id, user_id):
        bindings, parse_errors = _parse_bindings(ripple_spec)
        selected = _select_sources(bindings, trigger=trigger, only_source=only_source)
        _audit_source_run(
            actor=user_id,
            pocket_id=pocket_id,
            status="rate-limited",
            base_url=base_url,
            ran=0,
            errors=len(parse_errors) + len(selected),
        )
        return {
            "ran": [],
            "errors": parse_errors
            + [
                {"source": key, "error": "rate limit exceeded", "code": "rate_limited"}
                for key in selected
            ],
        }

    # D6/D15 — re-validate the base URL at call time even though config-time
    # validation already ran. Defense in depth against a tampered row.
    try:
        validate_external_url_strict(base_url)
    except ValueError:
        _audit_source_run(
            actor=user_id,
            pocket_id=pocket_id,
            status="rejected",
            base_url=base_url,
            ran=0,
            errors=0,
        )
        raise

    bindings, parse_errors = _parse_bindings(ripple_spec)
    selected = _select_sources(bindings, trigger=trigger, only_source=only_source)
    headers = _auth_headers(auth_type, auth_header, token)

    ran: list[dict] = []
    errors: list[dict] = list(parse_errors)

    if not selected:
        _audit_source_run(
            actor=user_id,
            pocket_id=pocket_id,
            status="success",
            base_url=base_url,
            ran=0,
            errors=len(errors),
        )
        return {"ran": ran, "errors": errors}

    # D9 — redirects disabled. D10 — tight timeouts.
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=_HTTP_TIMEOUT,
    ) as client:

        async def _guarded(key: str, binding: SourceBinding) -> dict:
            try:
                return await asyncio.wait_for(
                    _run_one(
                        client=client,
                        key=key,
                        binding=binding,
                        base_url=base_url,
                        headers=headers,
                    ),
                    timeout=_PER_SOURCE_TIMEOUT_S,
                )
            except TimeoutError:
                return {
                    "__error__": {
                        "source": key,
                        "error": "source timed out",
                        "code": "timeout",
                    }
                }
            except _GuardError as exc:
                # Covers both the shared-guard rejections and this
                # executor's own ``_SourceError`` subclass.
                return {"__error__": {"source": key, "error": exc.message, "code": exc.code}}
            except Exception:
                # Catch-all: never let a raw exception escape into the body.
                logger.warning("source %s: unexpected failure", key, exc_info=True)
                return {"__error__": {"source": key, "error": "source failed", "code": "error"}}

        results = await asyncio.gather(
            *(_guarded(key, binding) for key, binding in selected.items())
        )

    for result in results:
        if "__error__" in result:
            errors.append(result["__error__"])
        else:
            ran.append(result)

    _audit_source_run(
        actor=user_id,
        pocket_id=pocket_id,
        status="success",
        base_url=base_url,
        ran=len(ran),
        errors=len(errors),
    )
    return {"ran": ran, "errors": errors}


__all__ = ["run_sources", "SourceBinding"]
