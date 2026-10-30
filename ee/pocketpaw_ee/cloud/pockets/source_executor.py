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
# Updated: 2026-05-22 (RFC 04 M3) — `SourceBinding.refresh` now also
#   accepts `"interval"` (re-run on a timer) and `"webhook"` (re-run on an
#   inbound authenticated POST). A binding may carry
#   `refresh_interval_seconds` — the desired interval; the interval
#   scheduler floors it by `POCKETPAW_SOURCE_REFRESH_MIN_INTERVAL_SECONDS`
#   so a hallucinated `1` cannot spin the loop. Both new triggers are
#   AUTO-refresh: they re-run a source with no human in the loop, so they
#   are metered by the per-pocket budget in `_refresh_budget.py` —
#   SEPARATE from the manual per-(pocket, user) `_run_log` limiter here.
# Updated: 2026-06-08 (fix/pocket-sources-run-400) — added the public
#   `selected_source_keys()` helper (reuses `_parse_bindings` +
#   `_select_sources`) so a caller can ask "would this (trigger, only_source)
#   request run anything?" without a backend call. The `sources/run` route
#   uses it to turn the implicit on-open run of a blank/starter pocket (no
#   backend bound, nothing to fetch) into a clean no-op instead of a hard 400.
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
#
# Updated: 2026-06-08 (feat/sense-source, Sense tier chunk 6b) — a source
#   binding can now be `type: "sense"`: it resolves a provider-agnostic Sense
#   via `senses.resolver.execute_sense` instead of an HTTP GET, unwraps the
#   ExecuteActionResponse to its `.data` payload, and binds that into state
#   with the SAME `{source, bind, value}` row shape the http path returns. A
#   resolver refusal (ok=False) maps to a per-source `_SourceError` so the
#   error-aggregation loop treats it exactly like an http failure — siblings
#   keep running. `SourceBinding` gained `type` (default "http"), optional
#   `sense_id`/`action`/`params` (params are STATIC in v1 — no `{state.x}`
#   evaluation). `path` is now optional (required only for http, enforced at
#   run). `run_sources` gained an optional `workspace_id`, threaded with the
#   existing `pocket_id`/`user_id` into `_run_one` for sense resolution.
# Updated: 2026-06-08 (sense-tier robustness fix) — `run_sources`'s
#   `workspace_id` is now REQUIRED (`str`, no default). All four callers
#   already pass a real value; omitting it should be a loud TypeError, not a
#   None that flows into a sense resolve and silently returns "no provider".
#   DEFENSE: `_run_sense_binding` now rejects a falsy `workspace_id` up front
#   with a `_SourceError(code="bad_source")` so a missing workspace context
#   lands in the errors aggregation as a CLEAR error (mirrors the http-no-path
#   guard), never a silent no-provider.
# Updated: 2026-06-12 (feat/connector-as-pocket-backend) — two additions.
#   (1) CONNECTOR BACKEND: `run_sources` gained `backend_type` ("http" default
#   | "connector") + `connector_name`. When the pocket's backend is a connector,
#   each non-sense source runs against the BOUND connector via
#   `_run_connector_binding` → `connectors_service.execute` (the SAME read-first
#   path senses use: only `auto`-trust actions run; confirm/restricted is
#   refused per-source). The binding's `action`/`params` name the connector
#   action. `base_url` is unused and the SSRF re-validation is skipped for a
#   connector backend. http/sense paths are byte-for-byte unchanged when
#   `backend_type="http"`. (2) SOURCE TRANSFORM: `SourceBinding.transform` (a
#   small declarative `{select, map}` spec, interpreted by the pure
#   `_source_transform.apply_transform`) shapes the RAW result of ANY source
#   type before it binds — applied in the shared `_shape_result` row builder. A
#   malformed transform fails THAT source cleanly (`bad_transform`), never a
#   sibling. `SourceBinding.type` gained the `"connector"` literal.
#
# IMPORT-LINTER: still must NOT import `pocketpaw_ee.cloud.models.*`. The new
# `_source_transform` helper is pure (no imports beyond typing); the connectors
# service is imported LAZILY inside `_run_connector_binding` (same pattern as
# the sense resolver import) so the static graph stays clean and cycle-free.
# Updated: 2026-06-19 (feat/typed-ripplespec-phase2) — DUAL-PATH READER. The
#   spec-reading entry points (`_parse_bindings`, `selected_source_keys`,
#   `run_sources`) now accept `RippleSpec | dict | None`. A new `_sources_block`
#   helper reads `spec.sources` from a typed RippleSpec or `.get("sources")`
#   from a legacy flat dict — so a stored legacy dict and a promoted typed spec
#   both run identically (no migration, promote-on-read). The OSS-only
#   `pocketpaw.bundled_templates.schema` import keeps the import-linter contract
#   clean (no `models.*` dependency).

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from pocketpaw.bundled_templates.schema import RippleSpec
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
from pocketpaw_ee.cloud.pockets._source_transform import TransformError, apply_transform

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

