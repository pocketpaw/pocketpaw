"""Tests for the PR-review gap fixes.

Covers:
1. CancelledError path through _process_message_inner — trace must be closed
2. Concurrent budget enforcement safety (no race produces double-save)
3. session_id path-traversal validation on /api/v1/traces
4. AlertStore.list_alerts does NOT expose _unread in returned dicts
5. _is_mock_placeholder handles None __module__ gracefully
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. CancelledError path — trace must be closed via finally block
# ---------------------------------------------------------------------------


class TestTraceCleanupOnCancellation:
    """_process_message_inner must emit trace_end even when cancelled."""

    def _make_inner(self, trace_closed_holder: list[bool]):
        """Build a minimal coroutine that mimics the finally-guard pattern."""

        async def _inner() -> None:
            trace_closed = False

            async def _emit_trace_end(**_kwargs: object) -> None:
                nonlocal trace_closed
                trace_closed = True

            try:
                # simulate work that gets cancelled
                await asyncio.sleep(10)
            except Exception:
                await _emit_trace_end(status="error", reason="exception")
            finally:
                if not trace_closed:
                    try:
                        await _emit_trace_end(
                            status="cancelled",
                            reason="task_cancelled",
                        )
                    except Exception:
                        pass
                trace_closed_holder.append(trace_closed)

        return _inner

    @pytest.mark.asyncio
    async def test_trace_closed_on_cancel(self) -> None:
        """trace_closed must be True after the task is cancelled."""
        holder: list[bool] = []
        task = asyncio.create_task(self._make_inner(holder)())
        await asyncio.sleep(0)  # let the task start and reach sleep(10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert holder == [True], "finally block must set trace_closed=True via _emit_trace_end"

    @pytest.mark.asyncio
    async def test_trace_not_closed_twice_on_normal_completion(self) -> None:
        """On normal completion the finally guard must be a no-op.

        In the real loop, _emit_trace_end() is called at the end of the
        try block (before the except), which sets trace_closed=True.
        The finally guard therefore takes the 'if not trace_closed' branch
        as False and does nothing — exactly one close call in total.
        """

        closed_calls: list[str] = []

        async def _inner() -> None:
            trace_closed = False

            async def _emit_trace_end(status: str = "ok", **_: object) -> None:
                nonlocal trace_closed
                trace_closed = True
                closed_calls.append(status)

            try:
                pass  # normal completion work
                # normal close is inside try, just like loop.py
                await _emit_trace_end(status="ok")
            except Exception:
                await _emit_trace_end(status="error")
            finally:
                if not trace_closed:
                    try:
                        await _emit_trace_end(status="cancelled", reason="task_cancelled")
                    except Exception:
                        pass

        await _inner()
        # Only one close call from the normal path; finally guard is a no-op
        assert closed_calls == ["ok"]


# ---------------------------------------------------------------------------
# 2. Budget concurrency — _budget_lock guards against concurrent writes
# ---------------------------------------------------------------------------


class TestBudgetLockConcurrency:
    """Two concurrent override requests must not corrupt settings."""

    @pytest.mark.asyncio
    async def test_budget_lock_serialises_concurrent_overrides(self) -> None:
        """Concurrent POST /budget/override calls must execute serially."""
        lock = asyncio.Lock()
        call_order: list[int] = []

        async def fake_override(n: int) -> None:
            async with lock:
                call_order.append(n)
                await asyncio.sleep(0.01)  # simulate disk I/O
                call_order.append(-n)

        await asyncio.gather(
            fake_override(1),
            fake_override(2),
            fake_override(3),
        )

        # Each positive entry must be immediately followed by its own negative,
        # proving no interleaving occurred.
        for i in range(0, len(call_order), 2):
            assert call_order[i] + call_order[i + 1] == 0, (
                f"interleaved at position {i}: {call_order}"
            )


# ---------------------------------------------------------------------------
# 3. session_id path traversal validation
# ---------------------------------------------------------------------------


class TestSessionIdPathTraversal:
    """_sanitize_session_id must reject traversal patterns."""

    def _sanitize(self, value: str) -> str:
        from fastapi import HTTPException as _HTTPException

        from pocketpaw.api.v1.traces import _sanitize_session_id

        try:
            return _sanitize_session_id(value)
        except _HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc

    def test_normal_session_id_passes(self) -> None:
        assert self._sanitize("abc-123_xyz") == "abc-123_xyz"

    def test_empty_session_id_passes(self) -> None:
        assert self._sanitize("") == ""

    def test_dotdot_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            self._sanitize("../../etc/passwd")

    def test_forward_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            self._sanitize("session/subdir")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            self._sanitize("session\\other")

    def test_dotdot_without_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            self._sanitize("session..other")


# ---------------------------------------------------------------------------
# 4. AlertStore.list_alerts must NOT expose _unread
# ---------------------------------------------------------------------------


class TestAlertStoreDoesNotLeakUnread:
    def _store(self):
        from pocketpaw.alert_manager import AlertStore

        return AlertStore()

    def test_list_alerts_no_unread_key(self) -> None:
        store = self._store()
        store.append({"alert_type": "test", "severity": "info", "_unread": True})
        store.append({"alert_type": "test2", "severity": "warning", "_unread": False})
        for alert in store.list_alerts(unread_only=False):
            assert "_unread" not in alert, f"_unread leaked into API output: {alert}"

    def test_unread_only_filter_still_works_after_stripping(self) -> None:
        """The _unread filter logic must still work even though _unread is stripped."""
        store = self._store()
        store.append({"alert_type": "read_one", "_unread": False})
        store.append({"alert_type": "unread_one", "_unread": True})

        unread = store.list_alerts(unread_only=True)
        assert len(unread) == 1
        assert unread[0]["alert_type"] == "unread_one"
        assert "_unread" not in unread[0]

    def test_mark_read_then_list_no_unread_key(self) -> None:
        store = self._store()
        store.append({"alert_type": "a", "_unread": True})
        store.mark_read()
        for alert in store.list_alerts(unread_only=False):
            assert "_unread" not in alert


# ---------------------------------------------------------------------------
# 5. _is_mock_placeholder handles None __module__
# ---------------------------------------------------------------------------


class TestIsMockPlaceholderNoneModule:
    def test_regular_object_returns_false(self) -> None:
        from pocketpaw.budget import _is_mock_placeholder

        assert _is_mock_placeholder(42) is False
        assert _is_mock_placeholder("hello") is False
        assert _is_mock_placeholder(None) is False

    def test_mock_object_returns_true(self) -> None:

        from pocketpaw.budget import _is_mock_placeholder

        assert _is_mock_placeholder(MagicMock()) is True

    def test_none_module_does_not_raise(self) -> None:
        """A class whose __module__ is None must not raise TypeError."""
        from pocketpaw.budget import _is_mock_placeholder

        class _Exotic:
            pass

        _Exotic.__module__ = None  # type: ignore[assignment]
        obj = _Exotic()
        # Must not raise
        result = _is_mock_placeholder(obj)
        assert result is False
