"""Generate paw-enterprise/src/lib/core/shared/topics.gen.ts from EVENT_REGISTRY.

Run via:  uv run python scripts/gen_topics.py
"""

import os
from pathlib import Path

from pocketpaw_ee.cloud._core.realtime.events import EVENT_REGISTRY

# EVENT_REGISTRY is populated by ``Event.__init_subclass__``, so an event class
# is only in it once its module has been imported. Importing ``_core`` alone
# therefore generates a topic list missing every event declared in a domain
# module — which is how ``meeting.recording_ready`` and
# ``meeting.transcript_ready`` ended up handled by the frontend dispatcher but
# absent from TOPICS. Import the domain event modules for their registration
# side effect before reading the registry.
from pocketpaw_ee.cloud.meetings import events as _meetings_events  # noqa: F401


def _resolve_out() -> Path:
    """Locate paw-enterprise's topics file, or say so instead of guessing.

    This used to be a bare ``parents[2] / "paw-enterprise/..."``, which is only
    correct when the script runs from the primary checkout. Run it from a git
    worktree and ``parents[2]`` is the worktrees directory, so it wrote to a
    ``paw-enterprise/`` sibling that does not exist — and the ``mkdir`` below
    happily created one, printing success while the real file went untouched.
    Walk up looking for a paw-enterprise that actually has the target
    directory, and fail loudly when there is none.
    """
    override = os.environ.get("PAW_ENTERPRISE_DIR")
    candidates = (
        [Path(override)]
        if override
        else [p / "paw-enterprise" for p in Path(__file__).resolve().parents[1:5]]
    )
    for cand in candidates:
        target_dir = cand / "src/lib/core/shared"
        if target_dir.is_dir():
            return target_dir / "topics.gen.ts"
    raise SystemExit(
        "gen_topics: could not find paw-enterprise/src/lib/core/shared next to "
        f"{Path(__file__).resolve().parents[1]}. Set PAW_ENTERPRISE_DIR to point at it."
    )


OUT = _resolve_out()

HEADER = """// GENERATED -- do not edit. Run `uv run python backend/scripts/gen_topics.py`.
// Mirrors backend EVENT_REGISTRY keys.
"""


def main() -> None:
    topics = sorted(EVENT_REGISTRY.keys())
    lines = [HEADER, "export const TOPICS = ["]
    for t in topics:
        lines.append(f"  {t!r},")
    lines.append("] as const;")
    lines.append("")
    lines.append("export type Topic = (typeof TOPICS)[number];")
    # No mkdir: _resolve_out() already proved the directory exists. Creating it
    # here is what let a wrong path masquerade as a successful run.
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(topics)} topics -> {OUT}")


if __name__ == "__main__":
    main()
