"""Generate paw-enterprise/src/lib/core/shared/topics.gen.ts from EVENT_REGISTRY.

Run via:  uv run python scripts/gen_topics.py

Updated 2026-08-07 (fix/test-guard-blindness). Three changes, all in service of
the staleness guard in ``tests/cloud/realtime/test_topics_gen.py``, which used
to run this script as a subprocess and diff the file before and after.

1. ``DEFAULT_OUT`` is the ONE derivation of the output path. The guard used to
   re-derive the same location from its own ``__file__`` at a different depth
   (``parents[3].parent`` there vs ``parents[2]`` here). Two derivations that
   agree only by directory depth are not a contract: move either file and they
   quietly point at different places, at which point ``before == after`` is
   trivially true and the gate passes forever. The guard now imports this
   constant instead of computing its own.

2. ``render()`` is pure. The guard can compare what the generator WOULD write
   against the committed file without writing anything, which matters because
   the output lives in a DIFFERENT REPO. A test in pocketpaw had no business
   mutating a tracked file in paw-enterprise while other agents work in it.

3. The write pins ``newline="\\n"``. ``write_text`` with the default
   ``newline=None`` translates to ``os.linesep``, so this script emitted CRLF on
   Windows and LF everywhere else — the same registry produced different bytes
   depending on who ran it. The old guard could not see that: ``read_text``
   normalises newlines before comparing, so a Linux runner rewrote the tracked
   file and the test still passed. LF is the right pin: the target file is one
   of ~10 CRLF files among ~190 LF ones in paw-enterprise/src, which makes its
   CRLF a Windows generation artifact rather than a convention.

   CONSEQUENCE, deliberately not handled here: the committed topics.gen.ts is
   still CRLF, so the next deliberate run of this script rewrites it LF and
   shows a whole-file, whitespace-only diff. That belongs in a paw-enterprise
   commit, not in a pocketpaw PR. Nothing does it automatically — the guard no
   longer writes.

WHAT THIS SCRIPT'S OUTPUT DEPENDS ON, which is the reason for ``--print``.
``EVENT_REGISTRY`` is populated by ``Event.__init_subclass__``, so it holds
exactly the events whose defining module has been IMPORTED — not every event in
the codebase. This script imports only ``_core.realtime.events``, so the file it
writes is a snapshot of that minimal import chain, and running the same function
inside a process that has imported more of ``pocketpaw_ee`` produces MORE topics.
``--print`` gives the guard a clean interpreter to render in, so the check is
independent of whatever else the test session imported. The old guard got this
isolation by accident, from running the script as a subprocess; losing it silently
would have made the check order-dependent.

Updated 2026-08-07 (fix/topics-gen-collection). Collection is now EXPLICIT — see
the imports below. The script used to render whatever its own import chain
happened to register, which was 136 of the 146 events that exist; ten were
missing from the frontend's ``Topic`` union entirely (``belt_plan``, five
``mandate.*``, four ``meeting.*``). Two imports fix it, and the completeness test
in ``tests/cloud/realtime/test_topics_gen.py`` fails if a declared event is ever
missing from the render again.

A full ``pkgutil.walk_packages`` over ``pocketpaw_ee`` would also work and was
rejected: it takes 29 seconds, and 8 modules raise on import (the vendored oasis
substrate, a worker), so it would need failures swallowed — which is how events
went missing in the first place. Two named imports plus a test that checks every
declared event reached the output is cheaper and fails louder.
"""

import sys
from pathlib import Path

import pocketpaw_ee.cloud.mandates.events  # noqa: F401  (import registers the events)
import pocketpaw_ee.cloud.meetings.events  # noqa: F401  (import registers the events)
from pocketpaw_ee.cloud._core.realtime.events import EVENT_REGISTRY

# COLLECTION IS EXPLICIT, and it has to be. ``EVENT_REGISTRY`` fills from
# ``Event.__init_subclass__``, so importing the registry module alone yields only
# the events declared in it. The two ``pocketpaw_ee.cloud.*.events`` imports above
# are what add the ten that were simply absent from the generated file, and that
# the frontend's ``Topic`` union never had: ``belt_plan`` and five ``mandate.*``
# from the first, four ``meeting.*`` from the second.
#
# Those imports look unused. They are not — importing the module is what registers
# its events, which is why they carry ``noqa: F401``. Deleting one silently
# shrinks the generated file.
#
# ADDING AN EVENT MODULE: if you declare events in a new module, import it here.
# ``test_every_declared_event_reaches_the_generated_file`` fails until you do,
# naming the topic it could not find — nobody should have to learn this rule by
# reading this comment.

#: The generated file, in the paw-enterprise sibling repo. The single source of
#: truth for this location — import it, never re-derive it.
DEFAULT_OUT = (
    Path(__file__).resolve().parents[2] / "paw-enterprise/src/lib/core/shared/topics.gen.ts"
)

HEADER = """// GENERATED -- do not edit. Run `uv run python backend/scripts/gen_topics.py`.
// Mirrors backend EVENT_REGISTRY keys.
"""


def render() -> str:
    """Return the full text of topics.gen.ts, newline-separated with LF.

    Pure: no I/O, no dependence on the current platform or on what is already
    on disk. Line-for-line what this script has always written — the guard
    compares this against the committed file, so a cosmetic edit here is a
    real change to another repo's tracked content.
    """
    topics = sorted(EVENT_REGISTRY.keys())
    lines = [HEADER, "export const TOPICS = ["]
    for t in topics:
        lines.append(f"  {t!r},")
    lines.append("] as const;")
    lines.append("")
    lines.append("export type Topic = (typeof TOPICS)[number];")
    return "\n".join(lines) + "\n"


def main(out: Path | None = None) -> Path:
    """Write the rendered topics to ``out`` (default: ``DEFAULT_OUT``).

    Returns the path actually written so a caller can assert the write landed
    where it asked — the old guard ran this as a subprocess and never checked
    that anything had been written at all.
    """
    target = Path(out) if out is not None else DEFAULT_OUT
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" (not the os.linesep default) so the bytes are identical on
    # every platform. See the module docstring.
    target.write_text(render(), encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--print" in args:
        # Render to stdout and write nothing. The staleness guard uses this so it
        # renders in a CLEAN interpreter (see the module docstring) without
        # touching a tracked file in the paw-enterprise repo.
        #
        # NOT a substitute for the write path: on Windows, text-mode stdout
        # translates to CRLF, so `gen_topics.py --print > file` produces
        # different bytes than `main()` does. Redirecting this is not a
        # supported way to generate the file — pass a path instead.
        sys.stdout.write(render())
    else:
        # An explicit destination, for regenerating into a checkout that is not
        # the sibling working copy DEFAULT_OUT resolves to.
        positional = [a for a in args if not a.startswith("-")]
        written = main(Path(positional[0]) if positional else None)
        print(f"wrote {len(EVENT_REGISTRY)} topics -> {written}")
