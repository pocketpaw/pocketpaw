# tests/ee/test_soul_isolation.py — the test suite must never touch the
# developer's real ~/.soul (journal.db, decisions.db), and must not leak the
# decisions store's process-global db path from one test into the next.
#
# Guarded by the autouse ``_isolate_soul_data_dir`` fixture in tests/conftest.py.
# Without it, any later ``mount_cloud`` replayed the whole real journal into a
# fresh temp store (1182 s in one census run).
#
# The journal check is a canary (unique event id looked up in the real file),
# not an mtime check: other pytest processes on a dev box write ~/.soul
# concurrently, so mtimes move regardless of this test.

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pocketpaw_ee.cloud.decisions import store
from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_event
from pocketpaw_ee.cloud.decisions.service import get_decision_graph, reset_projection_for_tests
from soul_protocol.spec.journal import Actor

REAL_SOUL = Path.home() / ".soul"


def test_decision_event_stays_out_of_real_soul() -> None:
    event_id = uuid4()
    reset_projection_for_tests()
    try:
        record_decision_event(
            action="agent.proposed",
            correlation_id=uuid4(),
            actor=Actor(kind="agent", id="did:soul:iso_canary", scope_context=["org:iso"]),
            scope=["org:iso"],
            event_id=event_id,
            payload={"intent": "soul-isolation canary", "action": "noop"},
        )
        db_path = Path(get_decision_graph().store._db_path).resolve()
    finally:
        reset_projection_for_tests()

    assert not db_path.is_relative_to(REAL_SOUL.resolve()), db_path

    real_journal = REAL_SOUL / "journal.db"
    if real_journal.exists():
        # mode=ro needs the -shm file a live writer leaves behind; not immutable=1,
        # which would skip the WAL where a leaked event lands.
        try:
            conn = sqlite3.connect(f"file:{real_journal}?mode=ro", uri=True)
            row = conn.execute("SELECT 1 FROM events WHERE id = ?", (str(event_id),)).fetchone()
            conn.close()
        except sqlite3.OperationalError as exc:
            pytest.skip(f"real journal not readable read-only: {exc}")
        assert row is None, "test event landed in the real ~/.soul/journal.db"


class TestDbPathRestored:
    """Two tests in file order: the first leaks a path, the second checks it."""

    leaked: Path | None = None

    def test_a_override_db_path(self, tmp_path: Path) -> None:
        TestDbPathRestored.leaked = tmp_path / "d.db"
        store.set_db_path(TestDbPathRestored.leaked)

    def test_b_db_path_was_restored(self) -> None:
        leaked = TestDbPathRestored.leaked
        assert leaked is not None, "run the whole class"
        current = store.get_db_path()
        assert current != leaked
        assert not current.is_relative_to(leaked.parent), current
