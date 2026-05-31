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
# Updated: 2026-05-22 (RFC 05 M2b.1 — Instinct routing) — the M2a
#   fail-closed `_instinct_rejected` gate is GONE. `ActionBinding` now
#   declares the real governance fields (`requires_instinct`,
#   `instinct_policy`, `outcome`) with a `model_validator` that rejects an
#   `instinct_policy` set without `requires_instinct`. `run_action` takes a
#   `from_instinct` flag: when a binding requires instinct and the call is
#   NOT already post-approval, the executor runs every cheap validation
#   gate (rate-limit, base-URL, SSRF/allowlist/DNS) so a write the
#   allowlist would reject is rejected NOW, then returns an
#   `instinct_pending` sentinel carrying the resolved write under `_park`
#   — it makes NO HTTP call. The executor stays pure (no Beanie/Instinct
#   imports); `instinct_bridge.py` does the proposing. A successful write
#   result now carries the binding's `outcome` string for M2b.2 emission.
# Updated: 2026-05-22 (security-review fix for PR #1183, SHOULD-FIX 4) —
#   `_action_rate_limited` now bounds `_action_log`: a key whose
#   timestamp list is empty after window pruning is evicted, and stale
#   keys are swept opportunistically under the existing lock. Previously
#   the map grew one permanent entry per `(pocket, user)` pair that ever
#   ran a write — an unbounded memory leak in a long-running worker.
# Updated: 2026-05-28 (feat/wave-3a-instinct-dispatch) — RFC 03 v2
#   template-level Instinct gate. `run_action` accepts an optional
#   `template` parameter; when present and `from_instinct=False` the
#   executor calls `instinct_dispatch.gate_action(...)` BEFORE any of
#   the existing security guards (it's the cheapest possible failure
#   point — pure CEL evaluation, no I/O, no HTTP). The gate returns
#   one of three branches:
#     * blocked → return the new `instinct_blocked` sentinel; NO call.
#     * pending_approval → persist an `InstinctApproval` row via the
#       new EE-side service, return `instinct_pending` carrying the
#       `approval_id`; NO call.
#     * proceed → fall through into the existing gate stack (rate
#       limit / base-URL / SSRF / allowlist / DNS / M2b.1 park / HTTP).
#   When no template is passed (all current callers; backward-compat)
#   the gate is skipped — every existing flow is byte-identical.
# Updated: 2026-05-28 (feat/wave-3c-outcomes) — RFC 03 v2 template-level
#   outcome event emission. On the HTTP 2xx SUCCESS path, AFTER the
#   success audit and BEFORE the return, if a `template` is threaded
#   through the executor calls `outcomes_emitter.emit_outcomes(...)`.
#   The emitter fires one bus event per name declared in the action's
#   `outcomes_emitted[]`. Failure / blocked / pending-approval / from-
#   instinct paths do NOT emit — outcomes are billable, so only
#   confirmed direct success counts (the post-approval re-entry that
#   actually fires the HTTP call also emits, since `template` is
#   threaded through `instinct_bridge.execute_approved_write` callers
#   that pass it).
#   Emitter failures must never break the executor's return — the call
#   is wrapped so a bus or audit hiccup logs a warning but the
#   action's success result still propagates to the caller.
# Updated: 2026-05-25 (RFC 09 Slice 2 — Decision Graph live formation) —
#   `run_action` now MINTS a `correlation_id` at entry (Captain Decision 9
#   — every entry path through the chokepoint gets one for free; callers
#   can override via kwarg) and emits the chain-forming events through the
#   ``record_decision_event`` helper in ``decisions.journal_writer``:
#     * ``agent.proposed`` after gate-5 (allowlist) passes and before gate-7
#       (instinct park). GATED by ``not from_instinct`` so the Instinct
#       call-back path does NOT re-emit (audit Surprise 3 — would otherwise
#       reset chain ``proposed_at`` / ``intent`` / ``action`` via
#       ``_fold_proposed``).
#     * ``policy.evaluated(passed=True, policy="auto")`` + ``decision.completed
#       (landed)`` on the direct-path success branch (gated by
#       ``not binding.requires_instinct`` so the Instinct re-entry doesn't
#       double-emit with the bridge's site (b) at ``instinct_bridge.py``).
#       The policy emit makes the chain shape symmetric with the parked-then-
#       approved path — every chain carries a policy step.
#     * ``decision.completed(passed=False, action_outcome="failed",
#       error_class=...)`` on each of the 4 gate-8 failure branches (timeout,
#       backend HTTP error, guard error, unexpected exception). Same
#       ``not binding.requires_instinct`` guard — the Instinct re-entry
#       failure is closed by the bridge at ``instinct_bridge.py`` site (d).
#   correlation_id is THREADED through to ``_park`` so the bridge can stash
#   it on the parked Instinct Action (schema-2 bump in ``instinct_bridge``).
#
# A write has blast radius a read does not, so this executor adds three
# concerns on TOP of the shared SSRF guards:
#   1. The per-pocket WRITE ALLOWLIST (`allowed_writes`) — set by a human in
#      the backend config, OUTSIDE the spec. A method+path that does not
#      match an allowlist entry is rejected before any call leaves the
#      server. Authorship (the agent writes bindings) and authorization
#      (the human allow-lists the *class* of writes) are split.
#   2. INSTINCT PARK (M2b.1). A binding with `requires_instinct` is routed
#      through the Instinct approval surface instead of firing: a direct
#      run validates the write (gates 2-6) then returns the
#      `instinct_pending` sentinel with the resolved write under `_park`
#      and makes NO call. Only `instinct_bridge.execute_approved_write`,
#      re-entering with `from_instinct=True` after a human approval, runs
#      the actual HTTP call.
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
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator
from soul_protocol.spec.journal import Actor

