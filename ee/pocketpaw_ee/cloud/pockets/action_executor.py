# action_executor.py — Server-side executor for pocket WRITE actions.
# Created: 2026-05-22 (RFC 05 M2a) — the write half of the pocket data
#   layer. RFC 04's `source_executor.py` runs GET read bindings; this
#   module runs POST/PUT/PATCH/DELETE write bindings declared in a pocket's
#   `rippleSpec.actions` block against the pocket's single configured
#   backend.
# Updated: 2026-05-22 (security review hardening) — (S1) the write
#   allowlist now globs the human `path_pattern` against the percent-
#   DECODED request path so encoding cannot defeat the match; (S2) a
#   backend >=400 status is no longer echoed to the client — the exact
#   number goes only to the audit log via `_BackendHTTPError`, the client
#   sees a generic message; (N1) an empty-params DELETE sends no JSON
#   body; (N3) `workspace_id` is now on every write-action audit entry so
#   the entries are tenant-filterable.
#
# A write has blast radius a read does not, so this executor adds three
# concerns on TOP of the shared SSRF guards:
#   1. The per-pocket WRITE ALLOWLIST (`allowed_writes`) — set by a human in
#      the backend config, OUTSIDE the spec. A method+path that does not
#      match an allowlist entry is rejected before any call leaves the
#      server. Authorship (the agent writes bindings) and authorization
#      (the human allow-lists the *class* of writes) are split.
#   2. INSTINCT-REJECT (fail-closed). M2b will route `requires_instinct`
#      actions through the Instinct approval surface. M2a must NOT silently
#      honor-then-ignore the flag: any action whose RAW dict carries a
#      truthy `requires_instinct` is rejected with `code: instinct_required`
#      and makes NO call.
#   3. An `Idempotency-Key` header (client-supplied or server `uuid4().hex`)
#      so a write retried after a network timeout cannot double-submit.
#
# Every SSRF/timeout/size/redirect guard from the read executor is INHERITED
# verbatim via the shared `_http_guard.py` module — strict base-URL
# re-validation, path-traversal rejection, same-host assertion, DNS
# rebinding check, no redirect following, tight timeouts, 512 KB response
# cap, error-message sanitization.
#
# A write-specific rate limit (`_action_log`, 20 writes / 60s /
# (pocket, user)) is a SEPARATE counter from the read executor's `_run_log`.
#
# IMPORT-LINTER: must NOT import `pocketpaw_ee.cloud.models.*`. The executor
# receives base_url / auth / the action binding / the allowlist by
# parameter only — `pockets/service.py` owns all Beanie access.

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
import urllib.parse
import uuid
from typing import Any, Literal

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
_PER_ACTION_TIMEOUT_S = 10.0
# Write budget per (pocket, user) per window. Separate from the read
# executor's _RATE_LIMIT_MAX (10) — a write is heavier, but the read budget
# must not be drained by writes nor vice versa, so the counters are split.
_ACTION_RATE_LIMIT_MAX = 20
_ACTION_RATE_LIMIT_WINDOW_S = 60.0

# Per-(pocket, user) write timestamps. SEPARATE dict from
# source_executor._run_log so reads and writes never share a budget.
_action_log: dict[tuple[str, str], list[float]] = {}

# Guards the check-and-record on ``_action_log``. The read-filter-write is a
# TOCTOU race under ``asyncio.gather``; the lock makes it atomic — the same
# pattern source_executor uses for its read counter.
_action_log_lock = asyncio.Lock()


