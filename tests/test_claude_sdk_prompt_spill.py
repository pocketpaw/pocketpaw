# tests/test_claude_sdk_prompt_spill.py
# Created: 2026-08-03 (PA-7b, feat/prompt-assembler-channel) — the FILE half of
# the Windows spilled-prompt bug. The cache-key half lives in
# `test_prompt_backend_digest.py`, in the test named
# `..._spilled_windows_prompt_no_longer_collapses_into_one_cache_key`; both come
# from the same single line of code.
#
# THE BUG. Windows caps a command line at ~32,767 chars, so a system prompt over
# 24,000 is written to disk and passed to the CLI as `--system-prompt-file`.
# Until PA-7b it was written to ONE fixed path, `~/.pocketpaw/runtime/system_prompt.md`.
# `AgentPool` holds one backend instance per agent, so two concurrent
# large-prompt runs on one machine both wrote that file and whichever CLI
# subprocess started second read the other agent's prompt. Windows-only, so the
# cloud never saw it — the desktop app runs a pool.
#
# THE FIX is a hash of the content in the filename, which also closes the cache
# key (the path is what `_behavior_prefix` returns for a spilled prompt, and a
# constant path is a constant key). The tests here are about the file: two
# prompts get two files, the same prompt is not rewritten, and the directory is
# bounded — an unbounded pile of 24k+ files in the user's home would be a worse
# bug than the one being fixed.
#
# Every test redirects `Path.home()` at a tmp dir. A test that writes to the real
# `~/.pocketpaw` would pass and then pollute the machine it ran on.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pocketpaw.agents.claude_sdk import (
    _SPILLED_PROMPT_KEEP,
    _WINDOWS_PROMPT_SPILL_CHARS,
    _prune_spilled_prompts,
    _spill_prompt_to_file,
    _spilled_prompt_path,
)

pytestmark = pytest.mark.asyncio

_BIG = "You are Paw.\n\n## THE LAW\nNever fabricate.\n" + "x" * _WINDOWS_PROMPT_SPILL_CHARS


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Point `Path.home()` at a tmp dir for every test in this file."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _spill_dir(home: Path) -> Path:
    return home / ".pocketpaw" / "runtime" / "prompts"


async def test_two_concurrent_prompts_no_longer_land_on_one_file(_home):
    """The race, in the form the pool can actually produce.

    Two agents, two different large prompts, one machine. Under the fixed path
    the second write clobbered the first and the first agent's CLI — if it had
    not yet read the file — launched on the second agent's prompt. Here they are
    two files and each holds its own bytes.

    THE MUTATION THAT BREAKS THIS: make `_spilled_prompt_path` ignore its
    argument and return the pre-PA-7b `Path.home() / ".pocketpaw" / "runtime" /
    "system_prompt.md"`. Run: one path, and the first file's content assertion
    failed with the second prompt's bytes.
    """
    prompt_a = _BIG + "\n<agent>alpha</agent>"
    prompt_b = _BIG + "\n<agent>bravo</agent>"

    path_a = _spill_prompt_to_file(prompt_a)
    path_b = _spill_prompt_to_file(prompt_b)

    assert path_a != path_b
    assert path_a.read_text(encoding="utf-8") == prompt_a
    assert path_b.read_text(encoding="utf-8") == prompt_b


async def test_the_same_prompt_is_written_once_and_then_reused(_home):
    """The write is skipped when the file is already there.

    The name IS the content, so an existing file has the bytes we were about to
    write. Skipping is what keeps a repeated turn from rewriting 24k+ chars under
    a CLI that may be reading them, and it removes the common concurrent case
    from the race without relying on the write being atomic.

    The assertion is deliberately blunt: the file's bytes are replaced with a
    marker on disk, and the marker must SURVIVE the second call. That is direct
    evidence no write happened, and it is honest about the consequence — a
    content-addressed cache trusts the name, which is why the name carries 128
    bits and not 64.

    THE MUTATION THAT BREAKS THIS: delete the `if path.exists(): return path`
    guard in `_spill_prompt_to_file`. Run: the marker was overwritten with the
    prompt and the assertion failed.
    """
    first = _spill_prompt_to_file(_BIG)
    first.write_text("MARKER — nothing should overwrite this", encoding="utf-8")

    second = _spill_prompt_to_file(_BIG)

    assert second == first
    assert second.read_text(encoding="utf-8").startswith("MARKER")


async def test_the_spill_directory_stays_bounded(_home):
    """An unbounded pile of 24k+ files in the user's home is not an acceptable trade.

    The point of content-addressing is that the names differ, so a long-lived
    desktop session would otherwise leave one file per distinct prompt, forever.
    Spilling more than the bound and finding the directory still at the bound is
    the whole assertion.

    THE MUTATION THAT BREAKS THIS: remove the `_prune_spilled_prompts(...)` call
    from `_spill_prompt_to_file`. Run: 40 files, and the count assertion failed.
    """
    over = _SPILLED_PROMPT_KEEP + 8
    for i in range(over):
        _spill_prompt_to_file(f"{_BIG}\n<turn>{i}</turn>")

    files = list(_spill_dir(_home).iterdir())
    assert len(files) <= _SPILLED_PROMPT_KEEP, f"{len(files)} spilled prompts survived"