# A refresh trigger names WHEN a source re-runs:
#   pocket_open — re-run when the user opens the pocket
#   manual      — re-run from a refresh button (run_source action)
#   interval    — re-run on a timer (RFC 04 M3 — interval scheduler)
#   webhook     — re-run on an authenticated inbound POST (RFC 04 M3)
RefreshTrigger = Literal["pocket_open", "manual", "interval", "webhook"]

# Default refresh policy for a source that omits ``refresh``.
_DEFAULT_REFRESH: list[RefreshTrigger] = ["pocket_open"]


class SourceBinding(BaseModel):
    """One read-only data binding parsed from `rippleSpec.sources`.

    Unknown keys on a source entry are ignored — the spec may carry fields
    a later milestone reads. ``method`` is a Literal so only GET is ever
    accepted (write verbs are Milestone 2).

    ``refresh_interval_seconds`` is the source author's desired interval
    when ``"interval"`` is in ``refresh``. It is a REQUEST, not a
    guarantee: the interval scheduler floors it by the configured
    minimum (``POCKETPAW_SOURCE_REFRESH_MIN_INTERVAL_SECONDS``), so a
    hallucinated ``refresh_interval_seconds: 1`` is clamped, never
    honored. ``None`` means "use the floor as the interval".
    """

    type: Literal["http", "sense", "connector"] = "http"
    method: Literal["GET"] = "GET"
    # ``path`` is required for http sources, unused for sense/connector sources.
    # Kept optional here so a sense/connector entry survives parse; the http
    # path raises a clean per-source error if a misconfigured http source has
    # no path.
    path: str | None = None
    bind: str
    refresh: list[RefreshTrigger] = Field(default_factory=lambda: _DEFAULT_REFRESH.copy())
    refresh_interval_seconds: int | None = Field(default=None, ge=1)
    # Sense/connector-source fields. ``params`` are STATIC in v1 — passed
    # through to the Sense resolver / connector as-is (no ``{state.x}``
    # evaluation). For ``type == "sense"`` the resolver picks the connector via
    # ``sense_id``; for ``type == "connector"`` the pocket's bound connector is
    # used and only ``action``/``params`` matter (the binding does not name the
    # connector — the backend does).
    sense_id: str | None = None
    action: str | None = None
    params: dict | None = None
    # Optional declarative transform applied to the RAW fetch result before it
    # is bound to state — for ALL source types. v1 grammar: ``{"select": ...}``
    # (drill a dotted path) and/or ``{"map": [...]}`` (reshape a list row by
    # row). Interpreted by the pure ``_source_transform.apply_transform``; a
    # spec outside the grammar fails the source cleanly (never a sibling).
    transform: dict | None = None


def _normalize_bind(bind: str) -> str:
    """Strip a leading ``state.`` from a bind path.

    ``state.prs`` and ``prs`` both target the ``prs`` key of pocket state.
    """
    return bind[len("state.") :] if bind.startswith("state.") else bind


