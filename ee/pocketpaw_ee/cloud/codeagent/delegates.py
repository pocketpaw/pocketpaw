# delegates.py — The browser-delegate channel for Code Mode (CD-1).
#
# Created 2026-07-22 (feat/code-delegate-channel). The main chat agent is about
# to gain ONE tool, ``code_mode``, that hands a coding task to the Code Mode
# sub-agent. That tool runs in the BACKEND, but the work has to happen in the
# USER'S BROWSER — a WebContainer project runs in the tab and has no server-side
# row for a backend to reach, which is the same constraint that shaped the rest
# of this module (see ``service.py``: the server never opens a file).
#
# So the call is inverted. The backend does not execute the task; it PARKS on a
# future, pushes a ``code_delegate`` frame down the live SSE stream, and waits
# for the browser to post the answer back to ``POST /codeagent/resolve``. This
# module is both ends of that rendezvous and nothing else — it knows what a
# correlation id is and how to wake a parked caller, and it knows nothing about
# what the task said or what the browser did with it.
#
# Modelled on ``pocketpaw.agents.plan_mode.PlanApprovalManager``, the shipped
# precedent for parking a backend caller with a timeout. Two deliberate
# differences: this is keyed by CORRELATION ID rather than by session (one turn
# can delegate more than once, and a session key cannot tell two of them apart),
# and the future carries a RESULT PAYLOAD rather than a bare approve/reject.
#
# The one property everything here is built around: A PARKED CALLER MUST NEVER
# HANG. A browser that closed the tab, a user that navigated away, a frame that
# was pushed into a stream nobody is reading — each of those has to end as an
# error the tool can report, not as a chat turn that sits there forever. Every
# exit from ``wait`` therefore produces a ``DelegateOutcome``, and every exit
# removes the registry entry.
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError

logger = logging.getLogger(__name__)

# ── The park budget ─────────────────────────────────────────────────────────
# How long the backend will hold a turn open waiting for the browser.
#
# This is a MACHINE round trip, not a human decision, so ``plan_mode``'s 300s
# (a person reading a plan and clicking approve) is the wrong reference. The
# right lower bound is the browser's own work: the delegate frame kicks off at
# least one ``POST /codeagent/turn``, and that call alone is capped at
# ``MODEL_TIMEOUT_SECONDS`` = 120. Anything under two minutes would therefore
# expire the delegate BEFORE the sub-agent's own model call could even fail, and
# the user would be told "the browser did not answer" about a browser that was
# still working.
#
# 180 is that floor plus headroom for the SSE hop and the client's own fs reads.
# It deliberately does NOT cover the worst case — Code Mode's retrieval loop can
# run up to ``MAX_TOOL_ITERATIONS`` rounds, which no bounded park would survive.
# That is the correct trade: this number is a LIVENESS guarantee for the chat
# turn, not a spend budget for the sub-agent. A long task that overruns it
# reports a clean timeout instead of holding a stream open indefinitely.
#
# A spike will tune this against real delegate latencies; it is a module
# constant so that tuning is a one-line change with no call sites to chase.
CODE_DELEGATE_TIMEOUT_SECONDS = 180.0

# Ceiling on the payload the browser may post back. The client caps what it
# sends, but a server that trusts a client-supplied length has no ceiling at
# all — the same reasoning as ``MAX_TOOL_RESULT_CHARS`` in ``domain.py``, and
# the same order of magnitude as one turn's whole context budget.
MAX_DELEGATE_RESULT_CHARS = 200_000

# The SSE event name the browser listens for.
DELEGATE_EVENT = "code_delegate"

# Machine-readable failure codes on the outcome. They are codes rather than
# exceptions because the caller is an agent TOOL: a tool that raises kills the
# turn, a tool that returns "the browser did not answer" lets the model say so.
ERROR_TIMEOUT = "code_delegate.timeout"
ERROR_NO_CLIENT = "code_delegate.no_client"
ERROR_ABORTED = "code_delegate.aborted"


