# tests/cloud/chat/test_turn_images.py — the images ONE turn looks at.
#
# Created 2026-09-09 (feat/other-hand-vision).
# Updated 2026-09-11 (feat/other-hand-page-vision) — cover the PER-IMAGE cap.
# The turn budget was the only size check, and it is the wrong shape to catch
# the case that actually bites: one 5MB page snapshot sits well inside a 6MB
# turn and is refused by the provider instead of by us.
#
# Two things are worth testing here, and neither is "the happy path works":
#
#   1. The path came back from the CLIENT. It echoes what the snapshot endpoint
#      returned, so it is not secret and a hostile client can send any string.
#      Reading happens in the cloud precisely because this is the layer that
#      knows which directory belongs to the tenant asking, so the jail check is
#      the reason this function lives here at all.
#   2. A turn that cannot read its picture must still RUN. The preamble names
#      the path either way; losing the attachment costs the model its eyes for
#      one turn, and raising costs the user their turn.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface.domain import SurfaceContext, SurfaceKind, SurfaceMeta

WORKSPACE = "ws-images"

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture()
def jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace jail rooted under tmp_path, as the real one is under $HOME."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(tmp_path / "workspaces"))
    mine = tmp_path / "workspaces" / WORKSPACE / "agent" / "sess"
    mine.mkdir(parents=True)
    return mine


def _ctx(*images: str) -> object:
    """A ScopeContext stand-in carrying only what _read_turn_images reads."""

    class _Ctx:
        workspace_id = WORKSPACE
        surface_context = SurfaceContext(
            workspace_id=WORKSPACE,
            user_id="u1",
            kind=SurfaceKind.OTHER_HAND,
            meta=SurfaceMeta(),
            preamble="x",
            preamble_images=tuple(images),
        )

    return _Ctx()


class TestTheJailIsTheBoundary:
    def test_reads_an_image_inside_this_workspace(self, jail: Path) -> None:
        page = jail / "page.png"
        page.write_bytes(PNG)
        assert run_core._read_turn_images(_ctx(str(page))) == ((PNG, "image/png"),)

    def test_refuses_a_path_in_another_workspace(self, jail: Path) -> None:
        theirs = jail.parent.parent.parent / "ws-someone-else" / "agent" / "s"
        theirs.mkdir(parents=True)
        page = theirs / "page.png"
        page.write_bytes(PNG)
        assert run_core._read_turn_images(_ctx(str(page))) == ()

    def test_refuses_a_traversal_back_out_of_the_jail(self, jail: Path) -> None:
        outside = jail.parent.parent.parent.parent / "secret.png"
        outside.write_bytes(PNG)
        climb = str(jail / ".." / ".." / ".." / ".." / "secret.png")
        assert run_core._read_turn_images(_ctx(climb)) == ()

    def test_refuses_a_file_that_is_not_an_image(self, jail: Path) -> None:
        # The media type is derived from the suffix, so an unknown one has no
        # honest value to send and is dropped rather than guessed at.
        secret = jail / "notes.txt"
        secret.write_bytes(b"whatever")
        assert run_core._read_turn_images(_ctx(str(secret))) == ()


class TestALostPictureNeverCostsTheTurn:
    def test_a_missing_file_is_skipped_not_raised(self, jail: Path) -> None:
        assert run_core._read_turn_images(_ctx(str(jail / "gone.png"))) == ()

    def test_an_empty_file_is_skipped(self, jail: Path) -> None:
        (jail / "empty.png").write_bytes(b"")
        assert run_core._read_turn_images(_ctx(str(jail / "empty.png"))) == ()

    def test_one_bad_path_does_not_lose_the_good_ones(self, jail: Path) -> None:
        good = jail / "page.png"
        good.write_bytes(PNG)
        out = run_core._read_turn_images(_ctx("/etc/passwd", str(good)))
        assert out == ((PNG, "image/png"),)

    def test_a_surface_with_no_images_reads_nothing(self, jail: Path) -> None:
        # The withhold-when-empty guarantee: every other surface keeps the
        # plain-string prompt path it has always taken.
        assert run_core._read_turn_images(_ctx()) == ()

    def test_an_oversized_image_is_skipped(self, jail: Path, monkeypatch) -> None:
        monkeypatch.setattr(run_core, "_MAX_TURN_IMAGE_BYTES", 16)
        big = jail / "big.png"
        big.write_bytes(PNG)
        assert run_core._read_turn_images(_ctx(str(big))) == ()

    def test_at_most_three_images_ride_one_turn(self, jail: Path) -> None:
        paths = []
        for i in range(5):
            p = jail / f"p{i}.png"
            p.write_bytes(PNG)
            paths.append(str(p))
        assert len(run_core._read_turn_images(_ctx(*paths))) == 3

    def test_an_image_over_the_per_image_cap_is_skipped_and_the_rest_still_ride(
        self, jail: Path
    ) -> None:
        # The per-image ceiling is NOT the turn budget, and this is the gap it
        # closes: at 5MB+1 this page fits the 6MB turn budget with room to
        # spare, so the budget check passes it and the provider rejects it —
        # a failed turn with nothing useful said. It must be dropped HERE, and
        # dropping it must not cost the turn the picture that was fine.
        big = jail / "big.png"
        big.write_bytes(PNG)
        with big.open("r+b") as fh:  # sparse — the bytes are never read
            fh.truncate(run_core._MAX_IMAGE_BYTES + 1)
        assert big.stat().st_size < run_core._MAX_TURN_IMAGE_BYTES
        small = jail / "small.png"
        small.write_bytes(PNG)
        assert run_core._read_turn_images(_ctx(str(big), str(small))) == ((PNG, "image/png"),)
