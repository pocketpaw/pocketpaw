# tests/test_process_exits_cleanly.py — a store eviction must not keep the
# interpreter alive at exit.
#
# aiosqlite runs each connection on a NON-daemon worker thread, so a connection
# that is never closed blocks interpreter shutdown forever. The store factory
# (pocketpaw.stores) closes an evicted handle with a fire-and-forget task on the
# running loop. pytest-asyncio closes each test's loop WITHOUT cancelling
# pending tasks, so that task can be orphaned mid-flight; if it holds an
# aiosqlite connection, the process never exits (the CI flag-mode hang) or the
# worker dies printing "Event loop is closed".
#
# The guard runs the eviction in a child process exactly the way a test run
# does (new loop, evict, loop.close() with the task still pending) and asserts
# the child exits promptly and quietly.

from __future__ import annotations

import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from pocketpaw import stores

    stores._DATA_DIR = Path(sys.argv[1])

    async def one_test(i):
        for get in (stores.get_fabric_store, stores.get_instinct_store,
                    stores.get_agent_ledger_store):
            await get(workspace_id=f"ws{i}")._ensure_schema()
        stores.reset_store_caches()  # evicts -> schedules each handle's aclose
        await asyncio.sleep(0)       # the aclose task starts; the test ends

    for i in range(5):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(one_test(i))
        # pytest-asyncio's teardown: shutdown_asyncgens, close, no task cancel.
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    print("done", flush=True)
    """
)


def test_evicted_store_handles_do_not_keep_the_process_alive(tmp_path):
    script = tmp_path / "evict.py"
    script.write_text(_SCRIPT)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    proc = subprocess.run(
        [sys.executable, str(script), str(data_dir)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "done"
    assert "Event loop is closed" not in proc.stderr, proc.stderr
    assert "Exception in thread" not in proc.stderr, proc.stderr
