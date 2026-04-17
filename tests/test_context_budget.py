"""
Tests for context window budget tracking in AgentContextBuilder.
Created: 2026-04-01 - Priority-based injection with per-block character caps.
"""

from __future__ import annotations

from pocketpaw.bootstrap.context_builder import (
    _DEFAULT_BUDGET_CHARS,
    _INJECTION_CAPS,
    AgentContextBuilder,
    _Priority,
)


class TestAssembleWithBudget:
    """Unit tests for _assemble_with_budget — the budget-aware block assembler."""

    def test_all_blocks_fit_within_budget(self):
        """Small blocks should all be included when they fit comfortably."""
        blocks = [
            ("identity", _Priority.CRITICAL, "I am PocketPaw."),
            ("memory_context", _Priority.HIGH, "User likes coffee."),
            ("channel_hints", _Priority.LOW, "Keep it short."),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=10_000)
        assert "I am PocketPaw." in result
        assert "User likes coffee." in result
        assert "Keep it short." in result

    def test_low_priority_dropped_first(self):
        """When budget is tight, LOW blocks should be dropped before CRITICAL ones."""
        critical_block = "X" * 800
        high_block = "Y" * 100
        low_block = "Z" * 200

        blocks = [
            ("identity", _Priority.CRITICAL, critical_block),
            ("memory_context", _Priority.HIGH, high_block),
            ("channel_hints", _Priority.LOW, low_block),
        ]
        # Budget fits critical + high but not low (800 + 100 + separators < 950 < 800+100+200)
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=950)
        assert critical_block in result
        assert high_block in result
        assert low_block not in result

    def test_critical_never_dropped(self):
        """Even with a tiny budget, CRITICAL blocks are truncated but still present."""
        critical_content = "A" * 500
        medium_content = "B" * 200

        blocks = [
            ("identity", _Priority.CRITICAL, critical_content),
            ("sender_block", _Priority.MEDIUM, medium_content),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=100)
        # Critical block should be truncated to 100 chars, but present
        assert len(result) <= 100
        assert result.startswith("A")
        # Medium block should be dropped entirely
        assert "B" not in result

    def test_per_block_caps_applied(self):
        """A block exceeding its per-block cap should be truncated with a marker."""
        memory_cap = _INJECTION_CAPS["memory_context"]
        assert memory_cap is not None  # sanity check

        oversized_content = "M" * (memory_cap + 5000)
        blocks = [
            ("memory_context", _Priority.HIGH, oversized_content),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=50_000)
        # Should be capped to memory_cap + truncation marker, not the full content
        assert len(result) <= memory_cap + len("\n[...truncated]") + 10
        assert "[...truncated]" in result

    def test_empty_blocks_skipped(self):
        """Empty or whitespace-only blocks should not consume any budget."""
        blocks = [
            ("identity", _Priority.CRITICAL, "Hello"),
            ("memory_context", _Priority.HIGH, ""),
            ("channel_hints", _Priority.LOW, "   "),
            ("sender_block", _Priority.MEDIUM, "World"),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=10_000)
        assert "Hello" in result
        assert "World" in result
        # Only two non-empty blocks joined by \n\n
        assert result == "Hello\n\nWorld"

    def test_default_budget_is_generous(self):
        """The default 32K budget should accommodate typical prompt assemblies."""
        assert _DEFAULT_BUDGET_CHARS == 32_000

        # A reasonable set of blocks totalling ~5K should all fit
        blocks = [
            ("identity", _Priority.CRITICAL, "X" * 2000),
            ("memory_context", _Priority.HIGH, "Y" * 1500),
            ("sender_block", _Priority.MEDIUM, "Z" * 500),
            ("channel_hints", _Priority.LOW, "W" * 300),
            ("skills_list", _Priority.MEDIUM, "S" * 700),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks)
        # All blocks should be present
        assert "X" * 2000 in result
        assert "Y" * 1500 in result
        assert "Z" * 500 in result
        assert "W" * 300 in result
        assert "S" * 700 in result

    def test_budget_chars_kwarg(self):
        """Caller should be able to pass a custom budget_chars value."""
        blocks = [
            ("identity", _Priority.CRITICAL, "A" * 100),
            ("memory_context", _Priority.HIGH, "B" * 100),
        ]
        # With budget=150, only the critical block fits (100 chars + need room for second)
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=150)
        assert "A" * 100 in result
        # 100 chars used, 50 remaining — not enough for 100 chars of B
        assert "B" * 100 not in result

    def test_priority_ordering_preserved(self):
        """Blocks should be assembled in priority order, not insertion order."""
        blocks = [
            ("channel_hints", _Priority.LOW, "low"),
            ("identity", _Priority.CRITICAL, "critical"),
            ("memory_context", _Priority.HIGH, "high"),
            ("sender_block", _Priority.MEDIUM, "medium"),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=10_000)
        # CRITICAL should come before HIGH, which should come before MEDIUM, then LOW
        crit_pos = result.index("critical")
        high_pos = result.index("high")
        med_pos = result.index("medium")
        low_pos = result.index("low")
        assert crit_pos < high_pos < med_pos < low_pos

    def test_uncapped_block_uses_remaining_budget(self):
        """A block with no cap (None) should use whatever budget remains."""
        # identity has no cap in _INJECTION_CAPS
        large_identity = "I" * 20_000
        blocks = [
            ("identity", _Priority.CRITICAL, large_identity),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=25_000)
        assert result == large_identity

    def test_multiple_blocks_same_priority(self):
        """Multiple blocks at the same priority should all be included if budget allows."""
        blocks = [
            ("sender_block", _Priority.MEDIUM, "sender"),
            ("session_key", _Priority.MEDIUM, "session"),
            ("file_context", _Priority.MEDIUM, "files"),
        ]
        result = AgentContextBuilder._assemble_with_budget(blocks, budget_chars=10_000)
        assert "sender" in result
        assert "session" in result
        assert "files" in result


