# pocketpaw/agents/failover.py — L2 cross-backend (harness) failover (MCG-10).
#
# Created: 2026-06-26 (integration/model-catalog-v2, WU-D / MCG-10).
#
# What this is: the MECHANISM for failing over between agent HARNESSES when a
# whole backend lane is down. This is the *second* failover level:
#   * L1 (model / account) is owned by LiteLLM (multi-account weighted failover
#     + cross-group fallback to another provider). NOT here.
#   * L2 (harness / backend) is paw's job and LiteLLM CANNOT do it: when the
#     entire Claude Code lane is down (an Anthropic-wide overload), Claude
#     Code's own ``--fallback-model`` stays in Anthropic's capacity pool, so it
#     can't escape. We switch to a DIFFERENT harness instead:
#     claude_agent_sdk -> codex_cli -> opencode.
#
# Two hard rules that make this safe:
#   1. We only fail over on a LANE-LEVEL failure — an overload / unavailable /
#      auth error from the provider that persists AFTER the backend's own
#      retries. A normal task error (the model answered, but wrongly) does NOT
#      trigger a switch. See ``classify_lane_failure``.
#   2. We only fail over if NOTHING was streamed to the user yet. A half-
#      streamed turn cannot be replayed on another harness without duplicating
#      tokens, so once a user-visible event has been yielded we surface the
#      error instead of silently restarting. See the ``_streamed`` guard.
#
# Each harness in the chain is tried at most once; when the chain is exhausted
# the LAST error is surfaced. Every switch is audit-logged (which harness, why,
# the error class) via the shared OSS audit logger.
#
# OSS-pure: no ``pocketpaw_ee`` import. The EE cloud run path (``run_core``)
# wiring to actually CALL ``AgentRouter.run_with_failover`` is a follow-up
# (the IGW-seam pattern) — this slice ships the mechanism + the OSS hook.

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from pocketpaw.agents.protocol import AgentEvent

logger = logging.getLogger(__name__)

# Event types that are USER-VISIBLE: once one of these is yielded we have
# committed output to the user and can no longer fail over (no-replay rule).
# "thinking"/"thinking_done" go to the Activity panel only, but they are still
# emitted content we cannot cleanly replay on a different harness, so they also
# latch the no-replay guard. "token_usage" is bookkeeping (no visible content)
# and "done" is terminal, so neither latches the guard on its own — though in
# practice a clean "done" ends the run before failover is considered.
_STREAMED_EVENT_TYPES: frozenset[str] = frozenset(
    {"message", "tool_use", "tool_result", "thinking", "thinking_done"}
)

# Substrings that mark a provider LANE-DOWN condition: capacity exhaustion,
# service unavailability, or a credential/authorization failure that the
# backend could not recover from on its own. Matched case-insensitively against
# the error text (an exception's str() or an error AgentEvent's content). These
# are deliberately conservative — a generic "error" with none of these markers
# is treated as a normal task error and does NOT trigger a harness switch.
_LANE_DOWN_PATTERNS: tuple[str, ...] = (
    # Capacity / overload (Anthropic 529 "Overloaded", OpenAI/others rate limit)
    "overloaded",
    "overload",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "too_many_requests",
    "429",
    "529",
    "capacity",
    "quota",
    "usage limit",
    "usage_limit",
    "insufficient_quota",
    # Service unavailable / outage
    "service unavailable",
    "service_unavailable",
    "temporarily unavailable",
    "unavailable",
    "503",
    "502",
    "504",
    "bad gateway",
    "gateway timeout",
    "upstream",
    "overloaded_error",
    "api_error",
    "internal server error",
    # Auth / credential failures (lane is unusable for this harness)
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "permission denied",
    "permission_error",
    "forbidden",
    "401",
    "403",
)

# Compiled once. Word-ish boundaries are not used because the markers appear
# inside longer provider strings (e.g. "anthropic.APIStatusError: 529
# {'type': 'overloaded_error'}"); a plain case-insensitive substring scan is
# the right tool and avoids false negatives from punctuation.
_LANE_DOWN_RE = re.compile("|".join(re.escape(p) for p in _LANE_DOWN_PATTERNS), re.IGNORECASE)


