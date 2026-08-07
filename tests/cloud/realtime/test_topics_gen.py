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

import importlib.util
import subprocess
import sys
from pathlib import Path

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
    """The committed file matches what the generator would produce now."""
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
