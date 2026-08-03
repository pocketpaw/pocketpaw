# tests/test_channel_prompt_goldens.py
# Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel) — the safety net
# for moving the CHANNEL path (Telegram / Discord / Slack / CLI) off
# ``AgentContextBuilder._assemble_with_budget`` and onto
# ``pocketpaw.prompt.assemble``.
#
# WHY BYTES AND NOT BEHAVIOUR. PA-7a's whole acceptance is that a channel turn
# assembles the SAME prompt after the cutover as before it. A behavioural test
# ("the skills block is present") passes just as happily when a block moved from
# position 7 to position 11, or lost its leading newline, or picked up a second
# ``\n\n`` because an empty block stopped being skipped. Every one of those
# changes the prompt the model reads and every one of them is invisible to an
# ``in`` assertion. So these tests pin the exact bytes, for a fixed input, on
# five channel shapes, and they are committed BEFORE a line of the cutover is
# written.
#
# WHAT IS STUBBED AND WHY. Everything the builder reaches for that is not the
# assembly under test: the bootstrap provider, the memory manager, the kb
# subprocess, the health engine, the skill loader, the atlas store, the
# AGENTS.md loader, and the MCP config that gates the GWS block. They are
# stubbed AT THE RESOURCE, not at the builder's own private helpers, because
# the cutover moves several of those helpers into layer modules — a stub aimed
# at ``AgentContextBuilder._build_atlas_primer`` would have to move with it, and
# a golden that needs editing to survive a refactor is not a golden. The two
# real files the builder reads (``bootstrap/discord.md``, ``bootstrap/gws.md``)
# are deliberately NOT stubbed: their contents are part of the shipped prompt,
# and the goldens are the thing that notices if the cutover stops finding them.
#
# REGENERATING. Set ``PAW_WRITE_CHANNEL_GOLDENS=1`` and run this file; every
# golden is rewritten from the current implementation. That is a loud, explicit,
# opt-in action on purpose. A regenerated golden must land in its OWN commit
# with the reason in the message — "the test failed" is not a reason.

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.bootstrap.protocol import BootstrapContext
from pocketpaw.bus.events import Channel

pytestmark = pytest.mark.asyncio

_GOLDEN_DIR = Path(__file__).parent / "data" / "channel_prompt_goldens"
_WRITE_GOLDENS = os.environ.get("PAW_WRITE_CHANNEL_GOLDENS") == "1"


# ---------------------------------------------------------------------------
# The fixed world every golden renders against
# ---------------------------------------------------------------------------


class _StubBootstrap:
    """A bootstrap provider with no I/O and no clock in it."""

    async def get_context(self) -> BootstrapContext:
        return BootstrapContext(
            name="Goldie",
            identity="You are Goldie, a fixture.",
            soul="Determinism above all.",
            style="Terse.",
            instructions="Use the tools you are given.",
            knowledge=["The sky is up.", "kb-go is a subprocess."],
            user_profile="Prefers short answers.",
        )


class _StubSkill:
    def __init__(self, name: str, description: str, user_invocable: bool) -> None:
        self.name = name
        self.description = description
        self.user_invocable = user_invocable


class _StubSkillLoader:
    paths = (Path("/fixture/skills"), Path("/fixture/more-skills"))

    def get_all(self) -> dict[str, _StubSkill]:
        return {
            "commit": _StubSkill("commit", "Stage and commit work.", True),
            "humanize": _StubSkill("humanize", "Strip AI tells from prose.", False),
        }


class _StubHealthEngine:
    def get_health_prompt_section(self) -> str:
        return "\n# System Health\nStatus: degraded (memory backend slow)."


class _StubAgentsMd:
    constraints_block = "\n# Project Constraints (AGENTS.md)\n- Never push to main.\n- Use uv."


class _StubAgentsMdLoader:
    def find_and_load(self, directory: str):  # noqa: ANN201 - test stub
        return _StubAgentsMd()


class _StubAtlasEntry:
    def __init__(self, name: str, gist: str) -> None:
        self.name = name
        self.kind = "primitive"
        self.gist = gist
        self.summary = gist


class _StubAtlasStore:
    entries = (
        _StubAtlasEntry("Pocket", "a workspace app you build and share"),
        _StubAtlasEntry("Site", "a published page rendered to the edge"),
        _StubAtlasEntry("Soul", "portable identity with 5-tier memory"),
    )


class _StubMcpConfig:
    def __init__(self, name: str, enabled: bool) -> None:
        self.name = name
        self.enabled = enabled