@dataclass(frozen=True)
class DelegateOutcome:
    """How one delegation ended.

    ``ok`` is the branch the caller reads. On success ``result`` is whatever the
    browser posted back, verbatim — this module does not interpret it, because
    the shape belongs to the Code Mode client and pinning it here would mean two
    places to change every time the sub-agent learns a new answer shape.

    On failure ``error`` carries one of the module's codes and ``message`` is the
    sentence a model can repeat to the user. Both are always present on a
    failure, and both are empty on a success.
    """

    ok: bool
    result: dict = field(default_factory=dict)
    error: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        """Flatten for a tool result. Keeps the failure codes machine-readable
        so a caller can branch on ``error`` rather than parsing prose."""
        if self.ok:
            return {"ok": True, "result": self.result}
        return {"ok": False, "error": self.error, "message": self.message}


@dataclass
class _Pending:
    """One parked caller. ``workspace_id`` is captured at park time so the
    resolve route can check that the poster belongs to the same tenant as the
    parked turn — the correlation id is unguessable, but tenancy that rests on
    unguessability is not tenancy."""

    workspace_id: str
    future: asyncio.Future[DelegateOutcome]


def _set_if_pending(fut: asyncio.Future[DelegateOutcome], outcome: DelegateOutcome) -> None:
    """Settle a future, tolerating a race that already settled it."""
    if not fut.done():
        fut.set_result(outcome)


