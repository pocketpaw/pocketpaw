# ee/pocketpaw_ee/ship_engine/transcripts/__init__.py — recorded Dokku CLI
# output + the zero-network fake transport that replays it (SHIP-1).
#
# Each ``*.txt`` file alongside this module is one command's captured output
# in a tiny sectioned format (leading ``#`` lines are comments):
#
#     EXIT: <int>
#     --- stdout ---
#     <verbatim stdout>
#     --- stderr ---
#     <verbatim stderr>
#
# ``FakeSSHTransport`` implements the driver's ``SSHTransport`` protocol by
# mapping EXACT command strings to transcript names — so the whole
# ``DokkuDriver`` (and any future SSH-driven engine) is testable with zero
# network, and the contract suite replays realistic CLI output instead of
# hand-typed stubs. Ships in the package (not tests/) so a future
# DokployDriver's suite can reuse the harness.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from pocketpaw_ee.ship_engine.dokku import CommandResult

_TRANSCRIPTS_DIR = Path(__file__).parent

_STDOUT_MARKER = "--- stdout ---"
_STDERR_MARKER = "--- stderr ---"


def load_transcript(name: str) -> CommandResult:
    """Parse ``<name>`` (a ``*.txt`` beside this module) into a CommandResult."""
    lines = (_TRANSCRIPTS_DIR / name).read_text().splitlines()
    exit_code: int | None = None
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    section: list[str] | None = None
    for line in lines:
        if section is None and line.startswith("#"):
            continue  # header comment
        if section is None and line.startswith("EXIT:"):
            exit_code = int(line.removeprefix("EXIT:").strip())
            continue
        if line.strip() == _STDOUT_MARKER:
            section = stdout_lines
            continue
        if line.strip() == _STDERR_MARKER:
            section = stderr_lines
            continue
        if section is not None:
            section.append(line)
    if exit_code is None:
        raise ValueError(f"transcript {name!r} has no 'EXIT:' line")
    return CommandResult(
        exit_code=exit_code,
        stdout=_join(stdout_lines),
        stderr=_join(stderr_lines),
    )


def _join(lines: list[str]) -> str:
    """Rejoin a section; real CLI output ends with a newline when non-empty."""
    return "\n".join(lines) + "\n" if lines else ""


class FakeSSHTransport:
    """Replays transcripts for exact command strings — zero network.

    ``replies`` maps the EXACT command a driver will issue to a transcript
    filename. An unmapped command fails the test loudly (it means the driver
    changed its command surface). ``delay`` sleeps before answering so
    driver timeout budgets can be exercised. ``calls`` records every command
    for sequence assertions.
    """

    def __init__(self, replies: Mapping[str, str], *, delay: float = 0.0) -> None:
        self._replies = dict(replies)
        self._delay = delay
        self.calls: list[str] = []

    async def run(self, command: str) -> CommandResult:
        self.calls.append(command)
        if self._delay:
            await asyncio.sleep(self._delay)
        try:
            name = self._replies[command]
        except KeyError:
            raise AssertionError(
                f"FakeSSHTransport has no transcript for command: {command!r}"
            ) from None
        return load_transcript(name)
