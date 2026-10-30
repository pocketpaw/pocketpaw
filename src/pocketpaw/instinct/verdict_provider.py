# Instinct VerdictProvider seam — the verifier injection point for the
# Self-Verifying Loop (SVL-1).
# Created: 2026-06-23 (feat/svl-1-verify-stamp)
#
# The Self-Verifying Loop needs a place to *swap in* how an outcome is judged
# without the producing agent (or the deep_work executor) hard-wiring a
# concrete verifier. This module is that seam:
#
#   - VerdictProvider — the protocol. Any verifier that can turn an action
#     result + its captured success_criteria into a structured
#     OutcomeVerdict satisfies it.
#   - DeterministicVerdictProvider — the shipped default. It delegates to the
#     deterministic, no-LLM verify_outcome() from
#     pocketpaw.instinct.verification, so SVL-1 stamps a repeatable verdict
#     with zero model calls. Later slices can introduce an LLM-as-judge
#     provider behind the same protocol without touching the executor hook.
#
# OSS-core constraint: this file MUST NOT import pocketpaw_ee — an
# import-linter contract enforces it. The verifier is external to the agent
# that produced the result (verify_outcome is a pure deterministic check), so
# the loop's "verify is independent of the producer" invariant holds.

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pocketpaw.instinct.models import OutcomeVerdict
from pocketpaw.instinct.verification import verify_outcome


@runtime_checkable
class VerdictProvider(Protocol):
    """Turns an action result + its success criteria into an OutcomeVerdict.

    The injection seam for the Self-Verifying Loop. Implementations decide
    *how* to judge an outcome (deterministic token match, LLM-as-judge, a
    hybrid); callers depend only on this shape, so the verifier can be
    swapped without the deep_work executor knowing which one is wired in.
    """

    def verify(self, result: Any, success_criteria: list[str]) -> OutcomeVerdict:
        """Verify a result against its captured success criteria.

        Args:
            result: The action result — string, dict, or list.
            success_criteria: The verifiable end-state checks captured at
                task intake. May be empty (yields an ``UNKNOWN`` verdict).

        Returns:
            A structured :class:`OutcomeVerdict`.
        """
        ...


class DeterministicVerdictProvider:
    """The shipped default VerdictProvider — no LLM, fully repeatable.

    Delegates to :func:`pocketpaw.instinct.verification.verify_outcome`, the
    deterministic foundation verifier (issue #1162). Same input always yields
    the same verdict; no model call. This is the provider SVL-1 stamps with.
    """

    def verify(self, result: Any, success_criteria: list[str]) -> OutcomeVerdict:
        """Delegate to the deterministic ``verify_outcome`` check."""
        return verify_outcome(result, success_criteria)
