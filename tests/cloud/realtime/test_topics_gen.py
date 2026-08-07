"""Guard: topics.gen.ts must stay in sync with EVENT_REGISTRY.

If you change events.py, re-run `uv run python scripts/gen_topics.py` and
commit the result in paw-enterprise alongside the events.py change.

REWRITTEN 2026-08-07 (fix/test-guard-blindness). The previous version had two
defects, one silent and one operational.

SILENT: it re-derived the output path itself — ``parents[3]`` then ``.parent``
here, ``parents[2]`` in the script — so the guard and the generator agreed on
where the file lives only by directory depth. Move either file and they point
somewhere different, the subprocess writes to one place, the test diffs
another, and ``before == after`` becomes trivially true. A staleness gate that
can never fail is worse than no gate: it reports PASS forever. It also never
checked that the subprocess had written anything at all, so a generator that
silently no-opped read as "in sync". Now the location is imported from the
generator (``DEFAULT_OUT``), which is the only place it is derived.

OPERATIONAL: it ran the generator with ``check=True``, which WROTE
``paw-enterprise/src/lib/core/shared/topics.gen.ts`` — a tracked file in a
sibling repo other agents are working in. On Windows the round trip happened to
be byte-stable so it only touched mtime, but the generator wrote with
``os.linesep``, so on Linux or macOS the same run rewrote that tracked file from
CRLF to LF. The test still passed, because ``read_text`` normalises newlines
before comparing. A test in pocketpaw dirtying a checkout in paw-enterprise is
not a tradeoff worth making for a staleness check, and it does not need to:
comparing ``render()`` to the committed text proves the same thing with no
writes at all.

So this file now: imports the generator, compares the pure render against the
committed file, exercises the write only into ``tmp_path``, and asserts —
through a fixture that snapshots its mtime — that nothing here touches the
sibling repo.
"""

import importlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pocketpaw_ee
import pytest

# The generator lives in THIS repo, so deriving its path from this test's own
# location cannot disagree about where the SIBLING repo is — that question has
# exactly one answer now, and it lives in gen_topics.DEFAULT_OUT. The assert is
# the loud failure: move either file and the guard errors instead of quietly
# passing, which is the whole defect being fixed.
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "gen_topics.py"
assert SCRIPT.exists(), f"gen_topics.py not found at {SCRIPT} — fix this path, do not delete it"

_spec = importlib.util.spec_from_file_location("gen_topics", SCRIPT)
assert _spec is not None and _spec.loader is not None
gen_topics = importlib.util.module_from_spec(_spec)
sys.modules["gen_topics"] = gen_topics
_spec.loader.exec_module(gen_topics)


@pytest.fixture(autouse=True)
def _sibling_repo_is_never_written():
    """Fail if anything in this module writes the real topics.gen.ts — and undo it.

    This is the operational half of the fix, kept honest: the guard may READ
    paw-enterprise's tracked file and must never write it. Detection is on
    mtime, which moves on every write even when the bytes are identical, so
    re-introducing the old ``main()``-with-no-argument call trips this.

    It RESTORES as well as reports, and that is not belt-and-braces. Proving
    this guard works means writing the file once on purpose, and the bytes that
    lands are LF while the committed file is still CRLF — so a detect-only
    fixture would leave a dirty tracked file in another repo every time someone
    mutation-tests this plan. A guard against damaging a sibling checkout that
    damages a sibling checkout to prove itself is not a guard.
    """
    out = gen_topics.DEFAULT_OUT
    existed = out.exists()
    before_bytes = out.read_bytes() if existed else None
    before_mtime = out.stat().st_mtime_ns if existed else None
    try:
        yield
    finally:
        after_mtime = out.stat().st_mtime_ns if out.exists() else None
        touched = before_mtime != after_mtime
        if touched and existed and before_bytes is not None:
            if out.read_bytes() != before_bytes:
                out.write_bytes(before_bytes)
    assert not touched, (
        f"a test in this module wrote {out} — that is a tracked file in the "
        "paw-enterprise repo (its content has been restored). Render and "
        "compare, or write to tmp_path."
    )