from pocketpaw.security.url_validators import validate_external_url_strict
from pocketpaw_ee.cloud.decisions.journal_writer import (
    record_agent_proposed,
    record_decision_completed,
    record_policy_evaluated,
)
from pocketpaw_ee.cloud.pockets._http_guard import (
    _HTTP_TIMEOUT,
    _MAX_RESPONSE_BYTES,
    _assert_host_external,
    _auth_headers,
    _GuardError,
    _resolve_url,
    _strip_query,
)

if TYPE_CHECKING:
    # ``PocketTemplate`` ships in OSS (no EE import here, just typing).
    # Importing under TYPE_CHECKING keeps the runtime import graph
    # unchanged for callers that don't thread a template through.
    from pocketpaw.bundled_templates import PocketTemplate

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

    ``model_config`` still ignores unknown keys — a spec entry may carry
    RFC-03 template fields this executor does not act on, plus M2b.3's
    deferred ``approve_batch`` extras. Ignoring them keeps a forward-dated
    spec parseable instead of crashing on an unknown field.

    M2b.1 promotes the three governance fields to REAL declared fields:

    * ``requires_instinct`` — when true the write is routed through the
      Instinct approval surface instead of firing directly.
    * ``instinct_policy`` — how Instinct batches the write
      (``approve_per_row`` is the only policy this build executes;
      ``approve_batch`` is reserved for M2b.3 and not yet implemented).
    * ``outcome`` — the named outcome a successful run emits as a
      ``pocket.outcome`` event (M2b.2). ``None`` means no emit.

    A ``model_validator`` rejects an ``instinct_policy`` set WITHOUT
    ``requires_instinct`` — a policy with no gate to apply to is a
    misconfiguration, and silently ignoring it would hide the author's
    intent.
    """

    model_config = {"extra": "ignore"}

    kind: Literal["write_binding"] = "write_binding"
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str
    params: dict = Field(default_factory=dict)
    confirm: bool = False
    on_success: list[dict] = Field(default_factory=list)
    on_error: list[dict] = Field(default_factory=list)
    # --- M2b governance / metering fields (RFC 05 M2b.1) ----------------
    requires_instinct: bool = False
    instinct_policy: Literal["approve_per_row", "approve_batch"] | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def _policy_needs_gate(self) -> ActionBinding:
        """An ``instinct_policy`` is meaningless without ``requires_instinct``.

        Setting a policy but leaving the gate off would silently never
        route the write — reject the binding so the misconfiguration is
        caught at parse time (``agent_set_action`` and the executor both
        validate through this model).
        """
        if self.instinct_policy is not None and not self.requires_instinct:
            raise ValueError(
                "instinct_policy is set but requires_instinct is false — "
                "a policy needs the instinct gate enabled"
            )
        return self


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

    SHOULD-FIX 4 (PR #1183) — the map is bounded: a key whose timestamp
    list is empty after pruning is evicted, and stale keys are swept
    opportunistically. Without this every ``(pocket, user)`` pair that
    ever ran a write would leave a permanent entry — an unbounded
    process-lifetime memory leak in a long-running cloud worker.
    """
    key = (pocket_id, user_id)
    now = time.monotonic()
    window_start = now - _ACTION_RATE_LIMIT_WINDOW_S
    async with _action_log_lock:
        # Opportunistic sweep — drop any OTHER key whose stamps have all
        # aged out of the window. Bounds the dict to keys with live
        # traffic. Cheap: the map only ever holds active (pocket, user)
        # pairs, and the sweep runs under the same lock the check needs.
        stale = [
            k
            for k, ts in _action_log.items()
            if k != key and not any(t >= window_start for t in ts)
        ]
        for k in stale:
            del _action_log[k]

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


