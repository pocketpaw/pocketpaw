# tests/ee/sites/test_generator_version_build_id.py
# Created: 2026-08-25 (fix/sites-artifact-cache-tracks-generator-build).
#
# THE BUG. ``generator_version()`` is the term in the native-artifact cache key that
# is supposed to mean "which paw-sites generator built this render". It did not: every
# term was a hand-bumped constant (``_ARTIFACT_FORMAT_EPOCH``) or a dep VERSION string
# (ripple, motion). Rebuilding ``paw-sites/dist/cli.js`` moved none of them, so the
# store kept serving a render made by the previous generator with no way to evict it.
#
# How it surfaced, which is why this deserves a test rather than a comment: RX-2 taught
# the generator to stamp ``data-uid`` on react leaves. After the rebuild, every react
# preview still served the pre-RX-2 cached render with ZERO uids. NativeSiteEditor reads
# ``uids.size === 0`` as "nothing editable here" and falls back to the section-level
# iframe editor — which, unlike the native one, takes no editMode prop and therefore
# arms in Browse too. The operator sees "react selection is coarse and stays on in
# Browse mode"; the cause is a cache key three layers away, and rebuilding the generator
# (the obvious move) changes nothing. That loop is what the build-id term ends.
#
# The tests pin all four properties the fix has to hold at once — it must invalidate on
# a rebuild WITHOUT becoming unstable, or it would defeat the cache it is guarding.
from __future__ import annotations

import os
from pathlib import Path

import pytest
from pocketpaw_ee.sites import generator_client
from pocketpaw_ee.sites.generator_client import (
    _generator_build_id,
    generator_version,
)


@pytest.fixture
def fake_generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``dist/cli.js`` that PAW_SITES_GEN_CMD points at, interpreter-prefixed
    — the documented local override shape, so the entry is NOT argv[0].

    POSIX separators, because ``_gen_cmd_argv`` runs the value through ``shlex.split``
    in POSIX mode: a Windows path written with backslashes comes out with them eaten
    ("D:\\paw-workspace\\…" → "D:paw-workspace…") and resolves to nothing. The real
    override on this box is written with forward slashes for exactly that reason, so
    the fixture matches the shape that actually works rather than one that cannot.
    """
    entry = tmp_path / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// generator build A", encoding="utf-8")
    monkeypatch.setenv("PAW_SITES_GEN_CMD", f"node {entry.as_posix()}")
    return entry


def test_the_script_is_fingerprinted_not_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap this fix nearly fell into. The shipped override names a real
    interpreter AND a real script — "C:/PROGRA~1/nodejs/node.exe D:/…/dist/cli.js" —
    so BOTH tokens resolve as files. Fingerprinting the first one takes node.exe,
    whose mtime does not move when the generator is rebuilt: the version would be a
    constant and the cache would stay stale, with the fix appearing to be in place.
    """
    interpreter = tmp_path / "node.exe"
    interpreter.write_text("binary-ish", encoding="utf-8")
    script = tmp_path / "dist" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("// generator build A", encoding="utf-8")
    monkeypatch.setenv(
        "PAW_SITES_GEN_CMD", f"{interpreter.as_posix()} {script.as_posix()}"
    )

    before = _generator_build_id()
    # Rebuild ONLY the script; the interpreter is untouched, as on a real rebuild.
    script.write_text("// generator build B, now a different length", encoding="utf-8")

    assert _generator_build_id() != before


def test_rebuilding_the_generator_changes_the_version(fake_generator: Path) -> None:
    """The regression. A rebuilt generator must yield a different cache key."""
    before = generator_version()

    # A rebuild that also changes the bundle size — the ordinary case.
    fake_generator.write_text("// generator build B, now longer", encoding="utf-8")

    assert generator_version() != before


def test_a_same_size_rebuild_still_changes_the_version(fake_generator: Path) -> None:
    """mtime carries the rebuild even when the bundle size lands identical.

    mtime is set explicitly rather than by writing twice: Windows' ~15.6ms clock
    granularity can hand two quick writes the SAME mtime_ns, which would make this
    assertion flaky for a reason that has nothing to do with the behaviour.
    """
    before = generator_version()

    fake_generator.write_text("// generator build C", encoding="utf-8")  # same length
    st = fake_generator.stat()
    os.utime(fake_generator, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    assert generator_version() != before


def test_an_unchanged_generator_is_stable(fake_generator: Path) -> None:
    """The other half, and the one that matters more: an unchanged generator must
    produce a STABLE key across calls. A version that moved on its own would turn
    every view into a cache miss and a full rebuild — worse than the bug."""
    assert generator_version() == generator_version() == generator_version()


def test_an_unresolvable_generator_degrades_to_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No generator on disk and none on PATH: report a fixed sentinel, never a
    changing value. This restores exactly the pre-fix behaviour (no auto-invalidation)
    instead of making the key unstable."""
    monkeypatch.setenv("PAW_SITES_GEN_CMD", "definitely-not-a-real-bin-xyz")

    assert _generator_build_id() == "unresolved"
    assert generator_version() == generator_version()


def test_the_build_id_reaches_the_artifact_cache_key(fake_generator: Path) -> None:
    """End to end: the term has to actually ride the hash the store is keyed on,
    with every OTHER input held identical. This is the assertion that would have
    caught the original bug — the two react pockets differed by generator alone."""
    from pocketpaw_ee.sites.service import _artifact_content_hash

    inputs = {
        "source": {"src/App.tsx": "export default () => <div>hi</div>;"},
        "theme": {},
        "builder_origin": "http://localhost:1420",
        "engine": "react",
    }
    before = _artifact_content_hash(gen_version=generator_version(), **inputs)

    fake_generator.write_text("// rebuilt with react uid stamping", encoding="utf-8")
    after = _artifact_content_hash(gen_version=generator_version(), **inputs)

    assert before != after, (
        "a generator rebuild must change the artifact cache key, or a render built "
        "by the old generator is served forever"
    )


def test_the_format_epoch_still_participates(fake_generator: Path) -> None:
    """The build id ADDS to the key, it does not replace the epoch — the epoch is
    still the only lever for a change to the Python-side extraction shape, which
    watching the generator file cannot detect."""
    before = generator_version()
    monkeypatch_epoch = "v-next"
    original = generator_client._ARTIFACT_FORMAT_EPOCH
    try:
        generator_client._ARTIFACT_FORMAT_EPOCH = monkeypatch_epoch
        assert generator_version() != before
    finally:
        generator_client._ARTIFACT_FORMAT_EPOCH = original
