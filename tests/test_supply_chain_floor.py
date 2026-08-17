"""The Python install-time supply-chain floor, asserted on the files that enforce it.

Added with the floor itself: `[tool.uv] exclude-newer` in pyproject.toml (root and
ee/). The workspace policy is a 7-day minimum release age, and it was documented as
living in the developer's home directory — where none of those config files actually
exist. Per-repo enforcement replaced that fiction; this test is what keeps pocketpaw's
half of it from being deleted or quietly neutered later.

What is worth asserting here is not that a line of TOML is present — that is a
tautology — but the two properties that can rot independently of it:

  * the declared floor and the LOCKED distributions agree. A lock regenerated
    without the floor, or hand-edited, admits a distribution published after the
    cutoff while pyproject.toml still claims a floor. That is the failure that looks
    fine in review.
  * no adjacent uv.toml exists. Verified on uv 0.10.4: an adjacent uv.toml makes uv
    ignore the settings fields in `[tool.uv]` and merely warn, so someone adding one
    for an unrelated reason silently removes this floor.

Mutation coverage: tests/mutations/supply_chain_floor_python.json.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: (pyproject, lockfile) for every independently-resolved Python project in the repo.
#: ee/ carries its own uv.lock, so it resolves on its own and needs its own floor —
#: uv settings do not inherit from the parent directory.
PROJECTS = [
    (REPO_ROOT / "pyproject.toml", REPO_ROOT / "uv.lock"),
    (REPO_ROOT / "ee" / "pyproject.toml", REPO_ROOT / "ee" / "uv.lock"),
]


def _declared_floor(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    uv = data.get("tool", {}).get("uv", {})
    floor = uv.get("exclude-newer")
    assert floor, (
        f"{pyproject.relative_to(REPO_ROOT).as_posix()} declares no "
        "[tool.uv] exclude-newer, so this project resolves off the index with no "
        "release-age floor at all"
    )
    return str(floor)


def _locked_floor(lockfile: Path) -> str | None:
    data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    return data.get("options", {}).get("exclude-newer")


def _locked_upload_times(lockfile: Path) -> list[tuple[str, str, str]]:
    """(upload_time, package, version) for every distribution the lock pins.

    Read out of the lock rather than off the network: this asserts what a `uv sync`
    would actually install, which is the thing the floor exists to constrain.
    """
    data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str]] = []
    for pkg in data.get("package", []):
        name = pkg.get("name", "?")
        version = pkg.get("version", "?")
        dists = [pkg.get("sdist")] + list(pkg.get("wheels", []))
        for dist in dists:
            if isinstance(dist, dict) and dist.get("upload-time"):
                out.append((str(dist["upload-time"]), name, str(version)))
    return out


class TestThePythonSupplyChainFloor:
    @pytest.mark.parametrize(
        ("pyproject", "lockfile"),
        PROJECTS,
        ids=lambda p: p.parent.name if p.name == "pyproject.toml" else p.parent.name,
    )
    def test_the_lock_was_resolved_under_the_declared_floor(
        self, pyproject: Path, lockfile: Path
    ) -> None:
        """uv stamps the cutoff it resolved under into the lock's [options]. A mismatch
        means the lock predates the current floor, and `uv sync` installs from the lock
        — so the declared floor would be governing nothing that actually gets installed.
        Re-resolve with `uv lock` when this fails; do not edit the lock by hand.
        """
        declared = _declared_floor(pyproject)
        locked = _locked_floor(lockfile)
        rel = lockfile.relative_to(REPO_ROOT).as_posix()
        assert locked == declared, (
            f"{rel} records exclude-newer={locked!r} but "
            f"{pyproject.relative_to(REPO_ROOT).as_posix()} declares {declared!r}. "
            "Run `uv lock` in that project directory."
        )

    @pytest.mark.parametrize(("pyproject", "lockfile"), PROJECTS, ids=lambda p: p.parent.name)
    def test_no_locked_distribution_is_newer_than_the_floor(
        self, pyproject: Path, lockfile: Path
    ) -> None:
        """The floor's actual promise, checked against the pins rather than the config.

        Compared as ISO-8601 strings, which sort correctly and identically here because
        both sides are UTC `Z` timestamps — no timezone parsing to get subtly wrong.
        """
        declared = _declared_floor(pyproject)
        rel = lockfile.relative_to(REPO_ROOT).as_posix()
        newer = [(t, n, v) for t, n, v in _locked_upload_times(lockfile) if t > declared]
        assert not newer, (
            f"{rel} pins {len(newer)} distribution(s) published after the "
            f"{declared} floor: "
            + ", ".join(f"{n} {v} ({t})" for t, n, v in sorted(newer, reverse=True)[:5])
        )

    def test_the_two_projects_declare_the_same_floor(self) -> None:
        """Bumping one and forgetting the other leaves the OSS core and the enterprise
        layer resolving against different cutoffs — the kind of drift nobody notices
        until the two locks disagree about a shared transitive dependency."""
        floors = {p.relative_to(REPO_ROOT).as_posix(): _declared_floor(p) for p, _ in PROJECTS}
        assert len(set(floors.values())) == 1, f"floors disagree: {floors}"

    @pytest.mark.parametrize(("pyproject", "lockfile"), PROJECTS, ids=lambda p: p.parent.name)
    def test_no_adjacent_uv_toml_suppresses_the_floor(
        self, pyproject: Path, lockfile: Path
    ) -> None:
        """An adjacent uv.toml wins over `[tool.uv]` and uv only warns about it, so this
        is a silent-removal path that leaves the pyproject.toml declaration in place and
        looking authoritative. If a uv.toml is ever genuinely needed, move the floor into
        it rather than deleting this assertion."""
        stray = pyproject.parent / "uv.toml"
        assert not stray.exists(), (
            f"{stray.relative_to(REPO_ROOT).as_posix()} exists; uv ignores the settings "
            "in [tool.uv] when it does, which silently drops exclude-newer"
        )