# ---------------------------------------------------------------------------
# RFC 09 Slice 2 — Decision-Graph chain emitters
# ---------------------------------------------------------------------------
# Each helper is a thin wrapper over `record_decision_event` so the call
# sites read as one logical line and a future agent looking at gate 8
# does not have to reconstruct the Actor / scope shape from scratch.
#
# Failure isolation: each helper swallows any exception raised by the
# emit path. A Decision-Graph wiring bug or a downstream projection crash
# must NEVER fail the producer's request — the journal-side row is the
# source of truth, the Slice 4 reconciler is the safety net (RFC 09 §
# Architecture). The helper in `journal_writer.py` already swallows
# `projection.apply` failures; this outer layer guards against the journal
# itself raising (e.g. a transient SQLite lock).
#
# Actor shape: the producer is the pocket runtime (a system actor on
# behalf of the user who initiated the write). `id` is the user_id with a
# `user:` prefix so a future reader of the chain can attribute the
# proposal to the human who triggered it; `scope_context` carries the
# workspace + pocket so visibility filters narrow correctly.


def _chain_actor(*, user_id: str, workspace_id: str, pocket_id: str) -> Actor:
    """Build the Actor recorded on each chain-forming event.

    `kind="agent"` because the pocket runtime is the actor that
    PROPOSED the write (the chat agent / Tier-0 deterministic router /
    REST endpoint all flow through this same chokepoint — see RFC 09
    Captain Decision 9). The user_id rides along on the Actor's `id`
    so the projection can attribute the chain to the human who started
    it; the workspace + pocket scopes ride on `scope_context` so the
    `_visible()` filter on `DecisionStore.find` narrows correctly.
    """
    safe_user = user_id or "unknown"
    return Actor(
        kind="agent",
        id=f"user:{safe_user}",
        scope_context=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
    )


def _chain_scope(*, workspace_id: str, pocket_id: str) -> list[str]:
    """Scope tag set stamped onto every chain event for this pocket write.

    Matches the producer-1 / producer-4 scope shape in RFC 09 § Privacy
    / scope. The projection's `_visible()` check intersects these tags
    with the requester's scopes.
    """
    return [f"workspace:{workspace_id}", f"pocket:{pocket_id}"]