@pytest.fixture(autouse=True)
def _stub_world(monkeypatch):
    """Pin every input the builder reaches for outside the assembly itself.

    Stubs land on the RESOURCE (``pocketpaw.atlas.store.get_atlas_store``,
    ``pocketpaw.skills.get_skill_loader``, ...) rather than on the builder's
    private helpers, so the cutover can move a helper into a layer module
    without touching a single line of this file.
    """
    settings = MagicMock()
    settings.owner_id = "owner-9"
    settings.kb_scopes = []
    settings.kb_binary = "kb"
    settings.kb_limit = 3
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)

    async def _fake_kb(*args, **kwargs) -> str:
        return "### From workspace:w1\n- routing.md\n  How the bus routes inbound messages."

    monkeypatch.setattr(AgentContextBuilder, "_get_kb_context", staticmethod(_fake_kb))
    monkeypatch.setattr("pocketpaw.health.get_health_engine", lambda: _StubHealthEngine())
    monkeypatch.setattr("pocketpaw.skills.get_skill_loader", lambda: _StubSkillLoader())
    monkeypatch.setattr("pocketpaw.atlas.store.get_atlas_store", lambda: _StubAtlasStore())
    monkeypatch.setattr("pocketpaw.agents_md.AgentsMdLoader", _StubAgentsMdLoader)
    monkeypatch.setattr(
        "pocketpaw.mcp.config.load_mcp_config",
        lambda: [_StubMcpConfig("google-workspace", True)],
    )
    return settings


def _builder() -> AgentContextBuilder:
    memory = MagicMock()
    memory.get_semantic_context = AsyncMock(return_value="The user ships on Fridays.")
    memory.get_context_for_agent = AsyncMock(return_value="The user ships on Fridays.")
    return AgentContextBuilder(bootstrap_provider=_StubBootstrap(), memory_manager=memory)


_METADATA = {
    "username": "dizzy",
    "guild_id": "guild-77",
    "pocket_system_context": "\n# Pocket Creation\nBuild a pocket, not a website.",
    "pocket_context": {
        "id": "pk-123",
        "name": "Launch Tracker",
        "widgets": [{"name": "Burndown", "type": "chart"}],
    },
}

_FILE_CONTEXT = {
    "current_dir": "/work/pocketpaw",
    "open_file": "/work/pocketpaw/src/pocketpaw/bus/loop.py",
    "selected_files": ["/work/pocketpaw/README.md", "/work/pocketpaw/pyproject.toml"],
}


async def _full_prompt(channel: Channel | None, **overrides) -> str:
    """The fully-populated call every golden is taken from."""
    kwargs = {
        "include_memory": True,
        "user_query": "how do I publish a site?",
        "channel": channel,
        "sender_id": "sender-1",
        "session_key": "sess-abc",
        "file_context": _FILE_CONTEXT,
        "agents_md_dir": "/work/pocketpaw",
        "metadata": _METADATA,
    }
    kwargs.update(overrides)
    return await _builder().build_system_prompt(**kwargs)


def _check_golden(name: str, actual: str) -> None:
    path = _GOLDEN_DIR / f"{name}.txt"
    if _WRITE_GOLDENS:
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="")
        pytest.skip(f"wrote golden {path}")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"channel prompt bytes moved against {path}. If the move is INTENDED, "
        f"regenerate with PAW_WRITE_CHANNEL_GOLDENS=1 in its own commit and put "
        f"the reason in the message."
    )


# ---------------------------------------------------------------------------
# The goldens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("golden", "channel"),
    [
        ("telegram", Channel.TELEGRAM),
        ("discord", Channel.DISCORD),
        ("slack", Channel.SLACK),
        ("cli", Channel.CLI),
        ("no_channel", None),
    ],
)
async def test_the_assembled_channel_prompt_is_byte_identical(golden, channel):
    """The exact bytes ``build_system_prompt`` emits for a fixed, full input.

    MUTATION: change ``_JOIN`` in ``prompt/assembler.py`` from ``"\\n\\n"`` to
    ``"\\n"``, or reorder any entry of the channel layer list, or drop the
    ``.strip()``-empty skip. Each moves at least one of these five files.
    """
    _check_golden(golden, await _full_prompt(channel))


async def test_the_full_input_actually_exercises_every_block():
    """A golden is only a safety net over the blocks it contains.

    Fifteen blocks are appended by ``build_system_prompt``; this pins that the
    fixture reaches all fifteen, so a cutover that silently stops emitting one
    cannot hide behind an input that never produced it.

    MUTATION: delete any ``blocks.append(...)`` in ``build_system_prompt`` (or,
    after the cutover, remove its layer from the channel layer list). One of
    these markers goes missing.
    """
    prompt = await _full_prompt(Channel.DISCORD)
    markers = {
        "identity": "<identity>",
        "memory_context": "# Memory Context (already loaded",
        "kb_context": "# Knowledge Base (relevant articles",
        "sender_block": "You are speaking with sender_id=sender-1",
        "channel_hints": "# Response Format",
        # Both file-backed blocks are capped BELOW the file's length, so the
        # marker has to live in the surviving head — the tail (discord.md's
        # appended "## Current Context", gws.md's own name) is cut away.
        "channel_instructions": "# Discord Behavior Layer",
        "pocket_context": "# Pocket Creation",
        "current_pocket": "<current-pocket>",
        "session_key": "# Session Management",
        "file_context": "# File Context",
        "health_state": "# System Health",
        "skills_list": "# Available Skills",
        "atlas_primer": "# Paw OS Primer",
        "agents_md": "# Project Constraints (AGENTS.md)",
        "gws_instructions": "# Google Workspace CLI",
    }
    missing = [name for name, marker in markers.items() if marker not in prompt]
    assert not missing, f"golden fixture never produced these blocks: {missing}"


