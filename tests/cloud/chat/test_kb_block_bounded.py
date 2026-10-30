# tests/cloud/chat/test_kb_block_bounded.py — the KB block cannot own the turn.
# Created: 2026-08-04 (perf/prompt-assembly).
#
# WHY. ``_build_kb_snippets_block`` ran one ``kb`` subprocess per scope, in a
# serial loop, with nothing bounding either. Both halves were measured on a live
# local instance on 2026-08-04:
#
#   * SERIAL. Two scopes cost 50.2 ms even when BOTH were empty, because the
#     floor is process spawn and the loop paid it twice in sequence.
#   * UNBOUNDED. A workspace scope holding 4,051,312 words took 4.2 SECONDS per
#     search, on every turn, on a message that was just "hello". Search time was
#     flat across queries — "a" and a six-word question cost the same — which is
#     a scan, not a lookup. kb-go has no index for a scope that size.
#
# So the turn inherited a multi-second stall from a subsystem it only wanted a
# few hundred characters from. The timeout is a BACKSTOP for that defect, not a
# fix: it converts "the chat hangs" into "this turn has no KB block", which is
# the same outcome the code already produces for a scope with no hits.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted (``scripts/mutate.py``).

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud.chat import agent_service as A

pytestmark = pytest.mark.asyncio


def _ctx() -> A.ScopeContext:
    return A.ScopeContext(
        kind=A.ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="ws1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


class TestTheBlockIsBounded:
    async def test_a_slow_scope_is_dropped_not_waited_for(self) -> None:
        """The 4.2-second scope, in test form.

        THE MUTATION THAT BREAKS THIS: remove the ``asyncio.wait_for`` wrapper
        in ``_build_kb_snippets_block``. Run: the call waited the full 5s and
        the elapsed assertion failed. (Applied 2026-08-04.)
        """

        async def _glacial(scope, query, limit=3):
            await asyncio.sleep(5)
            return "never arrives"

        with (
            patch.object(A, "_kb_scopes_for_context", return_value=["workspace:ws1"]),
            patch(
                "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
                new=_glacial,
            ),
        ):
            t0 = time.perf_counter()
            block = await A._build_kb_snippets_block(_ctx(), "hello")
            elapsed = time.perf_counter() - t0

        assert block == "", "a scope that never answered still contributed to the prompt"
        assert elapsed < A._KB_SEARCH_TIMEOUT_SECONDS + 1.0, (
            f"the turn waited {elapsed:.1f}s on a slow KB scope"
        )

    async def test_one_slow_scope_does_not_sink_a_healthy_one(self) -> None:
        """Degrade per scope, not per block.

        A shared workspace scope being unindexed must not cost the member their
        own fast scope's hits. This is only true because the searches run
        concurrently AND the timeout is applied per scope.

        THE MUTATION THAT BREAKS THIS: move the ``wait_for`` outside the gather
        so it bounds the whole block. Run: the healthy scope's snippet was lost
        along with the slow one and the ``fast hit`` assertion failed.
        """

        async def _mixed(scope, query, limit=3):
            if scope.startswith("workspace:"):
                await asyncio.sleep(5)
                return "never arrives"
            return "fast hit"

        with (
            patch.object(A, "_kb_scopes_for_context", return_value=["user:u1", "workspace:ws1"]),
            patch(
                "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
                new=_mixed,
            ),
        ):
            block = await A._build_kb_snippets_block(_ctx(), "hello")

        assert "fast hit" in block
        assert "never arrives" not in block

    async def test_the_timeout_is_a_backstop_not_a_budget(self) -> None:
        """Guards the constant against being tuned down into a real limit.

        A healthy scope answers in ~25 ms (process spawn). If this ever drops
        near that, the cap stops being a backstop and starts silently trimming
        working scopes.

        THE MUTATION THAT BREAKS THIS: set ``_KB_SEARCH_TIMEOUT_SECONDS = 0.05``.
        Run: below the 10x floor and this failed. (Applied 2026-08-04.)
        """
        assert A._KB_SEARCH_TIMEOUT_SECONDS >= 0.25, (
            "a healthy scope costs ~25ms; a cap this tight would drop working scopes"
        )


class TestTheScopesRunConcurrently:
    async def test_n_scopes_cost_the_slowest_not_the_sum(self) -> None:
        """The serial loop this replaced.

        Four scopes at 200 ms each: serial is 800 ms, concurrent is ~200 ms.
        The margin is wide enough that this does not flake on a loaded machine.

        THE MUTATION THAT BREAKS THIS: restore the ``for scope in scopes:``
        serial await. Run: 800 ms elapsed and the assertion failed.
        (Applied 2026-08-04.)
        """
        scopes = [f"pocket:p{i}" for i in range(4)]

        async def _slowish(scope, query, limit=3):
            await asyncio.sleep(0.2)
            return f"hit for {scope}"

        with (
            patch.object(A, "_kb_scopes_for_context", return_value=scopes),
            patch(
                "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
                new=_slowish,
            ),
        ):
            t0 = time.perf_counter()
            block = await A._build_kb_snippets_block(_ctx(), "hello")
            elapsed = time.perf_counter() - t0

        assert elapsed < 0.6, f"{len(scopes)} scopes took {elapsed:.2f}s — still serial?"
        for scope in scopes:
            assert scope in block

    async def test_scope_order_survives_the_gather(self) -> None:
        """The rendered block must be byte-identical to the serial version.

        ``gather`` preserves input order, and the block is keyed by scope
        headings, so a reordering would move prompt bytes and invalidate every
        backend cache keyed on them for no reason.

        THE MUTATION THAT BREAKS THIS: collect results with
        ``asyncio.as_completed`` instead of ``gather``. Run: the ordering
        assertion failed intermittently — which is exactly why it is asserted
        rather than assumed. (Applied 2026-08-04.)
        """
        scopes = ["user:u1", "workspace:ws1", "pocket:p1"]

        async def _by_scope(scope, query, limit=3):
            # Reverse the natural completion order: the FIRST scope is slowest.
            await asyncio.sleep(0.05 * (len(scopes) - scopes.index(scope)))
            return f"hit {scope}"

        with (
            patch.object(A, "_kb_scopes_for_context", return_value=scopes),
            patch(
                "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
                new=_by_scope,
            ),
        ):
            block = await A._build_kb_snippets_block(_ctx(), "hello")

        positions = [block.index(f"### {s}") for s in scopes]
        assert positions == sorted(positions), f"scope order changed: {block}"

    async def test_a_raising_scope_still_drops_only_itself(self) -> None:
        """Pre-existing behaviour, preserved through the rewrite.

        THE MUTATION THAT BREAKS THIS: drop the broad ``except`` in ``_one``.
        Run: the exception propagated out of ``gather`` and the whole turn
        raised instead of degrading. (Applied 2026-08-04.)
        """

        async def _one_bad(scope, query, limit=3):
            if scope.startswith("workspace:"):
                raise RuntimeError("kb exploded")
            return "still here"

        with (
            patch.object(A, "_kb_scopes_for_context", return_value=["user:u1", "workspace:ws1"]),
            patch(
                "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
                new=_one_bad,
            ),
        ):
            block = await A._build_kb_snippets_block(_ctx(), "hello")

        assert "still here" in block