def _safe_record(record_fn, *, correlation_id: UUID, **kwargs):
    """Best-effort chain emit; swallow + log failures.

    Wraps any of the per-action wrappers from
    ``pocketpaw_ee.cloud.decisions.journal_writer`` so a Decision-Graph
    wiring bug or transient SQLite-locked exception cannot fail the
    producer's request. Returns the event id on success so the parked
    path can stash it as ``causation_id`` for the next event in the
    chain. Returns ``None`` when the emit raised — the Slice 4 reconciler
    will reconcile from the journal cursor on the next tick.
    """
    try:
        entry = record_fn(correlation_id=correlation_id, **kwargs)
        return entry.id
    except Exception:  # noqa: BLE001 — see "Failure isolation" above
        logger.warning(
            "decision-chain emit failed for %s correlation_id=%s",
            getattr(record_fn, "__name__", "record_*"),
            correlation_id,
            exc_info=True,
        )
        return None


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
    from_instinct: bool = False,
    template: PocketTemplate | None = None,
    row_context: dict[str, Any] | None = None,
    workspace_context: dict[str, Any] | None = None,
    row_id: str = "",
    correlation_id: UUID | None = None,
) -> dict:
    """Run ONE pocket write action against its configured backend.

    ``raw_action`` is the action's entry from the persisted
    ``rippleSpec.actions`` block — the server reads ``method`` /
    ``confirm`` / ``on_success`` / ``on_error`` / the M2b governance
    fields from it (a compromised client cannot pick the verb). ``path``
    and ``params`` arrive from the client already resolved by Ripple's
    ``{...}`` expression resolver.

    ``from_instinct`` is ``False`` for a direct run and ``True`` only when
    ``instinct_bridge.execute_approved_write`` re-enters this function
    after a human approved the parked write. When a binding has
    ``requires_instinct`` and ``from_instinct`` is ``False`` the executor
    runs every cheap validation gate then PARKS the write — it returns the
    ``instinct_pending`` sentinel and makes NO HTTP call.

    ``correlation_id`` (RFC 09 Slice 2 — Captain Decision 9) is the
    Decision-Graph chain id. When ``None`` (the default) the executor
    mints a fresh UUID at entry — every chat-agent / Tier-0 router /
    REST entry path through this chokepoint gets one for free. The
    Instinct re-entry path passes the original id back in so the chain
    folds correctly (``execute_approved_write`` reads it off the parked
    Action's schema-2 ``_pocket_write`` blob). On the parked path the
    minted / supplied id rides on the ``_park`` dict so the bridge can
    stash it on the Instinct Action.

    The result shape on a fired success::

        {"ok": true, "action", "status", "response", "outcome",
         "on_success": [...], "on_error": [...]}

    On the parked (instinct-pending) path::

        {"ok": true, "code": "instinct_pending", "_park": <write dict>,
         "action", "on_success": [], "on_error": []}

    On failure::

        {"ok": false, "action", "error", "code", "on_error": [...]}

    The executor is pure: it makes the one HTTP call (or signals a park)
    and returns. It does NOT persist to the Pocket document, does NOT emit
    ``pocket_mutation``, and does NOT touch Beanie or the Instinct store —
    the calling route / ``instinct_bridge`` own that.

    Gate order (each gate makes NO call when it rejects):
      1. Parse the binding (``ActionBinding``); a malformed entry is a
         ``bad_binding`` rejection.
      2. Write rate limit — 20 writes / 60s / (pocket, user).
      3. Strict base-URL re-validation (defense in depth).
      4. ``_resolve_url`` — path-traversal / absolute-URL / cross-host
         rejection (shared SSRF guard).
      5. ALLOWLIST — ``(method, query-stripped, percent-decoded path)``
         must match an ``allowed_writes`` entry; a miss is audited WARNING
         ``rejected``.
      5b. RFC 09 Slice 2 — emit ``agent.proposed`` (chain-forming).
         Suppressed when ``from_instinct`` is True (re-entry guard —
         audit Surprise 3). A write rejected at gate 5 never reaches the
         proposal point in the agent's mental model and so produces no
         chain (Captain Decision 10).
      6. DNS pre-resolve — reject a host that resolves internal.
      7. INSTINCT PARK — when the binding requires instinct and this is
         not a post-approval call, return the ``instinct_pending``
         sentinel after gates 2-6 have validated the write. A write the
         allowlist would reject is rejected here, NOT parked. The
         ``correlation_id`` rides on ``_park`` so the bridge can stash
         it on the parked Action's ``_pocket_write`` blob.
      8. The HTTP call: redirects disabled, 3xx is an error, tight
         timeouts, 512 KB response cap, sanitized errors. On the direct
         (non-Instinct) path: emit ``policy.evaluated(passed=True,
         policy="auto")`` + ``decision.completed(landed)`` on success,
         or ``decision.completed(failed)`` on each of the 4 except
         branches. The Instinct re-entry path (``from_instinct=True``)
         skips both emits — the bridge owns the chain close.
    """
    # ── 0. RFC 09 Slice 2 — mint the correlation_id (Captain Decision 9) ──
    # Every entry path (chat agent, Tier-0 deterministic pocket-router,
    # direct REST endpoint) flows through here, so minting at this
    # chokepoint covers every code path uniformly. Caller may override
    # (e.g. the Instinct re-entry from `execute_approved_write` passes the
    # original id back in so the chain folds correctly).
    correlation_id = correlation_id or uuid4()

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

    # ── 1.5. RFC 03 v2 template-level Instinct gate ─────────────────────
    # When a template is threaded through (and we're not re-entering
    # post-approval), evaluate `resolve_instinct` via the dispatch
    # wrapper. The wrapper is the only impure layer that talks to the
    # `instinct_approvals` Beanie collection — this module stays pure
    # of Beanie imports (import-linter contract).
    #
    # The gate is the CHEAPEST failure mode in the stack: pure CEL eval
    # on the row + workspace context, no HTTP, no SSRF resolve, no DNS
    # lookup. A BLOCK / ESCALATE_APPROVAL verdict short-circuits every
    # downstream guard.
    if template is not None and not from_instinct:
        from pocketpaw_ee.cloud.pockets import instinct_dispatch

        park = {
            "action": action,
            "method": method,
            "path": path,
            "params": params,
            "idempotency_key": idempotency_key,
            "outcome": binding.outcome,
        }
        gate = await instinct_dispatch.gate_action(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            template=template,
            action_name=action,
            row_context=row_context or {},
            workspace_context=workspace_context,
            row_id=row_id,
            park=park,
        )
        if gate.next_step == "blocked":
            _audit_action_run(
                actor=user_id,
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                action=action,
                status="instinct-blocked",
                base_url=base_url,
            )
            return {
                "ok": False,
                "code": "instinct_blocked",
                "action": action,
                "error": "action blocked by Instinct rule",
                "reason": gate.decision.reason,
                "on_error": on_error,
            }
        if gate.next_step == "pending_approval":
            _audit_action_run(
                actor=user_id,
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                action=action,
                status="instinct-pending",
                base_url=base_url,
            )
            return {
                "ok": True,
                "code": "instinct_pending",
                "action": action,
                "approval_id": gate.approval_id,
                "reason": gate.decision.reason,
                "_park": park,
                "on_success": [],
                "on_error": [],
            }
        # gate.next_step == "proceed" — fall through into the existing
        # gate stack. Notify rules are dropped on the floor here; Wave
        # 3c wires the side-effect dispatcher.

    # ── 2. write rate limit ─────────────────────────────────────────────
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

    # ── 3. strict base-URL re-validation ────────────────────────────────
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

    # ── 4. resolve + SSRF-guard the path ────────────────────────────────
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

    # ── 5. allowlist check ──────────────────────────────────────────────
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

    # ── 5b. RFC 09 Slice 2 — emit agent.proposed ────────────────────────
    # The allowlist passed (gate 5), so this write is something the agent
    # is actually going to *propose* — either fire it directly (gate 8)
    # or park it for human approval (gate 7). Emit BEFORE the park so the
    # chain starts at the proposal moment; the same chain will close at
    # gate 8 (direct) or via the Instinct bridge (parked → approved).
    #
    # Re-entry guard (audit Surprise 3): when `from_instinct=True` we are
    # inside the post-approval execution of an already-proposed write —
    # the proposal was emitted on the FIRST entry. Re-emitting here would
    # overwrite the chain's `proposed_at` / `intent` / `action` via
    # `_fold_proposed` (projection.py:300+) on every Instinct-approved
    # write — a subtle bug that turns a single Decision into a "double
    # proposed" mess. Suppress the emit on re-entry.
    if not from_instinct:
        _safe_record(
            record_agent_proposed,
            correlation_id=correlation_id,
            actor=_chain_actor(user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id),
            scope=_chain_scope(workspace_id=workspace_id, pocket_id=pocket_id),
            payload={
                # Fields the projection's `_fold_proposed` consumes
                # (`projection.py:_fold_proposed`): intent / action /
                # pocket_id / inputs. Extra fields ride along for the
                # explain narrator and a future swap to soul-protocol's
                # `build_proposal_event(AgentProposal(...))` once the
                # Slice 1a wheel publishes (TODO(rfc09-slice-1a-wheel)).
                "intent": f"{method} {path_decoded} via pocket {pocket_id}",
                "action": action,
                "pocket_id": pocket_id,
                "inputs": [],
                # AgentProposal-shaped fields for future-builder compat.
                "proposal_kind": "pocket_write",
                "summary": f"{method} {path_decoded} via pocket {pocket_id}",
                "proposal": {
                    "method": method,
                    "path": path_decoded,
                    "binding": {
                        "requires_instinct": binding.requires_instinct,
                        "instinct_policy": binding.instinct_policy,
                        "outcome": binding.outcome,
                    },
                },
            },
        )

    # ── 6. DNS pre-resolve ──────────────────────────────────────────────
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

    # ── 7. instinct park ────────────────────────────────────────────────
    # A binding that requires instinct, on a direct (not post-approval)
    # run, is PARKED — not fired. Gates 2-6 already ran as VALIDATION, so a
    # write the allowlist would reject was rejected above, NOT parked: an
    # off-policy write must never reach the approval surface looking
    # legitimate. The executor stays pure — it just returns the resolved
    # write under `_park` and signals `instinct_pending`. The router hands
    # `_park` to `instinct_bridge.propose_pocket_write`, which builds the
    # Instinct Action. NO HTTP call is made here.
    if binding.requires_instinct and not from_instinct:
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="instinct-pending",
            base_url=base_url,
        )
        return {
            "ok": True,
            "code": "instinct_pending",
            "action": action,
            "_park": {
                "action": action,
                "method": method,
                "path": path,
                "params": params,
                "idempotency_key": idempotency_key,
                "outcome": binding.outcome,
                # RFC 09 Slice 2 — correlation_id rides on _park so the
                # bridge can stash it on the parked Action's schema-2
                # _pocket_write blob. The Instinct router reads it back
                # at approve/reject time to chain policy.evaluated +
                # human.corrected + decision.completed under the same
                # correlation as the agent.proposed we already emitted.
                "correlation_id": str(correlation_id),
            },
            "on_success": [],
            "on_error": [],
        }

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
        _emit_direct_path_failure(
            binding=binding,
            from_instinct=from_instinct,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            error_class="TimeoutError",
            reason="timeout",
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
        _emit_direct_path_failure(
            binding=binding,
            from_instinct=from_instinct,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            error_class="BackendHTTPError",
            reason="http_error",
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
        _emit_direct_path_failure(
            binding=binding,
            from_instinct=from_instinct,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            error_class="GuardError",
            reason=exc.code,
        )
        return _error(action, exc.message, exc.code, on_error)
    except Exception as exc:  # noqa: BLE001 — never let a raw exception escape
        logger.warning("pocket %s action %s: unexpected failure", pocket_id, action, exc_info=True)
        _audit_action_run(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            action=action,
            status="error",
            base_url=base_url,
        )
        _emit_direct_path_failure(
            binding=binding,
            from_instinct=from_instinct,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            error_class=type(exc).__name__,
            reason="unexpected_error",
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

    # ── RFC 03 v2 outcome event emission ────────────────────────────────
    # On a confirmed HTTP 2xx success, fire one ``OutcomeEmitted`` event
    # per name declared in the action's ``outcomes_emitted[]``. Wave 3c.
    # Emission is gated on a threaded ``template`` — every legacy caller
    # that doesn't pass one is byte-identical (zero events emitted).
    # The emit is wrapped so a bus / audit hiccup never propagates back
    # into the caller's success path; the action genuinely succeeded.
    if template is not None:
        from pocketpaw_ee.cloud.pockets import outcomes_emitter

        try:
            await outcomes_emitter.emit_outcomes(
                workspace_id=workspace_id,
                user_id=user_id,
                pocket_id=pocket_id,
                template=template,
                action_name=action,
                row_id=row_id,
                row_context=row_context or {},
            )
        except Exception:  # noqa: BLE001 — emission must never break the success path
            logger.warning(
                "outcome emission failed for action=%s pocket=%s",
                action,
                pocket_id,
                exc_info=True,
            )

    # RFC 09 Slice 2 — direct-path (non-Instinct) close: emit the
    # auto-approve `policy.evaluated(passed=True)` for chain symmetry
    # with the parked-then-approved path, then the `decision.completed`
    # terminal. Guarded by `not binding.requires_instinct` so the
    # Instinct re-entry path (where `from_instinct=True` AND
    # `binding.requires_instinct=True`) does NOT double-emit with the
    # bridge at `instinct_bridge.execute_approved_write` site (b). The
    # captain's brief: "if there's a path that doesn't go through
    # Instinct's human approval flow, emit policy.evaluated(passed=True)
    # AND decision.completed(landed) in sequence."
    if not binding.requires_instinct:
        actor = _chain_actor(user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id)
        scope = _chain_scope(workspace_id=workspace_id, pocket_id=pocket_id)
        policy_event_id = _safe_record(
            record_policy_evaluated,
            correlation_id=correlation_id,
            actor=actor,
            scope=scope,
            payload={
                "policy": "auto",
                "passed": True,
                "evaluator": "auto",
            },
        )
        _safe_record(
            record_decision_completed,
            correlation_id=correlation_id,
            actor=actor,
            scope=scope,
            payload={
                "passed": True,
                "action_outcome": "landed",
            },
            causation_id=policy_event_id,
        )

    return {
        "ok": True,
        "action": action,
        "status": result["status"],
        "response": result["response"],
        # M2b.2 — the named outcome a successful write emits as a
        # `pocket.outcome` event. `None` when the binding declares none;
        # the emit helper treats `None` as a no-op.
        "outcome": binding.outcome,
        "on_success": binding.on_success,
        "on_error": on_error,
    }


def _emit_direct_path_failure(
    *,
    binding: ActionBinding,
    from_instinct: bool,
    correlation_id: UUID,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    error_class: str,
    reason: str,
) -> None:
    """Emit ``decision.completed(failed)`` for the direct (non-Instinct) path.

    Same guard as the success emit: only fires when the binding is NOT
    Instinct-gated. The Instinct re-entry failure path closes the chain
    from ``instinct_bridge.execute_approved_write`` site (d) instead, so
    we'd double-emit if we fired here too. ``error_class`` is the Python
    exception type name (TimeoutError / BackendHTTPError / GuardError /
    type(exc).__name__) for the narrator's "why did this fail" story.
    """
    if binding.requires_instinct:
        return
    _safe_record(
        record_decision_completed,
        correlation_id=correlation_id,
        actor=_chain_actor(user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id),
        scope=_chain_scope(workspace_id=workspace_id, pocket_id=pocket_id),
        payload={
            "passed": False,
            "action_outcome": "failed",
            "error_class": error_class,
            "reason": reason,
        },
    )


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