class PendingDelegates:
    """Correlation id → parked caller.

    In-process and single-loop by construction, which is the honest description
    of the transport it sits on: ``push_sse_event`` writes to a queue held by the
    live HTTP stream in THIS worker. A resolve that lands on a different worker
    finds no entry and is rejected as unknown — a clean 404 rather than a silent
    drop, but still a real limitation worth knowing about before this ships
    behind more than one uvicorn process (see the report's concerns).
    """

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def __bool__(self) -> bool:
        """A registry is a registry whether or not anything is parked in it.

        Without this, ``__len__`` makes an EMPTY registry falsy, and the obvious
        way to write an optional-dependency default — ``registry or
        get_pending_delegates()`` — silently swaps a caller's injected registry
        for the process singleton at exactly the moment it is empty, which is
        every first use. The call sites use ``is None`` anyway; this closes the
        trap for the next one that does not.
        """
        return True

    def __contains__(self, corr_id: object) -> bool:
        return corr_id in self._pending

    def open(self, workspace_id: str) -> str:
        """Register a new parked slot and return its correlation id.

        Split from ``wait`` on purpose: the caller has to get the id BEFORE it
        pushes the frame, or the browser could answer a correlation id the
        registry has not heard of yet.
        """
        corr_id = uuid4().hex
        fut: asyncio.Future[DelegateOutcome] = asyncio.get_running_loop().create_future()
        self._pending[corr_id] = _Pending(workspace_id=workspace_id, future=fut)
        return corr_id

    def discard(self, corr_id: str) -> None:
        """Drop a slot without settling it. For the caller that failed to push —
        nobody is waiting on that future, so there is nothing to wake."""
        self._pending.pop(corr_id, None)

    async def wait(
        self,
        corr_id: str,
        *,
        timeout: float = CODE_DELEGATE_TIMEOUT_SECONDS,
    ) -> DelegateOutcome:
        """Park until the browser resolves ``corr_id``, or the budget runs out.

        Returns an outcome on EVERY path, including timeout — a tool that raises
        on a slow browser turns a recoverable delay into a dead turn.

        The ``finally`` is the anti-leak guarantee: whether this returns, times
        out, or the awaiting task is cancelled (a disconnected client tears down
        the whole run task), the entry leaves the registry. A stranded entry is
        not just memory — it is a correlation id that a late browser could
        resolve into a future nobody is reading.
        """
        entry = self._pending.get(corr_id)
        if entry is None:
            # Only reachable if something discarded the slot between open and
            # wait. Report it rather than parking on a future nothing can reach.
            return DelegateOutcome(
                ok=False,
                error=ERROR_ABORTED,
                message="The delegated task was cancelled before it started.",
            )
        try:
            return await asyncio.wait_for(entry.future, timeout=timeout)
        except TimeoutError:
            logger.warning(
                "codeagent.delegate timed out after %.0fs corr=%s ws=%s",
                timeout,
                corr_id,
                entry.workspace_id,
            )
            return DelegateOutcome(
                ok=False,
                error=ERROR_TIMEOUT,
                message=(
                    "The browser did not finish the delegated task in "
                    f"{int(timeout)}s. It may still be running in the tab."
                ),
            )
        finally:
            self._pending.pop(corr_id, None)

    def resolve(self, corr_id: str, result: dict, *, workspace_id: str) -> bool:
        """Wake the caller parked on ``corr_id``. False if there is nobody there.

        The entry is popped BEFORE the future is settled, which makes a duplicate
        resolve indistinguishable from an unknown id — one lookup miss, one
        rejection path, no second mechanism to keep in step with the first.

        A workspace mismatch is also a miss rather than a distinct error: telling
        a caller "that id exists but is not yours" confirms the id exists.
        """
        entry = self._pending.get(corr_id)
        if entry is None:
            return False
        if entry.workspace_id != workspace_id:
            logger.warning(
                "codeagent.delegate resolve rejected: workspace mismatch corr=%s", corr_id
            )
            return False
        self._pending.pop(corr_id, None)
        return self._settle(entry.future, DelegateOutcome(ok=True, result=result))

    def abort(self, corr_id: str, *, message: str = "") -> bool:
        """Fail a parked caller deliberately (shutdown, stream teardown).

        Distinct from ``discard``: this one WAKES the waiter with an error rather
        than leaving it to time out, so a known-dead delegation costs the user
        nothing instead of three minutes.
        """
        entry = self._pending.pop(corr_id, None)
        if entry is None:
            return False
        return self._settle(
            entry.future,
            DelegateOutcome(
                ok=False,
                error=ERROR_ABORTED,
                message=message or "The delegated task was cancelled.",
            ),
        )

    def abort_all(self, *, message: str = "") -> int:
        """Abort every parked caller. Returns how many were woken."""
        return sum(1 for corr_id in list(self._pending) if self.abort(corr_id, message=message))

    @staticmethod
    def _settle(fut: asyncio.Future[DelegateOutcome], outcome: DelegateOutcome) -> bool:
        """Set a result on the future's OWN loop.

        Normally that is the loop we are already on and this is a plain
        ``set_result``. The cross-loop branch exists because the resolve arrives
        on an HTTP request while the parked caller lives in a run task, and
        nothing in the type system guarantees those share a loop forever;
        ``set_result`` from the wrong loop corrupts the waiter's scheduling in a
        way that is very hard to read back from a bug report.
        """
        if fut.done():
            return False
        loop = fut.get_loop()
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — resolve is always called from a loop
            running = None
        if running is loop:
            fut.set_result(outcome)
        else:  # pragma: no cover — single-loop in every shipped deployment
            loop.call_soon_threadsafe(_set_if_pending, fut, outcome)
        return True


# Singleton, mirroring ``plan_mode.get_plan_manager``. Both ends of the
# rendezvous — an MCP tool deep in a run task and a FastAPI route — need the
# SAME registry and neither can be handed one, so the process owns it.
_registry: PendingDelegates | None = None


def get_pending_delegates() -> PendingDelegates:
    """Get the process-wide pending-delegate registry."""
    global _registry
    if _registry is None:
        _registry = PendingDelegates()
    return _registry


