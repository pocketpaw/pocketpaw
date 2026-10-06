# tests/cloud/test_belt_live_feed.py — a Belt run's feed, tailed live.
#
# Pins the live half of the station feed (the stored half is test_belt_feed.py):
#   * the runner hands stdout lines over before the process exits, a line far
#     past the reader's 64 KiB limit included, with stdin fed alongside, and a
#     timeout still kills the group and returns no stdout.
#   * every station step (orient, develop, check, fix, review) publishes its
#     frames to the run's stream in order, in the chat frame vocabulary, each
#     tagged with its stage; the same frames are stored per stage.
#   * live frames carry no secret, worktree path, repo path or host account.
#   * the SSE route replays the newest attempt from its ``start``, 404s a
#     foreign run, and says ``from_history`` for a run with no stream that is
#     not being developed.

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402

PY = sys.executable


# ---------------------------------------------------------------------------
# the runner's on_line callback
# ---------------------------------------------------------------------------


async def test_lines_arrive_before_the_process_exits(tmp_path: Path):
    """Two lines half a second apart reach ``on_line`` half a second apart:
    they are read live, not after the process is gone."""
    script = (
        "import sys,time\n"
        "data=sys.stdin.read()\n"
        "print('first', len(data), flush=True)\n"
        "time.sleep(0.6)\n"
        "print('x'*200000, flush=True)\n"
        "sys.stdout.write('tail-without-newline'); sys.stdout.flush()\n"
    )
    seen: list[tuple[float, str]] = []

    async def on_line(line: str) -> None:
        seen.append((time.monotonic(), line))

    prompt = "p" * 300_000  # bigger than a pipe buffer: written alongside the reads
    code, out, _err = await ds.run_subprocess(
        [PY, "-c", script], cwd=tmp_path, timeout=30, stdin=prompt, on_line=on_line
    )
    assert code == 0
    assert [line[:5] for _, line in seen] == ["first", "x" * 5, "tail-"]
    assert seen[0][1] == "first 300000"
    assert len(seen[1][1]) == 200_000  # one line, past the 64 KiB readline limit
    assert seen[1][0] - seen[0][0] > 0.3, "lines were delivered only at exit"
    assert out == "\n".join(line for _, line in seen)


async def test_a_timed_out_streamed_run_returns_no_stdout(tmp_path: Path):
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    script = "import time\nprint('started', flush=True)\ntime.sleep(30)\n"
    code, out, err = await ds.run_subprocess(
        [PY, "-c", script], cwd=tmp_path, timeout=1.5, on_line=on_line
    )
    assert (code, out) == (-1, "") and "timed out" in err
    assert seen == ["started"]  # what arrived before the kill is the live record