def _render_in_clean_interpreter() -> str:
    """Render via ``gen_topics.py --print`` in a fresh process.

    NOT the same as calling ``gen_topics.render()`` here, and the difference is
    load-bearing. ``EVENT_REGISTRY`` is filled by ``Event.__init_subclass__``, so
    it contains whatever events the current process has IMPORTED. In this test
    session that depends on which other test modules ran first: measured
    2026-08-07, ``tests/cloud/composio`` pulls in the belt events, so an
    in-process render gains ``belt_plan`` and the comparison below fails purely
    on collection order.

    The old guard ran the generator as a subprocess and got this isolation
    without anyone writing down that it needed it. Keeping the subprocess — but
    for stdout instead of for a write into another repo — keeps the check
    order-independent while dropping the part that was actually harmful.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--print"],
        capture_output=True,
        text=True,  # normalises the pipe's newlines, so this is content, not bytes
        check=True,
    )
    return proc.stdout


def test_topics_gen_is_committed_state() -> None:
    """The committed file matches what the generator would produce now.

    EXPECTED RED on fix/topics-gen-collection, and deliberately not weakened.
    That branch makes the generator collect the ten events it was missing, so
    this now compares 146 rendered topics against a file paw-enterprise still
    has at 136. The failure diff IS the defect, listed. It goes green when the
    paired paw-enterprise PR lands the regenerated file on that repo's dev.

    Not marked xfail: an xfail that flips to XPASS the moment another repo
    merges is a second cross-repo timing bomb, and the whole point of this
    thread is guards that report the wrong thing. A red test with an
    explanation is honest; a green one that is green for a scheduling reason is
    not. DEFAULT_OUT resolves to the working sibling checkout, which this test
    may never write, so it cannot be made green from here.
    """
    out = gen_topics.DEFAULT_OUT
    assert out.exists(), f"topics.gen.ts missing at {out} -- run scripts/gen_topics.py"
    # read_text normalises newlines, so this compares CONTENT and tolerates the
    # committed file's CRLF. The bytes the generator writes are pinned
    # separately, below.
    committed = out.read_text(encoding="utf-8")
    assert committed == _render_in_clean_interpreter(), (
        "topics.gen.ts is stale -- run `uv run python scripts/gen_topics.py` "
        "and commit the result in paw-enterprise"
    )


def test_render_covers_every_registered_event() -> None:
    """Every EVENT_REGISTRY key reaches the generated file.

    The comparison above passes if render() and the committed file agree, which
    is also true when both are wrong. This pins render() to the registry
    directly, so dropping a topic from the output fails here even if someone
    regenerates the file to match.
    """
    from pocketpaw_ee.cloud._core.realtime.events import EVENT_REGISTRY

    rendered = gen_topics.render()
    assert EVENT_REGISTRY, "EVENT_REGISTRY is empty — this guard would prove nothing"
    for topic in EVENT_REGISTRY:
        assert f"  {topic!r}," in rendered, f"{topic!r} is registered but not in the output"


# ── Completeness + determinism of the collection (fix/topics-gen-collection) ──
# The generator renders EVENT_REGISTRY, which fills from Event.__init_subclass__
# — so it holds only the events whose module was imported. That made the output a
# function of the import chain rather than of the codebase, and ten events never
# reached the frontend's Topic union at all. These two tests pin the fix from
# both ends: nothing declared is missing, and the answer does not depend on who
# is asking.

_EVENT_TYPE_DECL = re.compile(r"""EVENT_TYPE\s*(?::\s*ClassVar\[str\]\s*)?=\s*["']([^"']+)["']""")


def _declared_event_types() -> set[str]:
    """Every EVENT_TYPE literal declared anywhere in the pocketpaw_ee package.

    This IS a source scan, which this sprint has spent a while distrusting, so
    it earns its place by pinning its own liveness: the test below asserts the
    scan found known sentinels and a plausible floor. A refactor that changes
    how EVENT_TYPE is declared therefore breaks the scan LOUDLY instead of
    quietly finding nothing and reporting success — which is the exact failure
    mode that made the other scans worthless.

    The root is derived from the imported package, not from another parents[N]
    walk, so it cannot drift the way the two output-path derivations did.
    """
    root = Path(pocketpaw_ee.__file__).resolve().parent
    found: set[str] = set()
    for path in root.rglob("*.py"):
        found.update(_EVENT_TYPE_DECL.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found


def test_the_scan_that_checks_completeness_is_itself_alive() -> None:
    """The completeness scan must actually be finding declarations."""
    declared = _declared_event_types()
    # Sentinels from three different modules: the core registry, the mandates
    # module, and the meetings module. If a declaration-style change stops the
    # regex matching, at least one of these disappears and this fails.
    assert "pocket.created" in declared, "scan lost the core events"
    assert "belt_plan" in declared, "scan lost the mandates events"
    assert "meeting.transcript_ready" in declared, "scan lost the meetings events"
    # A floor, so a scan that silently degrades to a handful of matches fails
    # instead of vacuously passing the completeness test below.
    assert len(declared) >= 140, f"scan found only {len(declared)} EVENT_TYPE declarations"


def test_every_declared_event_reaches_the_generated_file() -> None:
    """No event declared in the codebase is missing from the render.

    This is the guard for the defect: the generator collects by importing, so an
    event module nobody imports is invisible to it. Declaring events in a NEW
    module fails here until that module is imported in gen_topics.py.
    """
    rendered = gen_topics.render()
    missing = sorted(t for t in _declared_event_types() if f"  {t!r}," not in rendered)
    assert not missing, (
        f"{len(missing)} declared event(s) never reach topics.gen.ts: {missing}. "
        "Import the module that declares them in scripts/gen_topics.py."
    )


def test_collection_does_not_depend_on_what_the_process_imported() -> None:
    """The render is the same in a loaded process and in a clean one.

    Before the explicit imports, these two differed: a process that had imported
    the mandates or meetings modules rendered more topics than the generator's
    own minimal chain did. Importing them here FORCES the divergence to exist if
    collection is still lazy, so this does not depend on which other test
    modules ran first.
    """
    importlib.import_module("pocketpaw_ee.cloud.mandates.events")
    importlib.import_module("pocketpaw_ee.cloud.meetings.events")
    assert gen_topics.render() == _render_in_clean_interpreter(), (
        "the generated topics depend on what the calling process imported — "
        "collection in scripts/gen_topics.py is not explicit"
    )


def test_main_writes_where_it_is_told(tmp_path: Path) -> None:
    """main(out) writes to out and reports the path it wrote.

    The old guard ran the generator as a subprocess and never confirmed a write
    happened, so a no-op generator read as "in sync".
    """
    target = tmp_path / "nested" / "topics.gen.ts"
    written = gen_topics.main(target)
    assert written == target
    assert target.exists(), "main() returned a path it did not write"
    assert target.read_text(encoding="utf-8") == gen_topics.render()


def test_generated_bytes_are_lf_on_every_platform(tmp_path: Path) -> None:
    """The generator pins LF instead of following os.linesep.

    Without the pin this script emits CRLF on Windows and LF elsewhere, so the
    same registry produces different bytes depending on who ran it — and the
    content comparison above cannot see it, because read_text normalises.

    NOTE: this assertion can only FAIL on a platform whose os.linesep is not
    "\\n". On Linux and macOS the unpinned default already produces LF, so the
    mutation that removes the pin escapes there. It is caught on Windows, which
    is where the CRLF in the committed file came from.
    """
    target = tmp_path / "topics.gen.ts"
    gen_topics.main(target)
    raw = target.read_bytes()
    assert b"\r\n" not in raw, "generator emitted CRLF — pin newline='\\n' in main()"
    assert b"\n" in raw


def test_default_out_points_at_the_generated_file() -> None:
    """DEFAULT_OUT still resolves to the real file in the sibling repo.

    Guards the derivation itself. If the repo layout moves and this constant
    starts pointing at nothing, every other check here would still pass against
    a path no one reads — the exact blindness this rewrite exists to remove.
    """
    out = gen_topics.DEFAULT_OUT
    assert out.name == "topics.gen.ts"
    assert out.parent.name == "shared"
    assert out.exists(), f"DEFAULT_OUT does not resolve to a real file: {out}"
