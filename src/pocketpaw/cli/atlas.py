# CLI atlas command — build/check the compiled atlas artifact (AT-4).
# Created: 2026-07-02 (feat/atlas-compiler).
# `pocketpaw atlas build` compiles authored entries + extracted connector
# and sense knowledge into src/pocketpaw/atlas/data/atlas.json (the
# checked-in, byte-deterministic artifact). `--check` compiles to memory
# and exits non-zero with a diff summary when the checked-in artifact is
# stale — the CI freshness gate. Must run from the repo root (the
# compiler reads the repo's connectors/ dir).

from __future__ import annotations

from pathlib import Path

from pocketpaw.cli.utils import print_fail, print_ok


def run_atlas_cmd(action: str | None = None, check: bool = False) -> int:
    """Manage the compiled atlas artifact.

    - build: compile authored + extracted entries and write the artifact
    - build --check: compile to memory; exit 1 with a diff summary if the
      checked-in artifact is stale (for CI)
    """
    if action != "build":
        print_fail("Usage: pocketpaw atlas build [--check]")
        return 1

    from pocketpaw.atlas.compile import DEFAULT_CONNECTORS_DIR, check_artifact, write_artifact

    connectors_dir = Path(DEFAULT_CONNECTORS_DIR)
    if not connectors_dir.is_dir():
        print_fail(
            f"connectors dir not found at ./{connectors_dir} — "
            "run `pocketpaw atlas build` from the repo root"
        )
        return 1

    if check:
        fresh, summary = check_artifact(connectors_dir=connectors_dir)
        if fresh:
            print_ok("atlas artifact is up to date")
            return 0
        print_fail(summary)
        return 1

    path, model = write_artifact(connectors_dir=connectors_dir)
    kinds: dict[str, int] = {}
    for entry in model.entries:
        kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
    breakdown = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    print_ok(f"wrote {path} ({len(model.entries)} entries: {breakdown})")
    return 0