def _shape_result(*, key: str, binding: SourceBinding, raw: object) -> dict:
    """Apply the binding's transform (if any) to ``raw`` and build the row.

    The ONE place every source type (http / sense / connector) funnels its
    raw result through before binding to state. With no ``transform`` it
    returns the raw value unchanged — today's behavior for http/sense.
    A ``transform`` outside the v1 grammar raises ``_SourceError`` (mapped from
    the pure transformer's ``TransformError``) so a hallucinated/ malformed
    transform fails THIS source cleanly without touching a sibling.
    """
    try:
        value = apply_transform(raw, binding.transform)
    except TransformError as exc:
        raise _SourceError(f"source transform is invalid: {exc}", code="bad_transform") from exc
    return {"source": key, "bind": _normalize_bind(binding.bind), "value": value}


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
                category="pocket_source_run",
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


def _sources_block(ripple_spec: RippleSpec | dict | None) -> dict[str, Any]:
    """Return the ``sources`` block from a ``RippleSpec | dict | None`` reader input.

    Phase-2 dual-path: when handed a typed ``RippleSpec`` read its ``sources``
    field; when handed a legacy flat dict use the existing ``.get("sources")``
    path. ``None`` / a non-dict ``sources`` value yields an empty dict, exactly
    as the prior ``(ripple_spec or {}).get("sources") or {}`` expression did.
    """
    if isinstance(ripple_spec, RippleSpec):
        raw = ripple_spec.sources
    elif isinstance(ripple_spec, dict):
        raw = ripple_spec.get("sources")
    else:
        raw = None
    return raw if isinstance(raw, dict) else {}


def selected_source_keys(
    ripple_spec: RippleSpec | dict | None,
    *,
    trigger: str | None = None,
    only_source: str | None = None,
) -> list[str]:
    """Return the source keys a ``(trigger, only_source)`` request would run.

    Reuses the same ``_parse_bindings`` + ``_select_sources`` logic the run
    path uses, so callers can answer "is there anything to run?" WITHOUT
    making a backend call or duplicating the selection rules. Malformed
    entries (which ``_parse_bindings`` turns into parse errors, not bindings)
    are correctly excluded — they are never runnable.

    Accepts a typed ``RippleSpec`` or a legacy flat dict (Phase-2 dual-path).

    Used by the ``sources/run`` route to decide, when no backend is
    configured, whether the request is a benign no-op (nothing selected →
    empty result) or a real misconfiguration (a runnable source was authored
    but the pocket has no backend bound).
    """
    bindings, _parse_errors = _parse_bindings(ripple_spec)
    selected = _select_sources(bindings, trigger=trigger, only_source=only_source)
    return list(selected.keys())


def _parse_bindings(
    ripple_spec: RippleSpec | dict | None,
) -> tuple[dict[str, SourceBinding], list[dict]]:
    """Parse ``rippleSpec.sources`` into SourceBinding objects.

    Returns ``(valid_bindings, parse_errors)``. A malformed entry becomes a
    parse error rather than aborting the whole run.

    Phase-2 dual-path: accepts a typed ``RippleSpec`` (reads ``spec.sources``)
    or a legacy flat dict (the existing ``.get("sources")`` path). Either way a
    malformed / absent ``sources`` block yields no bindings.
    """
    raw = _sources_block(ripple_spec)
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