async def test_the_prompt_just_spilled_is_never_the_one_pruned(_home):
    """The file the CLI is about to read must outlive its own prune.

    Newest-by-mtime would USUALLY protect it, and "usually" is not a property to
    hand a launching subprocess: filesystem timestamp resolution is coarse enough
    that a burst of spills can tie, and a tie resolves to whatever order the
    directory listing came back in — which, for content-hash filenames, is
    arbitrary. So the file is excluded from the candidate list outright.

    The mtimes here are FORCED so the protected file is the oldest thing in the
    directory. That is the adversarial case, and it is the only way to test this
    deterministically: driving it through a natural burst of spills passes with
    the guard removed roughly as often as it fails, because the tie-break is luck.

    THE MUTATION THAT BREAKS THIS: drop the `candidate == protect` skip (and the
    reserved budget slot) from `_prune_spilled_prompts`. Run: the protected file
    was deleted and `exists()` failed.
    """
    paths = [_spill_prompt_to_file(f"{_BIG}\n<turn>{i}</turn>") for i in range(5)]
    doomed = paths[0]
    # Oldest by a wide margin: any mtime-ordered prune reaches it first.
    os.utime(doomed, (1_000_000, 1_000_000))

    _prune_spilled_prompts(_spill_dir(_home), keep=2, protect=doomed)

    assert doomed.exists(), "the prune deleted the prompt the CLI was about to read"
    assert len(list(_spill_dir(_home).iterdir())) == 2, "the protected file must fit in the bound"


async def test_build_options_actually_uses_the_content_addressed_path(monkeypatch, _home):
    """The helpers above are correct; this proves `_build_options` CALLS them.

    A spill helper that is right but unreached is the original bug intact. Drives
    the real option build with `os.name` forced to "nt" so the branch runs on any
    platform, and checks the built options carry the file dict pointing at the
    hashed path — not a string prompt, and not the old fixed name.

    THE MUTATION THAT BREAKS THIS: restore `_build_options`' inline
    `runtime_dir / "system_prompt.md"` write. Run: the path assertion failed —
    the built options pointed at the constant filename.
    """
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings

    # Force the Windows branch by patching the PREDICATE, never ``os.name``.
    # ``pathlib`` binds ``WindowsPath.__new__`` to a raising stub at import time
    # on POSIX, so a Linux process with ``os.name`` forced to "nt" sends every
    # ``Path(...)`` into that stub — this test died on CI and passed on Windows.
    monkeypatch.setattr(
        "pocketpaw.agents.claude_sdk._prompt_must_spill",
        lambda prompt: len(prompt) > _WINDOWS_PROMPT_SPILL_CHARS,
    )
    backend = ClaudeSDKBackend(get_settings())
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: [])

    built = await backend._build_options(
        "hello",
        system_prompt=_BIG,
        history=None,
        session_key=None,
        deny_mcp_tool_ids=frozenset(),
        allow_sdk_tools=frozenset(),
        allow_mcp_tool_ids=frozenset(),
        skill_names=frozenset(),
        stderr_sink=[],
    )

    spilled = built.options_kwargs["system_prompt"]
    assert isinstance(spilled, dict), "a prompt over the Windows limit was passed inline"
    assert spilled["type"] == "file"
    # ``final_prompt`` is the assembled prompt plus whatever ``_build_options``
    # splices in, so the name is derived from what actually landed on disk rather
    # than from ``_BIG``.
    written = Path(spilled["path"])
    assert written.parent == _spill_dir(_home)
    assert written == _spilled_prompt_path(written.read_text(encoding="utf-8"))
    assert written.name != "system_prompt.md", "the constant filename is back"


async def test_a_prompt_under_the_limit_is_still_passed_inline(monkeypatch, _home):
    """The spill is for oversized prompts only — nothing else changed shape.

    THE MUTATION THAT BREAKS THIS: change `_build_options`' threshold test to
    `>= 0`. Run: the small prompt came back as a file dict and the isinstance
    assertion failed.
    """
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings

    # Force the Windows branch by patching the PREDICATE, never ``os.name``.
    # ``pathlib`` binds ``WindowsPath.__new__`` to a raising stub at import time
    # on POSIX, so a Linux process with ``os.name`` forced to "nt" sends every
    # ``Path(...)`` into that stub — this test died on CI and passed on Windows.
    monkeypatch.setattr(
        "pocketpaw.agents.claude_sdk._prompt_must_spill",
        lambda prompt: len(prompt) > _WINDOWS_PROMPT_SPILL_CHARS,
    )
    backend = ClaudeSDKBackend(get_settings())
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: [])

    built = await backend._build_options(
        "hello",
        system_prompt="You are Paw.",
        history=None,
        session_key=None,
        deny_mcp_tool_ids=frozenset(),
        allow_sdk_tools=frozenset(),
        allow_mcp_tool_ids=frozenset(),
        skill_names=frozenset(),
        stderr_sink=[],
    )

    assert isinstance(built.options_kwargs["system_prompt"], str)
    assert not _spill_dir(_home).exists(), "a small prompt still touched the spill directory"
