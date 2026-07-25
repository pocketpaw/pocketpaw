# test_claude_sdk_nul_guard.py — a NUL never reaches the CLI spawn.
#
# Created 2026-07-25 (fix/claude-sdk-nul-arg-guard) after a live failure:
#
#   CLIConnectionError: Failed to start Claude Code: embedded null character
#     ... _winapi.CreateProcess(...)  ->  ValueError: embedded null character
#
# Windows `CreateProcess` refuses a command line, an environment block, or a cwd
# containing a NUL, and the SDK passes all three straight through. The result is a
# spawn that dies before the agent exists, with a message that names NOTHING: not
# the field, not the value, not even whether the offender was an arg or an env var.
# Every turn on that path then fails identically.
#
# A NUL is never legitimate in any of these — it cannot appear in a real prompt, a
# tool id, a model name, a path, or an env value. So the boundary strips it and says
# WHERE it was, which turns an opaque un-debuggable crash into a run that works plus
# a breadcrumb naming the source.
#
# These tests are about the guard only. They deliberately inject the NUL rather than
# reproduce a specific upstream source, because the point of the guard is to hold for
# a source nobody has identified yet.
from __future__ import annotations

from pocketpaw.agents.claude_sdk import _scrub_nul_chars


def test_a_nul_in_a_string_option_is_stripped_and_reported() -> None:
    kwargs = {"system_prompt": "You are helpful.\x00 Be brief."}

    offenders = _scrub_nul_chars(kwargs)

    assert kwargs["system_prompt"] == "You are helpful. Be brief."
    assert offenders == ["system_prompt"]


def test_a_nul_in_an_env_value_is_stripped_and_the_KEY_is_named() -> None:
    """The env block is the likeliest carrier and the hardest to trace, so the
    report has to name the variable, not just "env"."""
    kwargs = {"env": {"ANTHROPIC_API_KEY": "sk-real", "SOME_TOKEN": "abc\x00def"}}

    offenders = _scrub_nul_chars(kwargs)

    assert kwargs["env"] == {"ANTHROPIC_API_KEY": "sk-real", "SOME_TOKEN": "abcdef"}
    assert offenders == ["env['SOME_TOKEN']"]


def test_a_nul_in_cwd_is_stripped() -> None:
    kwargs = {"cwd": "C:\\Users\\x\\.pocketpaw\\workspaces\\w1\x00\\agent"}

    offenders = _scrub_nul_chars(kwargs)

    assert "\x00" not in kwargs["cwd"]
    assert offenders == ["cwd"]


def test_a_nul_inside_a_list_option_is_stripped_at_its_index() -> None:
    kwargs = {"allowed_tools": ["Read", "mcp__pocketpaw_code__readFile\x00"]}

    offenders = _scrub_nul_chars(kwargs)

    assert kwargs["allowed_tools"] == ["Read", "mcp__pocketpaw_code__readFile"]
    assert offenders == ["allowed_tools[1]"]


def test_nested_structures_are_reached() -> None:
    """`mcp_servers` and `plugins` are dicts of dicts / lists of dicts, and both
    end up serialized into a CLI argument."""
    kwargs = {
        "mcp_servers": {"pocketpaw_code": {"command": "node", "args": ["srv.js\x00"]}},
        "plugins": [{"type": "local", "path": "/tmp/skills\x00"}],
    }

    offenders = _scrub_nul_chars(kwargs)

    assert kwargs["mcp_servers"]["pocketpaw_code"]["args"] == ["srv.js"]
    assert kwargs["plugins"][0]["path"] == "/tmp/skills"
    assert sorted(offenders) == ["mcp_servers['pocketpaw_code']['args'][0]", "plugins[0]['path']"]


def test_a_clean_option_set_is_untouched_and_reports_nothing() -> None:
    """The guard runs on EVERY turn, so the no-NUL path must be a no-op — no
    rewriting, no copying, no log line."""
    kwargs = {
        "system_prompt": "You are helpful.",
        "allowed_tools": ["Read", "Write"],
        "env": {"ANTHROPIC_API_KEY": "sk-real"},
        "cwd": "/home/u/.pocketpaw",
        "max_turns": 40,
        "include_partial_messages": True,
    }
    before = {
        "system_prompt": kwargs["system_prompt"],
        "allowed_tools": list(kwargs["allowed_tools"]),
        "env": dict(kwargs["env"]),
        "cwd": kwargs["cwd"],
    }

    offenders = _scrub_nul_chars(kwargs)

    assert offenders == []
    assert kwargs["system_prompt"] == before["system_prompt"]
    assert kwargs["allowed_tools"] == before["allowed_tools"]
    assert kwargs["env"] == before["env"]
    assert kwargs["cwd"] == before["cwd"]


def test_non_string_values_survive_untouched() -> None:
    """Numbers, bools, None and opaque objects (hooks, session_store) pass through
    — the guard must not try to walk or coerce them."""
    sentinel = object()
    kwargs = {
        "max_turns": 40,
        "include_partial_messages": True,
        "model": None,
        "session_store": sentinel,
        "hooks": {"PreToolUse": [sentinel]},
    }

    offenders = _scrub_nul_chars(kwargs)

    assert offenders == []
    assert kwargs["max_turns"] == 40
    assert kwargs["include_partial_messages"] is True
    assert kwargs["model"] is None
    assert kwargs["session_store"] is sentinel
    assert kwargs["hooks"]["PreToolUse"][0] is sentinel


def test_every_nul_in_one_value_goes_not_just_the_first() -> None:
    kwargs = {"system_prompt": "a\x00b\x00c\x00"}

    offenders = _scrub_nul_chars(kwargs)

    assert kwargs["system_prompt"] == "abc"
    assert offenders == ["system_prompt"]


# ── the guard is actually wired into the real option build ──────────────────


async def test_build_options_strips_a_nul_before_it_can_reach_the_spawn(monkeypatch) -> None:
    """The unit tests above prove the helper; this proves it RUNS. A guard that is
    correct but never called is the whole bug over again."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings

    backend = ClaudeSDKBackend(get_settings())
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: [])

    built = await backend._build_options(
        "hello",
        system_prompt="You are helpful.\x00 Be brief.",
        history=None,
        session_key=None,
        deny_mcp_tool_ids=frozenset(),
        allow_sdk_tools=frozenset(),
        allow_mcp_tool_ids=frozenset(),
        skill_names=frozenset(),
        stderr_sink=[],
    )

    def has_nul(value) -> bool:
        if isinstance(value, str):
            return "\0" in value
        if isinstance(value, dict):
            return any(has_nul(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_nul(v) for v in value)
        return False

    assert not has_nul(built.options_kwargs), "a NUL survived into the built options"
    assert "\x00" not in built.options_kwargs["system_prompt"]
