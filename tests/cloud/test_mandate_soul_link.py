# tests/cloud/test_mandate_soul_link.py — the mandate <-> soul bridge.
#
# ``remember_shift`` appends a shift summary to the mandate's bound soul and
# saves it back where it was: a ``.soul`` archive stays an archive, a soul
# directory stays a directory. Real soul-protocol on tmp files, no mocks: the
# assertion is that a re-awakened soul recalls the memory, not just that the
# call returned True (it swallows errors by design).

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")
soul_protocol = pytest.importorskip("soul_protocol")

from pocketpaw_ee.cloud.mandates.soul_link import remember_shift  # noqa: E402

SUMMARY = "Shift 3 closed gap G41 text shapes with a passing CI run"


async def _recalls(source: Path) -> list[str]:
    soul = await soul_protocol.Soul.awaken(source)
    return [str(e.content) for e in await soul.recall("G41 text shapes", limit=5)]


async def test_remember_shift_saves_back_into_a_soul_file(tmp_path: Path):
    path = tmp_path / "creative-core-v0.2.soul"
    soul = await soul_protocol.Soul.birth("creative-core")
    await soul.export(path, include_keys=True)

    assert await remember_shift(str(path), SUMMARY) is True

    assert path.is_file()  # still an archive, not turned into a directory
    assert any(SUMMARY in line for line in await _recalls(path))


async def test_remember_shift_saves_back_into_a_soul_directory(tmp_path: Path):
    path = tmp_path / ".soul"
    soul = await soul_protocol.Soul.birth("creative-core")
    await soul.save_local(path)

    assert await remember_shift(str(path), SUMMARY) is True

    assert path.is_dir()
    assert any(SUMMARY in line for line in await _recalls(path))