async def _run_sense_binding(
    *,
    key: str,
    binding: SourceBinding,
    workspace_id: str | None,
    pocket_id: str,
    user_id: str,
) -> dict:
    """Resolve a ``type="sense"`` source via the Sense resolver.

    Returns the SAME success row shape the http path returns —
    ``{"source", "bind", "value"}`` — with ``value`` UNWRAPPED to the
    underlying ``ExecuteActionResponse.data`` payload (state gets the actual
    data, not the envelope). On a resolver refusal (``ok=False`` — no
    provider, or read-first approval gate) it raises ``_SourceError`` with
    the resolver's stable code/message, so the aggregation loop treats it as
    a per-source error exactly like an http failure (does NOT abort siblings).

    ``params`` are STATIC in v1 — passed through as-is (no ``{state.x}``).
    """
    # DEFENSE: a sense source resolves a provider FOR A WORKSPACE. A falsy
    # workspace context would flow into the resolver and silently come back as
    # "no provider"; surface it as a CLEAR per-source error instead (mirrors
    # the http-no-path guard). ``run_sources`` now requires workspace_id, so
    # this is a belt-and-braces guard against a future internal caller.
    if not workspace_id:
        raise _SourceError("sense source requires a workspace context", code="bad_source")

    # Lazy import: avoids a top-level cycle and keeps the SSRF/http core of
    # this module importable without the senses subsystem.
    from pocketpaw_ee.cloud.senses.resolver import execute_sense

    result = await execute_sense(
        binding.sense_id,
        binding.action,
        binding.params or {},
        workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
    )
    if not result.ok:
        # Map the resolver refusal onto the same per-source error shape http
        # failures use, so ``run_sources``'s error aggregation is unchanged.
        raise _SourceError(
            result.message or "sense source failed",
            code=result.error or "sense_error",
        )

    # result.data is the ExecuteActionResponse; result.data.data is the
    # payload that should land in pocket state. The binding's transform (if
    # any) shapes that payload before it binds.
    payload = getattr(result.data, "data", None)
    return _shape_result(key=key, binding=binding, raw=payload)


async def _run_connector_binding(
    *,
    key: str,
    binding: SourceBinding,
    connector_name: str | None,
    workspace_id: str | None,
    pocket_id: str,
    user_id: str,
) -> dict:
    """Run a source against the pocket's BOUND connector (backend_type="connector").

    The pocket's backend names the connector; the binding's ``action`` /
    ``params`` name which connector action to call. Routes through the SAME
    ``connectors_service.execute`` path senses use — read-first (v1): the
    action's ``trust_level`` must be exactly ``"auto"``; a confirm / restricted
    / unknown action is REFUSED here with a clean per-source error and
    ``execute`` is never called (so a sibling source keeps running).

    Returns the SAME ``{source, bind, value}`` row shape the http/sense paths
    return, with the connector's ``ExecuteActionResponse.data`` payload shaped
    by the binding's transform before it binds. ``params`` are STATIC in v1
    (no ``{state.x}`` evaluation).
    """
    if not workspace_id:
        # Mirror the sense-path guard: a connector executes FOR a workspace; a
        # falsy workspace context would mis-scope the execute. Loud per-source
        # error instead of a silent mis-resolution.
        raise _SourceError("connector source requires a workspace context", code="bad_source")
    if not connector_name:
        # The pocket's backend is supposed to name the connector. A connector
        # backend with no name is a misconfiguration — fail this source clearly.
        raise _SourceError("connector backend has no connector_name", code="bad_source")
    if not binding.action:
        raise _SourceError("connector source has no action", code="bad_source")

    # Lazy import — keeps the SSRF/http core importable without the connectors
    # subsystem and avoids a top-level import cycle (the connectors service
    # imports pockets.service, which the source executor lives alongside).
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest

    # READ-FIRST GATE — identical policy to the sense resolver: only an
    # ``auto``-trust action runs. Anything else is refused BEFORE execute.
    trust_info = await connectors_service.get_action_trust(connector_name, binding.action)
    if trust_info is None or not getattr(trust_info, "is_read", False):
        trust = getattr(trust_info, "trust_level", None) if trust_info else None
        raise _SourceError(
            f"action {binding.action!r} on {connector_name!r} is not auto-trust "
            f"(trust_level={trust!r}) — refused in v1 (read-first)",
            code="action_needs_approval",
        )

    try:
        response = await connectors_service.execute(
            workspace_id,
            connector_name,
            ExecuteActionRequest(
                action=binding.action,
                params=binding.params or {},
                pocket_id=pocket_id,
            ),
            user_id=user_id,
        )
    except Exception as exc:
        # connectors_service raises CloudError/NotFound for an unknown
        # connector/action, a local-runtime-unavailable action, etc. Contain
        # it as a per-source error — never let it abort a sibling source.
        logger.warning(
            "connector source %s: execute on %s failed: %s",
            key,
            connector_name,
            type(exc).__name__,
        )
        raise _SourceError("connector action execution failed", code="connector_error") from exc

    # A connector action can fail at the adapter without raising — surface that
    # as a per-source error too, mirroring the sense resolver's ok=False path.
    if not getattr(response, "success", False):
        raise _SourceError(
            getattr(response, "error", None) or "connector action returned an error",
            code="connector_error",
        )

    payload = getattr(response, "data", None)
    return _shape_result(key=key, binding=binding, raw=payload)


