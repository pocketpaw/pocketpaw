"""
Tests for context window budget tracking in AgentContextBuilder.
Created: 2026-04-01 - Priority-based injection with per-block character caps.
Updated: 2026-08-03 (PA-7a, feat/prompt-assembler-channel) - the subject moved.
``_assemble_with_budget``, ``_Priority`` and ``_INJECTION_CAPS`` are deleted;
the channel path assembles through ``pocketpaw.prompt.assemble`` over the
fifteen ``channel.*`` layers, each carrying its own ``max_chars`` and
``Priority``. The budget MECHANICS are therefore tested once, generically, in
``tests/test_prompt_budget.py``. What is left here is the thing only this file
can check: that every cap and every priority came across the cutover with the
number it had, and that the two blocks the old table had NO entry for are still
uncapped rather than quietly given one.

The behavioural tests below drive the REQUEST layers only
(``tests/test_channel_prompt_goldens.py`` covers the full fifteen against real
bytes). Request layers read nothing but their ``ChannelInputs``, so these stay
hermetic without stubbing the machine.
"""

from __future__ import annotations

from pocketpaw.bootstrap.context_builder import (
    _DEFAULT_BUDGET_CHARS,
    AgentContextBuilder,
)
from pocketpaw.prompt import Priority, PromptContext, assemble, prompt_layer_registry
from pocketpaw.prompt.channel import CHANNEL_PROMPT_LAYERS, ChannelInputs

# The table as ``_INJECTION_CAPS`` held it the moment before PA-7a deleted it,
# transcribed here so the assertion below compares the layers against the OLD
# numbers rather than against themselves. ``None`` means uncapped.
#
# Two things in it are not what a reader would guess, and both are inherited
# rather than chosen:
#   * ``pocket_context`` and ``current_pocket`` were MISSING from the table, so
#     ``_INJECTION_CAPS.get(name)`` returned ``None`` and they went into the
#     prompt at whatever length the caller produced. ``current_pocket``
#     ``json.dumps`` a widget summary in with no bound.
#   * the table also capped ``instructions``, a block this path never appended.
#     There is no ``channel.instructions`` layer and the entry has no successor.
# PA-9 owns cap arithmetic; PA-7a's job was to move the numbers, not judge them.
_OLD_INJECTION_CAPS: dict[str, int | None] = {
    "channel.identity": None,
    "channel.memory_context": 4000,
    "channel.kb_context": 3000,
    "channel.sender_block": 500,
    "channel.channel_hints": 500,
    "channel.channel_instructions": 1000,
    "channel.session_key": 200,
    "channel.file_context": 2000,
    "channel.health_state": 300,
    "channel.skills_list": 2000,
    "channel.atlas_primer": 2000,
    "channel.agents_md": 3000,
    "channel.gws_instructions": 1000,
    "channel.pocket_context": None,
    "channel.current_pocket": None,
}

# The priority each block was appended with, same source, same moment.
_OLD_PRIORITIES: dict[str, Priority] = {
    "channel.identity": Priority.CRITICAL,
    "channel.memory_context": Priority.HIGH,
    "channel.kb_context": Priority.HIGH,
    "channel.sender_block": Priority.HIGH,
    "channel.pocket_context": Priority.HIGH,
    "channel.current_pocket": Priority.HIGH,
    "channel.channel_instructions": Priority.MEDIUM,
    "channel.session_key": Priority.MEDIUM,
    "channel.file_context": Priority.MEDIUM,
    "channel.skills_list": Priority.MEDIUM,
    "channel.atlas_primer": Priority.MEDIUM,
    "channel.agents_md": Priority.MEDIUM,
    "channel.gws_instructions": Priority.MEDIUM,
    "channel.channel_hints": Priority.LOW,
    "channel.health_state": Priority.LOW,
}

def _ctx(**channel_inputs) -> PromptContext:
    return PromptContext(
        instance=None,
        agent_id="",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        channel_inputs=ChannelInputs(**channel_inputs),
    )


def _layers(*names):
    return [prompt_layer_registry.get(name) for name in names]