async def test_a_prompt_with_the_optional_inputs_absent_is_byte_identical():
    """A block that does not apply must leave NO gap — not a doubled ``\\n\\n``.

    The old assembler skips a block whose content is empty or whitespace-only;
    the new one skips empty text at the join. Those agree only while every layer
    renders ``""`` rather than ``"   "`` for "nothing to say", which is exactly
    what this golden would catch.

    MUTATION: make any layer return whitespace instead of ``""`` when its input
    is absent, or drop the ``not content.strip()`` guard. This golden grows a
    separator.
    """
    prompt = await _builder().build_system_prompt(
        include_memory=False,
        user_query=None,
        channel=Channel.CLI,
        sender_id=None,
        session_key=None,
        file_context=None,
        agents_md_dir=None,
        metadata=None,
    )
    _check_golden("minimal", prompt)


async def test_a_block_over_its_cap_is_cut_to_the_cap_plus_the_marker():
    """``memory_context`` is capped at 4000, and the marker pushes it to 4015.

    The 15 extra chars are a shipped quirk of ``_assemble_with_budget``:
    ``content[:cap] + "\\n[...truncated]"`` overshoots the cap it is enforcing.
    ``prompt/assembler.py`` reproduces it deliberately. Pinning the EXACT
    resulting length is what makes this test notice a "fix" of that arithmetic,
    which would move every over-cap block on every channel turn.

    MUTATION: change ``_INJECTION_CAPS["memory_context"]`` to 3000 (or, after
    the cutover, the memory layer's ``max_chars``), or drop the ``+ marker``.
    The length assertion fails.
    """
    memory = MagicMock()
    memory.get_semantic_context = AsyncMock(return_value="M" * 5000)
    memory.get_context_for_agent = AsyncMock(return_value="M" * 5000)
    builder = AgentContextBuilder(bootstrap_provider=_StubBootstrap(), memory_manager=memory)
    prompt = await builder.build_system_prompt(
        include_memory=True,
        user_query="how do I publish a site?",
        channel=Channel.TELEGRAM,
        sender_id="sender-1",
    )

    marker = "\n[...truncated]"
    start = prompt.index("\n# Memory Context (already loaded")
    block = prompt[start : start + 4000 + len(marker)]
    assert block.endswith(marker)
    assert len(block) == 4015
    # And the block really does end there — what follows is the join, not more
    # of the memory text.
    assert prompt[start + 4015 : start + 4017] == "\n\n"


async def test_a_tight_budget_drops_the_low_and_medium_blocks():
    """A budget that fits CRITICAL + nothing else drops everything droppable.

    The budget is sized so the CRITICAL identity block fits WHOLE, deliberately:
    a budget that cuts CRITICAL is the one place PA-7a changes behaviour on
    purpose (see ``test_a_critical_block_over_budget_is_truncated_today``), and
    a golden must not straddle an intended divergence.

    MUTATION: make ``_assemble_with_budget`` ignore ``budget_chars``, or sort
    LOW before MEDIUM. The dropped blocks reappear.
    """
    identity_len = len((await _StubBootstrap().get_context()).to_system_prompt())
    prompt = await _full_prompt(Channel.TELEGRAM, budget_chars=identity_len + 20)

    assert "<identity>" in prompt
    for marker in (
        "# Memory Context (already loaded",  # HIGH
        "# Session Management",  # MEDIUM
        "# Available Skills",  # MEDIUM
        "# Paw OS Primer",  # MEDIUM
        "# Response Format",  # LOW
        "# System Health",  # LOW
    ):
        assert marker not in prompt, f"{marker!r} survived a budget that cannot hold it"


async def test_a_critical_block_over_budget_is_truncated_today():
    """TODAY a CRITICAL block over budget is CUT TO ``remaining``.

    This pins the CURRENT behaviour so the cutover's one intended divergence is
    visible as a deliberate edit to this file rather than as a silent drift.
    PA-7a replaces it with the opposite assertion — a CRITICAL layer is emitted
    WHOLE and the budget overruns — in its own commit, because a budget-sized
    cut is the one cut that breaks a cache key (``Priority``'s docstring).

    MUTATION: remove the ``if priority == _Priority.CRITICAL`` branch from
    ``_assemble_with_budget``. The identity block vanishes entirely and the
    length assertion fails.
    """
    identity = (await _StubBootstrap().get_context()).to_system_prompt()
    prompt = await _full_prompt(Channel.CLI, budget_chars=len(identity) - 100)

    assert prompt == identity[: len(identity) - 100]
    assert len(prompt) == len(identity) - 100
