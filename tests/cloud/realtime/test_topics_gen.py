"""Guard: topics.gen.ts must stay in sync with EVENT_REGISTRY.

If you change events.py, re-run `uv run python scripts/gen_topics.py` and
commit the result alongside the events.py change.
"""

import subprocess
import sys
from pathlib import Path


def test_topics_gen_is_committed_state():
    repo_root = Path(__file__).resolve().parents[3]
    out = repo_root.parent / "paw-enterprise/src/lib/core/shared/topics.gen.ts"
    assert out.exists(), "topics.gen.ts missing -- run scripts/gen_topics.py"
    before = out.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, "scripts/gen_topics.py"],
        cwd=repo_root,
        check=True,
    )
    after = out.read_text(encoding="utf-8")
    assert before == after, (
        "topics.gen.ts is stale -- run `uv run python backend/scripts/gen_topics.py` "
        "and commit the result"
    )