class TestKbContext:
    """Unit tests for kb (knowledge base) context injection via the kb-go CLI."""

    async def test_empty_query_returns_empty(self):
        """No user query means nothing to search, so kb injection is skipped."""
        result = await AgentContextBuilder._get_kb_context(None)
        assert result == ""

        result = await AgentContextBuilder._get_kb_context("")
        assert result == ""

    async def test_empty_scope_returns_empty(self, monkeypatch):
        """If kb_scope is not configured, kb injection is a no-op."""
        from unittest.mock import MagicMock

        import pocketpaw.bootstrap.context_builder as ctx_mod

        settings = MagicMock()
        settings.kb_scope = ""
        settings.kb_binary = "kb"
        settings.kb_limit = 3
        monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)

        result = await ctx_mod.AgentContextBuilder._get_kb_context("authentication")
        assert result == ""

    async def test_missing_binary_returns_empty(self, monkeypatch):
        """If the kb binary isn't found, failure is silent — empty string returned."""
        from unittest.mock import MagicMock

        import pocketpaw.bootstrap.context_builder as ctx_mod

        settings = MagicMock()
        settings.kb_scope = "test-scope"
        settings.kb_binary = "/nonexistent/kb-binary-that-does-not-exist"
        settings.kb_limit = 3
        monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)

        result = await ctx_mod.AgentContextBuilder._get_kb_context("authentication")
        assert result == ""

    async def test_successful_kb_fetch(self, monkeypatch):
        """When kb returns output, the stdout text is injected verbatim."""
        from unittest.mock import AsyncMock, MagicMock

        import pocketpaw.bootstrap.context_builder as ctx_mod

        settings = MagicMock()
        settings.kb_scope = "test-scope"
        settings.kb_binary = "kb"
        settings.kb_limit = 3
        monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)

        # Fake the subprocess
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(
            return_value=(b"## Article 1\nauth module details\n", b"")
        )

        async def fake_create_subprocess_exec(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

        result = await ctx_mod.AgentContextBuilder._get_kb_context("auth")
        assert "auth module details" in result

    def test_kb_context_has_injection_cap(self):
        """kb_context should have a reasonable cap to avoid blowing the context window."""
        cap = _INJECTION_CAPS.get("kb_context")
        assert cap is not None
        assert 1000 <= cap <= 5000  # sanity range