async def delegate_to_browser(
    workspace_id: str,
    task: str,
    mode: str = "ask",
    *,
    timeout: float = CODE_DELEGATE_TIMEOUT_SECONDS,
    registry: PendingDelegates | None = None,
    push: Callable[[str, dict[str, Any]], None] | None = None,
) -> DelegateOutcome:
    """Hand ``task`` to the browser's Code Mode sub-agent and wait for its answer.

    This is the whole outbound half of the channel and the only entry point
    CD-2's ``code_mode`` tool needs: register a slot, push one ``code_delegate``
    frame, park.

    It FAILS FAST when there is no SSE stream in scope. ``push_sse_event`` is a
    documented no-op outside a stream, so without that check a tool invoked from
    a CLI handler, a background job, or a unit test would push into nothing and
    then park for the full budget before reporting a timeout that was knowable
    at once. The distinct ``no_client`` code also gives the model something true
    to say — "there is no browser attached" is a different problem from "the
    browser was slow".

    ``registry`` and ``push`` are DI seams, matching the ``client=`` seam
    ``service.run_turn`` uses so the suite runs with no stream and no network.
    """
    reg = get_pending_delegates() if registry is None else registry

    if push is None:
        # Deferred import: every other cloud module reaches ``agent_service``
        # this way, because importing it at module scope pulls the chat stack
        # into anything that touches codeagent and reintroduces a cycle.
        from pocketpaw_ee.cloud.chat.agent_service import has_sse_event_sink, push_sse_event

        if not has_sse_event_sink():
            logger.debug("codeagent.delegate refused: no SSE stream in scope")
            return DelegateOutcome(
                ok=False,
                error=ERROR_NO_CLIENT,
                message=(
                    "No browser session is attached to this conversation, so the "
                    "coding task cannot be run."
                ),
            )
        push = push_sse_event

    corr_id = reg.open(workspace_id)
    try:
        push(DELEGATE_EVENT, {"corrId": corr_id, "task": task, "mode": mode})
    except Exception:  # noqa: BLE001 — a failed push must not kill the turn
        logger.warning("codeagent.delegate push failed corr=%s", corr_id, exc_info=True)
        # Nobody is parked on this future yet, so drop the slot rather than
        # settling it — an abort here would wake a waiter that does not exist.
        reg.discard(corr_id)
        return DelegateOutcome(
            ok=False,
            error=ERROR_NO_CLIENT,
            message="The coding task could not be sent to the browser.",
        )

    logger.debug(
        "codeagent.delegate parked corr=%s ws=%s mode=%s timeout=%.0f",
        corr_id,
        workspace_id,
        mode,
        timeout,
    )
    return await reg.wait(corr_id, timeout=timeout)


def resolve_pending(
    workspace_id: str,
    corr_id: str,
    result: dict,
    *,
    registry: PendingDelegates | None = None,
) -> None:
    """Inbound half: deliver the browser's answer to the parked caller.

    Raises ``NotFound`` when there is nothing parked under ``corr_id`` — which
    covers an unknown id, a SECOND resolve of an id already answered, a resolve
    that arrives after the park timed out, and a resolve from the wrong
    workspace. They are one case on purpose: in all four, the honest answer is
    "there is no turn here waiting for you", and splitting them would leak
    whether a given id ever existed.
    """
    _guard_result_size(result)
    reg = get_pending_delegates() if registry is None else registry
    if not reg.resolve(corr_id, result, workspace_id=workspace_id):
        raise NotFound("code_delegate", corr_id)


def _guard_result_size(result: dict) -> None:
    """Reject a payload past ``MAX_DELEGATE_RESULT_CHARS``.

    Measured on the serialized form, since that is what the parked caller will
    end up putting in front of a model. A payload that cannot be serialized at
    all is rejected here too rather than exploding later inside a tool result.
    """
    try:
        size = len(json.dumps(result, default=str))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "code_delegate.invalid_result",
            "The delegate result could not be read",
        ) from exc
    if size > MAX_DELEGATE_RESULT_CHARS:
        raise ValidationError(
            "code_delegate.result_too_large",
            f"The delegate result exceeds {MAX_DELEGATE_RESULT_CHARS} characters",
        )


__all__ = [
    "CODE_DELEGATE_TIMEOUT_SECONDS",
    "DELEGATE_EVENT",
    "ERROR_ABORTED",
    "ERROR_NO_CLIENT",
    "ERROR_TIMEOUT",
    "MAX_DELEGATE_RESULT_CHARS",
    "DelegateOutcome",
    "PendingDelegates",
    "delegate_to_browser",
    "get_pending_delegates",
    "resolve_pending",
]
