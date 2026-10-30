# tests/test_claude_sdk_prompt_spill_posix.py
# Created: 2026-09-15 (fix/prompt-spill-posix) — the spill is not a Windows
# quirk, and believing it was took the deployed cloud down.
#
# THE BUG, as reported from production:
#
#   API Error: Failed to start Claude Code: [Errno 7] Argument list too long:
#   '/opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude'
#
# Errno 7 is E2BIG from execve. The SDK passes ``system_prompt`` INLINE as
# ``--system-prompt <whole prompt>``, and Linux caps a SINGLE argv entry at
# ``MAX_ARG_STRLEN`` = 32 pages = 131,072 bytes. That is NOT the familiar
# ``ARG_MAX`` (~2 MB, the whole vector) — the per-argument ceiling is 16x
# smaller and far less known, which is how a prompt well under ARG_MAX still
# fails to exec.
#
# ``_prompt_must_spill`` read ``os.name == "nt" and len(prompt) > 24_000``, so
# on POSIX it returned False no matter how long the prompt was. The spill that
# exists precisely to avoid this was never armed where the product runs. The
# file header of the sibling test says "Windows-only, so the cloud never saw
# it" — the cloud is where it bites, through a different limit.
#
# WHAT MAKES IT REACHABLE: ``_ATTACHMENT_TOTAL_CHARS`` is 100,000 (raised from
# 30,000 on 2026-09-08 so a pasted brief stops being truncated). One big upload
# plus identity, instructions, KB and history clears 128 KiB comfortably.
#
# BYTES, NOT CHARACTERS, and this is the half a reviewer skips. The kernel
# counts bytes; ``len()`` counts code points. These prompts are full of em
# dashes and box drawing, 3 bytes each in UTF-8, so a prompt measured as
# "40,000 chars, well under the limit" can be 120,000 bytes and fail to exec.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted.

from __future__ import annotations

import pytest

from pocketpaw.agents.claude_sdk import (
    _POSIX_PROMPT_SPILL_BYTES,
    _WINDOWS_PROMPT_SPILL_BYTES,
    _prompt_must_spill,
    _prompt_spill_threshold,
)


def test_a_huge_prompt_spills_on_posix() -> None:
    """The reported outage in one line.

    THE MUTATION THAT BREAKS THIS: restore ``os.name == "nt" and ...`` in
    ``_prompt_must_spill``. Run: returns False on posix and this fails.
    """
    assert _prompt_must_spill("x" * 200_000, os_name="posix") is True


def test_an_ordinary_prompt_does_not_spill_on_posix() -> None:
    """Spilling every prompt would be its own bug: a file write and a read on
    the hot path of every turn, plus a cache key that moves per turn.

    THE MUTATION THAT BREAKS THIS: ``return True`` in ``_prompt_must_spill``.
    """
    assert _prompt_must_spill("you are a helpful agent", os_name="posix") is False


def test_the_threshold_is_measured_in_bytes_not_characters() -> None:
    """40,000 em dashes is 40,000 characters and 120,000 BYTES.

    Under a character-counting threshold this prompt looks less than half the
    limit and is passed inline, and execve then refuses it. This is the test
    that fails if someone 'simplifies' the encode away.

    THE MUTATION THAT BREAKS THIS: measure ``len(prompt)`` instead of
    ``len(prompt.encode("utf-8"))``. Run: 40,000 < 96,000 so it reports no
    spill, and this fails.
    """
    prompt = "—" * 40_000  # em dash: 3 bytes each in UTF-8
    assert len(prompt) < _POSIX_PROMPT_SPILL_BYTES
    assert len(prompt.encode("utf-8")) > _POSIX_PROMPT_SPILL_BYTES
    assert _prompt_must_spill(prompt, os_name="posix") is True


def test_windows_keeps_its_own_tighter_threshold() -> None:
    """Windows caps the WHOLE command line at ~32,767 chars, which is a
    different and tighter limit than the POSIX per-argument one. Collapsing the
    two to a single number would regress Windows.

    THE MUTATION THAT BREAKS THIS: return ``_POSIX_PROMPT_SPILL_BYTES`` for
    every platform. Run: a 30,000-byte prompt stops spilling on nt and this
    fails.
    """
    assert _WINDOWS_PROMPT_SPILL_BYTES < _POSIX_PROMPT_SPILL_BYTES
    assert _prompt_spill_threshold("nt") == _WINDOWS_PROMPT_SPILL_BYTES
    assert _prompt_spill_threshold("posix") == _POSIX_PROMPT_SPILL_BYTES
    assert _prompt_must_spill("x" * 30_000, os_name="nt") is True


def test_the_posix_threshold_leaves_headroom_under_the_kernel_limit() -> None:
    """MAX_ARG_STRLEN is 131,072 bytes and the argument is not the only thing
    on the command line — the flag itself, the tool allow-list, the MCP config
    and the environment all share the exec. A threshold set AT the kernel limit
    would still fail to exec.

    THE MUTATION THAT BREAKS THIS: raise ``_POSIX_PROMPT_SPILL_BYTES`` to
    131_072. Run: no headroom left and this fails.
    """
    max_arg_strlen = 32 * 4096  # PAGE_SIZE * 32, Linux binfmts.h
    assert _POSIX_PROMPT_SPILL_BYTES < max_arg_strlen
    # At least 32 KiB of room for the rest of the command line.
    assert max_arg_strlen - _POSIX_PROMPT_SPILL_BYTES >= 32_768


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_an_empty_prompt_never_spills(os_name: str) -> None:
    """Guards the boundary the other tests approach from above only."""
    assert _prompt_must_spill("", os_name=os_name) is False