class TestChannelLayerBudget:
    """The caps, the priorities, and the budget behaviour they drive."""

    async def test_every_cap_came_across_with_the_number_it_had(self):
        """Each layer's ``max_chars`` is its old ``_INJECTION_CAPS`` entry.

        MUTATION: change any ``max_chars`` in ``prompt/channel/request.py`` or
        ``prompt/channel/environment.py``. The offending name is named.
        """
        actual = {name: prompt_layer_registry.get(name).max_chars for name in CHANNEL_PROMPT_LAYERS}
        assert actual == _OLD_INJECTION_CAPS

    async def test_the_two_uncapped_blocks_are_still_uncapped(self):
        """``pocket_context`` and ``current_pocket`` never had a cap entry.

        Called out separately from the table above because "``None`` because the
        old table said ``None``" and "``None`` because the old table had no row"
        are different facts, and only the second is a gap PA-9 should look at.

        MUTATION: give ``ChannelCurrentPocketLayer`` a ``max_chars``. Fails —
        and would also move bytes on any live pocket big enough to hit it.
        """
        assert prompt_layer_registry.get("channel.pocket_context").max_chars is None
        assert prompt_layer_registry.get("channel.current_pocket").max_chars is None

    async def test_the_dead_instructions_cap_has_no_successor(self):
        """``_INJECTION_CAPS['instructions']`` capped a block this path never built.

        MUTATION: register a ``channel.instructions`` layer. Fails, and it
        should — the cloud path's ``instructions`` layer is a different block
        with a different producer, and a channel twin would put the EE behaviour
        stack into channel prompts.
        """
        assert "channel.instructions" not in CHANNEL_PROMPT_LAYERS
        assert "channel.instructions" not in prompt_layer_registry.list()

    async def test_every_priority_came_across_with_the_rank_it_had(self):
        """Each layer's ``priority`` is the one its block was appended with.

        MUTATION: flip ``ChannelFormatHintLayer.priority`` to MEDIUM. Fails
        here, and the emission-order test in
        ``tests/test_channel_prompt_layers.py`` fails too — the two together are
        what make a priority change impossible to land by accident.
        """
        actual = {name: prompt_layer_registry.get(name).priority for name in CHANNEL_PROMPT_LAYERS}
        assert actual == _OLD_PRIORITIES

    async def test_all_blocks_fit_within_a_generous_budget(self):
        """Small blocks are all included when they fit comfortably."""
        assembled = await assemble(
            _layers("channel.identity", "channel.memory_context", "channel.session_key"),
            _ctx(identity="I am PocketPaw.", memory_context="User likes coffee.", session_key="s"),
            budget_chars=10_000,
        )
        assert "I am PocketPaw." in assembled.text
        assert "User likes coffee." in assembled.text
        assert "Current session_key: s" in assembled.text

    async def test_low_priority_is_dropped_before_critical(self):
        """When the budget is tight, LOW goes and CRITICAL stays."""
        from pocketpaw.bus.events import Channel

        assembled = await assemble(
            _layers("channel.identity", "channel.channel_hints"),
            _ctx(identity="X" * 800, channel=Channel.TELEGRAM),
            budget_chars=850,
        )
        assert "X" * 800 in assembled.text
        assert "# Response Format" not in assembled.text
        # Two entries, and they are two different cuts: the Telegram hint is
        # over its own 500-char cap FIRST (unconditional, before the budget is
        # consulted), and the capped 515 chars are then what the budget cannot
        # afford. ``dropped`` records both because neither is the other.
        assert [(d.name, d.reason.split()[0]) for d in assembled.dropped] == [
            ("channel.channel_hints", "truncated"),
            ("channel.channel_hints", "dropped"),
        ]

    async def test_a_block_over_its_cap_is_truncated_with_a_marker(self):
        """A block above ``max_chars`` is cut and says so, budget notwithstanding."""
        assembled = await assemble(
            _layers("channel.memory_context"),
            _ctx(memory_context="M" * 9000),
            budget_chars=50_000,
        )
        assert "[...truncated]" in assembled.text
        assert len(assembled.text) == 4000 + len("\n[...truncated]")

    async def test_the_default_budget_is_still_generous(self):
        """The 32K default accommodates a typical assembly."""
        assert _DEFAULT_BUDGET_CHARS == 32_000
        assembled = await assemble(
            _layers("channel.identity", "channel.memory_context"),
            _ctx(identity="X" * 2000, memory_context="Y" * 1500),
        )
        assert "X" * 2000 in assembled.text
        assert "Y" * 1500 in assembled.text

    async def test_an_uncapped_block_is_never_truncated(self):
        """``channel.identity`` has no cap, so 20k chars arrive whole."""
        big = "I" * 20_000
        assembled = await assemble(
            _layers("channel.identity"), _ctx(identity=big), budget_chars=25_000
        )
        assert assembled.text == big

    async def test_blank_blocks_leave_no_separator(self):
        """A layer with nothing to say contributes no text AND no ``\\n\\n``.

        MUTATION: drop ``_nonblank`` from ``ChannelIdentityLayer.render``. A
        whitespace-only identity starts contributing a separator and the
        assertion below fails.
        """
        assembled = await assemble(
            _layers("channel.identity", "channel.session_key", "channel.file_context"),
            _ctx(identity="   ", session_key="s-1", file_context=None),
            budget_chars=10_000,
        )
        assert assembled.text.startswith("\n# Session Management")
        assert "\n\n" not in assembled.text.strip()


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
        cap = prompt_layer_registry.get("channel.kb_context").max_chars
        assert cap is not None
        assert 1000 <= cap <= 5000  # sanity range
