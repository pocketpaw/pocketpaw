# tests/test_version_matches_pyproject.py — the number we serve is the number we shipped.
#
# Created 2026-09-13. ``src/pocketpaw/__init__.py`` used to carry a hand-written
# ``__version__`` string. It fell three releases behind ``pyproject.toml``
# without anything noticing, and because ``GET /api/v1/version`` serves it, a
# healthy deployment running current ``dev`` reported 0.4.15. That sent a launch
# day into a deploy investigation for a deploy that was fine.
#
# ``__version__`` now reads installed distribution metadata, so the two cannot
# disagree in a built image. This test guards the source tree the release is cut
# from: if someone reintroduces a literal, or bumps pyproject without
# reinstalling, the mismatch fails here instead of in production telemetry.

from __future__ import annotations

import pathlib
import tomllib

import pocketpaw

_PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_dunder_version_matches_pyproject() -> None:
    declared = tomllib.loads(_PYPROJECT.read_text())["project"]["version"]
    assert pocketpaw.__version__ == declared, (
        f"pocketpaw.__version__ is {pocketpaw.__version__!r} but pyproject.toml "
        f"declares {declared!r}. /api/v1/version serves __version__, so this gap "
        f"is invisible until a deployment reports the wrong release."
    )


def test_version_is_not_the_uninstalled_fallback() -> None:
    """The fallback means metadata lookup failed, which in CI is a broken install."""
    assert pocketpaw.__version__ != "0.0.0.dev0"