def classify_lane_failure(error_text: str | None) -> bool:
    """Return True if *error_text* looks like a LANE-DOWN failure.

    A lane-down failure is a provider capacity / availability / credential
    error that persists after the backend's own retries — the signal that the
    *whole harness lane* is unusable right now, so switching to a different
    harness is worth trying. A normal task error (the model ran but produced a
    bad answer, a tool failed, the prompt was rejected on content grounds)
    returns False: another harness would hit the same wall, so we must not
    burn the chain on it.

    Empty / None text returns False (nothing to classify as lane-down).
    """
    if not error_text:
        return False
    return bool(_LANE_DOWN_RE.search(error_text))


# A factory that builds (or fetches a cached) backend instance for a given
# harness name, or returns None if that harness is unavailable (missing deps,
# failed init). The router supplies this; tests supply a fake.
BackendFactory = Callable[[str], Any]


class BackendFailoverRunner:
    """Drives an agent run across an ordered chain of HARNESSES (L2 failover).

    Construction is cheap and stateless apart from the injected factory; build
    one per logical run (or reuse — it holds no per-run state across calls).

    Usage::

        runner = BackendFailoverRunner(chain, get_backend)
        async for event in runner.run(message, system_prompt=..., ...):
            yield event

    Behavior:
      * Try each harness in ``chain`` in order. The first harness is the
        primary lane; the rest are the escape hatches.
      * If a harness raises or yields a ``type="error"`` event whose text is
        classified lane-down BEFORE any user-visible event was streamed, audit-
        log the switch and move to the next harness.
      * If output was already streamed, surface the error as-is (no replay) and
        stop — we never silently restart a half-streamed turn.
      * If an error is NOT lane-down (a normal task error), surface it and stop
        — another harness would hit the same wall.
      * Each harness is tried at most once. When the chain is exhausted, the
        last error is surfaced.
    """

    def __init__(
        self,
        chain: list[str],
        get_backend: BackendFactory,
        *,
        audit_actor: str = "agent",
    ) -> None:
        self._chain = list(chain)
        self._get_backend = get_backend
        self._audit_actor = audit_actor

    async def run(
        self,
        message: str,
        **run_kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Run *message* across the harness chain. Yields ``AgentEvent``s.

        ``run_kwargs`` (system_prompt / history / session_key / the optional
        per-surface frozensets) are forwarded verbatim to each backend's
        ``run`` — the runner does not interpret them.
        """
        last_error: str | None = "All configured harnesses failed"
        tried_any = False

        for index, backend_name in enumerate(self._chain):
            backend = self._get_backend(backend_name)
            if backend is None:
                logger.warning("Harness '%s' unavailable — skipping", backend_name)
                continue

            tried_any = True
            is_primary = index == 0
            # Per-attempt streaming latch: flips True the instant a user-visible
            # event leaves this harness. Once True we can no longer fail over.
            streamed = False
            attempt_error: str | None = None

            # ``failed_over`` records that THIS harness hit a pre-stream lane-
            # down condition and we should advance to the next harness. We set
            # it instead of ``continue``-ing from inside the ``async for`` so
            # the generator is fully exhausted/closed before we move on.
            failed_over = False

            try:
                async for event in backend.run(message, **run_kwargs):
                    etype = getattr(event, "type", None)

                    # An error event: decide failover BEFORE forwarding it, so a
                    # lane-down error pre-stream is swallowed (we retry on the
                    # next harness) rather than shown to the user.
                    if etype == "error":
                        attempt_error = _event_text(event)
                        if not streamed and classify_lane_failure(attempt_error):
                            # Lane down, nothing streamed yet → fail over. Do
                            # NOT yield this error event; stop consuming this
                            # harness and advance to the next one.
                            last_error = attempt_error
                            failed_over = True
                            self._audit_switch(
                                from_backend=backend_name,
                                to_backend=self._next_available(index),
                                error_text=attempt_error,
                                error_class="error_event",
                                via="error_event",
                            )
                            break
                        # Output already streamed (no replay) OR a normal task
                        # error → surface it and stop entirely.
                        self._audit_no_failover(
                            backend_name,
                            attempt_error,
                            streamed=streamed,
                            reason="streamed" if streamed else "not_lane_down",
                        )
                        yield event
                        continue

                    # Normal event. Latch the no-replay guard on first user-
                    # visible output, then forward.
                    if etype in _STREAMED_EVENT_TYPES:
                        streamed = True
                    yield event

                    if etype == "done":
                        # Clean completion on this harness.
                        if not is_primary:
                            logger.info("Harness failover succeeded on '%s'", backend_name)
                        return

            except Exception as exc:  # noqa: BLE001 — classify, don't crash.
                attempt_error = str(exc)
                last_error = attempt_error
                if not streamed and classify_lane_failure(attempt_error):
                    logger.warning(
                        "Harness '%s' lane-down (raised): %s",
                        backend_name,
                        attempt_error,
                    )
                    self._audit_switch(
                        from_backend=backend_name,
                        to_backend=self._next_available(index),
                        error_text=attempt_error,
                        error_class=type(exc).__name__,
                        via="exception",
                    )
                    failed_over = True
                else:
                    # Streamed already, or not a lane-down error → surface + stop.
                    logger.warning(
                        "Harness '%s' failed (no failover: streamed=%s lane_down=%s): %s",
                        backend_name,
                        streamed,
                        classify_lane_failure(attempt_error),
                        attempt_error,
                    )
                    self._audit_no_failover(
                        backend_name,
                        attempt_error,
                        streamed=streamed,
                        reason="streamed" if streamed else "not_lane_down",
                    )
                    yield AgentEvent(type="error", content=attempt_error)
                    yield AgentEvent(type="done", content="")
                    return

            if failed_over:
                # Advance to the next harness in the chain.
                continue

            # Generator finished without a terminal "done" and without an error
            # we acted on. Treat as a soft completion: nothing left to yield.
            if attempt_error is None:
                return
            # An error event was surfaced (not lane-down / post-stream) and the
            # backend's generator ended without its own "done" — close cleanly.
            yield AgentEvent(type="done", content="")
            return

        # Chain exhausted (or nothing was runnable). Surface the last error.
        if not tried_any:
            logger.error("No harness in the failover chain was available")
        yield AgentEvent(
            type="error",
            content=last_error or "All configured harnesses failed",
        )
        yield AgentEvent(type="done", content="")

    # ── internals ────────────────────────────────────────────────────────

    def _next_available(self, index: int) -> str | None:
        """Name of the next harness AFTER *index* that resolves, or None."""
        for name in self._chain[index + 1 :]:
            if self._get_backend(name) is not None:
                return name
        return None

    def _audit_switch(
        self,
        *,
        from_backend: str,
        to_backend: str | None,
        error_text: str | None,
        error_class: str,
        via: str,
    ) -> None:
        """Audit-log a harness switch (which, why, error class). Best-effort."""
        try:
            from pocketpaw.security.audit import (
                AuditEvent,
                AuditSeverity,
                get_audit_logger,
            )

            get_audit_logger().log(
                AuditEvent.create(
                    severity=AuditSeverity.WARNING,
                    actor=self._audit_actor,
                    action="backend_failover",
                    target=f"{from_backend} -> {to_backend or '(chain exhausted)'}",
                    status="switch",
                    from_backend=from_backend,
                    to_backend=to_backend,
                    error_class=error_class,
                    detected_via=via,
                    # Truncate so a giant provider stack trace can't bloat the
                    # audit line; the class + a snippet is enough to triage.
                    error=(error_text or "")[:500],
                    level="L2_harness",
                )
            )
        except Exception:  # noqa: BLE001 — audit must never break a run.
            logger.exception("Failed to write backend-failover audit log")

    def _audit_no_failover(
        self,
        backend_name: str,
        error_text: str | None,
        *,
        streamed: bool,
        reason: str,
    ) -> None:
        """Audit-log that an error was SURFACED without a switch. Best-effort."""
        try:
            from pocketpaw.security.audit import (
                AuditEvent,
                AuditSeverity,
                get_audit_logger,
            )

            get_audit_logger().log(
                AuditEvent.create(
                    severity=AuditSeverity.INFO,
                    actor=self._audit_actor,
                    action="backend_failover",
                    target=backend_name,
                    status="no_switch",
                    reason=reason,
                    streamed=streamed,
                    error=(error_text or "")[:500],
                    level="L2_harness",
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write backend-failover audit log")


def _event_text(event: AgentEvent) -> str:
    """Extract a string from an error event's content for classification."""
    content = getattr(event, "content", "")
    if isinstance(content, str):
        return content
    return str(content)