async def _run_one(
    *,
    client: httpx.AsyncClient,
    key: str,
    binding: SourceBinding,
    base_url: str,
    headers: dict[str, str],
    workspace_id: str | None,
    pocket_id: str,
    user_id: str,
    backend_type: str = "http",
    connector_name: str | None = None,
) -> dict:
    """Fetch a single source. Returns a ``ran`` row; raises ``_GuardError``
    (the shared guard rejections) or its ``_SourceError`` subclass (the
    read executor's own per-source failures).

    Dispatch order:
      1. A ``type="sense"`` binding ALWAYS resolves via the Sense resolver
         (``_run_sense_binding``) — sense sources are backend-agnostic, so the
         pocket's backend type never changes how they run.
      2. Otherwise, when the pocket's backend is ``backend_type="connector"``,
         the source runs against the BOUND connector via
         ``_run_connector_binding`` (action surface, not an HTTP GET).
      3. Otherwise (the default ``backend_type="http"``) it runs the
         SSRF-guarded GET below, unchanged.

    The transform (if any) is applied inside whichever runner builds the row,
    so it composes with all three transports."""
    if binding.type == "sense":
        return await _run_sense_binding(
            key=key,
            binding=binding,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
        )

    if binding.type == "connector" or backend_type == "connector":
        return await _run_connector_binding(
            key=key,
            binding=binding,
            connector_name=connector_name,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
        )

    if not binding.path:
        # An http source with no path is a misconfiguration — surface it as a
        # clean per-source error rather than crashing _resolve_url.
        raise _SourceError("http source has no path", code="bad_source")
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

    # The binding's transform (if any) shapes the parsed JSON before it binds;
    # with no transform this is the raw value — today's behavior, unchanged.
    return _shape_result(key=key, binding=binding, raw=value)


async def run_sources(
    *,
    pocket_id: str,
    user_id: str,
    ripple_spec: RippleSpec | dict | None,
    base_url: str,
    auth_type: str,
    auth_header: str | None,
    token: str,
    trigger: str | None = None,
    only_source: str | None = None,
    workspace_id: str,
    backend_type: str = "http",
    connector_name: str | None = None,
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

    ``workspace_id`` is REQUIRED — a sense source resolves a provider for a
    workspace, and a None would silently mis-resolve to "no provider". All
    callers already pass a real value, so a caller that omits it raises a loud
    TypeError rather than running a mis-resolved sense.

    ``backend_type`` / ``connector_name`` select the transport for non-sense
    sources. ``"http"`` (the default) fetches each source with the SSRF-guarded
    GET against ``base_url`` — unchanged. ``"connector"`` routes each non-sense
    source through the pocket's bound connector (``connector_name``) via
    ``connectors_service.execute``; ``base_url`` is then unused (and not
    validated, since a connector backend has none).
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
    # validation already ran. Defense in depth against a tampered row. A
    # connector backend has no base_url (it routes through the connector action
    # surface, not an HTTP GET), so the SSRF re-validation does not apply.
    if backend_type != "connector":
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
                        workspace_id=workspace_id,
                        pocket_id=pocket_id,
                        user_id=user_id,
                        backend_type=backend_type,
                        connector_name=connector_name,
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


__all__ = ["run_sources", "selected_source_keys", "SourceBinding", "RefreshTrigger"]