class _BackendHTTPError(Exception):
    """Raised by ``_do_request`` when the backend returns a >=400 status.

    Carries the exact numeric ``status_code`` so the caller can record it
    in the audit log — but the caller never echoes it to the client. A
    separate type (not ``_GuardError``) keeps the status-bearing failure
    from leaking the number through a shared ``message`` field.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"backend returned status {status_code}")
        self.status_code = status_code


class ActionBinding(BaseModel):
    """One write binding parsed from `rippleSpec.actions`.

    ``model_config`` ignores unknown keys — the spec entry carries M2b
    governance/metering fields (`requires_instinct`, `instinct_policy`,
    `outcome`) and possibly RFC-03 template fields that this M2a executor
    does not act on. Ignoring them keeps an M2b-authored spec parseable by
    an M2a runtime instead of crashing on an unknown field.

    NOTE: `requires_instinct` is deliberately NOT a declared field — the
    instinct-reject check reads it off the RAW action dict (see
    `_instinct_rejected`). Declaring it here would let `extra:ignore` drop
    it and the fail-closed gate would silently pass.
    """

    model_config = {"extra": "ignore"}

    kind: Literal["write_binding"] = "write_binding"
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str
    params: dict = Field(default_factory=dict)
    confirm: bool = False
    on_success: list[dict] = Field(default_factory=list)
    on_error: list[dict] = Field(default_factory=list)


def _instinct_rejected(raw_action: dict[str, Any]) -> bool:
    """Return ``True`` when the RAW action dict requests Instinct gating.

    Fail-closed: M2a has no Instinct wiring, so any truthy
    ``requires_instinct`` means the action must NOT fire. Reads the raw
    dict — never the parsed ``ActionBinding`` — because ``extra: ignore``
    drops the field off the model.
    """
    return bool(raw_action.get("requires_instinct"))


def _allowlist_match(method: str, path_no_query: str, allowed_writes: list[dict[str, Any]]) -> bool:
    """Return ``True`` when ``(method, path)`` matches an allowlist entry.

    ``method`` is matched exactly (case-sensitive — both sides are upper
    verbs). ``path_no_query`` is matched against each entry's
    ``path_pattern`` via ``fnmatch.fnmatchcase`` — a glob, so ``*`` spans
    any run of characters including ``/``. The query string is stripped by
    the caller before this check so a pattern like ``/leases/*`` is not
    defeated by ``?x=y``.

    The caller passes the percent-DECODED path — the human-authored
    ``path_pattern`` is globbed against the decoded request path so the
    match is consistent regardless of client encoding (a ``%2e%2e`` cannot
    slip past as something the pattern does not recognise). The
    ``path_pattern`` itself is matched as-is and is NOT decoded.

    An empty ``allowed_writes`` matches nothing — fail-closed: a pocket
    with no write policy can fire no writes.
    """
    for entry in allowed_writes:
        if not isinstance(entry, dict):
            continue
        if entry.get("method") != method:
            continue
        pattern = entry.get("path_pattern")
        if not isinstance(pattern, str):
            continue
        if fnmatch.fnmatchcase(path_no_query, pattern):
            return True
    return False


async def _action_rate_limited(pocket_id: str, user_id: str) -> bool:
    """Return True when ``(pocket_id, user_id)`` has used its write budget.

    Records the call timestamp when it returns False (call permitted). The
    check-and-record runs under ``_action_log_lock`` so concurrent writes
    cannot race past the limit (TOCTOU under ``asyncio.gather``). Mirrors
    ``source_executor._rate_limited`` but against the separate write
    counter.
    """
    key = (pocket_id, user_id)
    now = time.monotonic()
    window_start = now - _ACTION_RATE_LIMIT_WINDOW_S
    async with _action_log_lock:
        stamps = [t for t in _action_log.get(key, []) if t >= window_start]
        if len(stamps) >= _ACTION_RATE_LIMIT_MAX:
            _action_log[key] = stamps
            return True
        stamps.append(now)
        _action_log[key] = stamps
        return False


def _audit_action_run(
    *,
    actor: str,
    workspace_id: str,
    pocket_id: str,
    action: str,
    status: str,
    base_url: str,
    backend_status: int | None = None,
) -> None:
    """Write an audit-log entry for a write-action run.

    Mirrors ``source_executor._audit_source_run`` — category
    ``pocket_backend_config``, severity WARNING. The token is NEVER passed;
    ``base_url`` is query-stripped before it is logged. ``workspace_id`` is
    logged so write-action entries are tenant-filterable, the same way the
    backend-config audit entries already are. A rejected write (allowlist
    miss, instinct gate, bad path) is audited with the matching ``status``
    so the rejection is visible. ``backend_status`` carries the exact
    numeric HTTP status from the backend on an ``http_error`` — it goes
    ONLY into the audit log, never the client response, so the endpoint is
    not a path-probing oracle. Audit failures must not break the run, so
    the call is wrapped.
    """
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        fields: dict[str, Any] = {
            "pocket_id": pocket_id,
            "pocket_action": action,
            "base_url": _strip_query(base_url),
        }
        if backend_status is not None:
            fields["backend_status"] = backend_status

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.WARNING,
                actor=actor,
                action="pocket.actions.run",
                target=pocket_id,
                status=status,
                category="pocket_backend_config",
                workspace_id=workspace_id,
                **fields,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the run
        logger.warning("pocket action-run audit-log write failed", exc_info=True)


def _error(action: str, message: str, code: str, on_error: list[dict]) -> dict:
    """Build the standard failure response for a write action."""
    return {
        "ok": False,
        "action": action,
        "error": message,
        "code": code,
        "on_error": on_error,
    }


async def run_action(
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    action: str,
    raw_action: dict[str, Any],
    path: str,
    params: dict[str, Any],
    base_url: str,
    auth_type: str,
    auth_header: str | None,
    token: str,
    allowed_writes: list[dict[str, Any]],
    idempotency_key: str | None = None,
) -> dict:
    """Run ONE pocket write action against its configured backend.

    ``raw_action`` is the action's entry from the persisted
    ``rippleSpec.actions`` block — the server reads ``method`` /
    ``confirm`` / ``on_success`` / ``on_error`` from it (a compromised
    client cannot pick the verb). ``path`` and ``params`` arrive from the
    client already resolved by Ripple's ``{...}`` expression resolver.

    The result shape on success::

        {"ok": true, "action", "status", "response",
         "on_success": [...], "on_error": [...]}

    On failure::

        {"ok": false, "action", "error", "code", "on_error": [...]}

    The executor is pure: it makes the one HTTP call and returns. It does
    NOT persist to the Pocket document and does NOT emit ``pocket_mutation``
    — the response is delivered in the calling route's HTTP body.

    Gate order (each gate makes NO call when it rejects):
      1. Parse the binding (``ActionBinding``); a malformed entry is a
         ``bad_binding`` rejection.
      2. INSTINCT-REJECT — fail-closed on a truthy raw ``requires_instinct``.
      3. Write rate limit — 20 writes / 60s / (pocket, user).
      4. Strict base-URL re-validation (defense in depth).
      5. ``_resolve_url`` — path-traversal / absolute-URL / cross-host
         rejection (shared SSRF guard).
      6. ALLOWLIST — ``(method, query-stripped, percent-decoded path)``
         must match an ``allowed_writes`` entry; a miss is audited WARNING
         ``rejected``.
      7. DNS pre-resolve — reject a host that resolves internal.
      8. The HTTP call: redirects disabled, 3xx is an error, tight
         timeouts, 512 KB response cap, sanitized errors.
    """
    # ── 1. parse the binding ────────────────────────────────────────────
    try:
        binding = ActionBinding.model_validate(raw_action)
    except ValidationError as exc:
        msg = (
            exc.errors()[0].get("msg", "malformed action binding")
            if exc.errors()
            else ("malformed action binding")
        )
        return _error(action, f"action binding is malformed: {msg}", "bad_binding", [])

    on_error = binding.on_error
    method = binding.method

    # ── 2. instinct-reject (fail-closed) ────────────────────────────────
    # M2a has no Instinct wiring. Honoring-then-ignoring the flag would
    # silently fire a write the spec said needs approval — reject instead.
    if _instinct_rejected(raw_action):
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="instinct-required",
            base_url=base_url,
        )
        return _error(
            action,
            "action requires approval — Instinct gating is not in this build",
            "instinct_required",
            on_error,
        )

    # ── 3. write rate limit ─────────────────────────────────────────────
    if await _action_rate_limited(pocket_id, user_id):
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="rate-limited",
            base_url=base_url,
        )
        return _error(action, "write rate limit exceeded", "rate_limited", on_error)

    # ── 4. strict base-URL re-validation ────────────────────────────────
    # D6/D15 — re-validate even though config-time validation already ran.
    try:
        validate_external_url_strict(base_url)
    except ValueError as exc:
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="rejected",
            base_url=base_url,
        )
        return _error(action, str(exc), "bad_base_url", on_error)

    # ── 5. resolve + SSRF-guard the path ────────────────────────────────
    try:
        url = _resolve_url(base_url, path)
    except _GuardError as exc:
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="rejected",
            base_url=base_url,
        )
        return _error(action, exc.message, exc.code, on_error)

    # ── 6. allowlist check ──────────────────────────────────────────────
    # Match (method, path-with-query-stripped) against the human-set
    # allowlist. The query is stripped so `/leases/*` is not bypassed by a
    # trailing `?x=y`. A miss makes NO call and is audited as `rejected`.
    path_no_query = _strip_query(url)
    # _strip_query keeps the scheme+host; match the entry against the path
    # portion only, the same shape the human authors in `path_pattern`.
    path_only = urllib.parse.urlsplit(path_no_query).path or "/"
    # Decode percent-encoding ONCE before the match — `_allowlist_match`
    # globs the human-authored `path_pattern` against the DECODED path, so
    # an entry like `/leases/*/renew` matches consistently regardless of
    # how the client encoded the path. A `%2e%2e` cannot slip past the
    # allowlist as something the human pattern does not recognise. The
    # `path_pattern` itself is human-authored and matched as-is — only the
    # request path is decoded.
    path_decoded = urllib.parse.unquote(path_only)
    if not _allowlist_match(method, path_decoded, allowed_writes):
        logger.warning(
            "pocket %s action %s: %s %s not in write allowlist",
            pocket_id,
            action,
            method,
            path_decoded,
        )
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="rejected",
            base_url=base_url,
        )
        return _error(
            action,
            f"{method} {path_decoded} is not in this pocket's write allowlist",
            "not_allowed",
            on_error,
        )

    # ── 7. DNS pre-resolve ──────────────────────────────────────────────
    try:
        await _assert_host_external(urllib.parse.urlsplit(url).hostname or "")
    except _GuardError as exc:
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="rejected",
            base_url=base_url,
        )
        return _error(action, exc.message, exc.code, on_error)

    # ── 8. the HTTP call ────────────────────────────────────────────────
    headers = _auth_headers(auth_type, auth_header, token)
    # Idempotency-Key — client-supplied wins, else a server-generated hex.
    # A write retried after a network timeout carries the SAME key so a
    # well-behaved backend can dedupe it.
    headers["Idempotency-Key"] = idempotency_key or uuid.uuid4().hex

    try:
        result = await asyncio.wait_for(
            _do_request(
                method=method,
                url=url,
                headers=headers,
                params=params,
            ),
            timeout=_PER_ACTION_TIMEOUT_S,
        )
    except TimeoutError:
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="error",
            base_url=base_url,
        )
        return _error(action, "action timed out", "timeout", on_error)
    except _BackendHTTPError as exc:
        # S2 — the exact backend HTTP status goes ONLY to the audit log,
        # never the client. Echoing `resp.status_code` to the caller turns
        # this endpoint into a path-probing oracle on the configured
        # backend. The client sees a generic `http_error` category.
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="error",
            base_url=base_url,
            backend_status=exc.status_code,
        )
        return _error(action, "the backend rejected the request", "http_error", on_error)
    except _GuardError as exc:
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="error",
            base_url=base_url,
        )
        return _error(action, exc.message, exc.code, on_error)
    except Exception:  # noqa: BLE001 — never let a raw exception escape
        logger.warning("pocket %s action %s: unexpected failure", pocket_id, action, exc_info=True)
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="error",
            base_url=base_url,
        )
        return _error(action, "action failed", "error", on_error)

    _audit_action_run(
        actor=user_id,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        action=action,
        status="success",
        base_url=base_url,
    )
    return {
        "ok": True,
        "action": action,
        "status": result["status"],
        "response": result["response"],
        "on_success": binding.on_success,
        "on_error": on_error,
    }


async def _do_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> dict:
    """Make the one write request. Returns ``{status, response}``; raises
    ``_GuardError`` on a transport failure, a 3xx redirect, or an oversized
    body, and ``_BackendHTTPError`` on a >=400 status.

    ``params`` is sent as the JSON request body for POST/PUT/PATCH. For a
    DELETE the body is sent ONLY when ``params`` is non-empty — a DELETE
    with no params sends no JSON body at all, because some backends and
    WAFs reject a DELETE that carries a body. Redirects are disabled on
    the client; a 3xx is an error, exactly as the read executor treats one.
    """
    # N1 — omit the JSON body on an empty-params DELETE; some backends/WAFs
    # reject a DELETE with a body. Any verb with non-empty params still
    # sends the body.
    send_body = bool(params) or method != "DELETE"

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=_HTTP_TIMEOUT,
    ) as client:
        try:
            if send_body:
                resp = await client.request(method, url, headers=headers, json=params)
            else:
                resp = await client.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            # D12 — never propagate raw exception text; log a stripped URL.
            logger.warning(
                "action request to %s failed: %s",
                _strip_query(url),
                type(exc).__name__,
            )
            raise _GuardError("request to backend failed", code="request_failed") from exc

    # D9 — redirects are disabled on the client; treat any 3xx as an error.
    if 300 <= resp.status_code < 400:
        raise _GuardError("backend returned a redirect (not followed)", code="redirect")
    # S2 — a >=400 status raises a status-bearing error; the caller logs
    # the exact number to the audit log and returns a generic message to
    # the client so the endpoint is not a backend path-probing oracle.
    if resp.status_code >= 400:
        raise _BackendHTTPError(resp.status_code)

    # D11 — reject oversized bodies; never surface partial data.
    body = resp.content
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _GuardError("backend response exceeds the 512 KB limit", code="too_large")

    # A successful write often returns the mutated record; sometimes an
    # empty 204. Parse JSON when present, else fall back to None — a
    # non-JSON 2xx is still a success.
    response: Any = None
    if body:
        try:
            response = resp.json()
        except ValueError:
            response = None
    return {"status": resp.status_code, "response": response}


__all__ = ["ActionBinding", "run_action"]
