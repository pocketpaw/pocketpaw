# tests/ee/sites/test_generator_client_supply_chain.py
# Created: 2026-09-12 (fix/sites-install-supply-chain-floor) — reproduce-first
# coverage for "the release-age floor is enforced on one of two build paths".
#
# THE BUG. ``daytona_runner`` uploads a ``bunfig.toml`` into every sandbox and
# displaces any the project emitted, so a Daytona build installs behind a 7-day
# ``minimumReleaseAge`` with ``ignoreScripts`` on. ``_SubprocessRunner.install``
# wrote NO bunfig at all — and that is the runner ``dev_server``, ``draft_markup``,
# ``service`` and the deployed Coolify image all use. ``deploy/coolify/Dockerfile``
# bakes bun plus the vendored generator and carries no bunfig anywhere, and the
# protections this workspace relies on live in a developer's home dir (``~/.bunfig.toml``),
# which a container inherits none of. So a production site build resolved from the
# open registry with lifecycle scripts ENABLED, as the backend user, in the container
# holding the app's secrets.
#
# That was survivable only because the installed dependency set is a short fixed list
# nobody outside the generator can influence. The design that lets a site's authoring
# agent request a package (docs/design/drafts/2026-09-12-sites-npm-dependency-allowlist.md)
# is what ends that, which is why this is its chunk 0 rather than adjacent cleanup.
#
# These tests exercise the REAL ``_SubprocessRunner`` and patch
# ``asyncio.create_subprocess_exec`` to a fast exit-0 child, so no bun is required.
# They mirror ``test_daytona_runner``'s bunfig tests deliberately: the same two
# questions (is a floor present, does a project-supplied one lose) asked of the OTHER
# runner, so a future reader comparing the two files sees one policy, not two.
from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import bun_supply_chain as bsc  # noqa: E402
from pocketpaw_ee.sites.generator_client import _SubprocessRunner  # noqa: E402

# A child that exits 0 immediately — stands in for a successful `bun install`.
_OK_ARGV = [sys.executable, "-c", "pass"]


def _spawn_observer(tmp_path, observed: dict):
    """Patch-in for ``create_subprocess_exec`` that snapshots the bunfig state at the
    exact moment the install would spawn, then runs a trivial exit-0 child instead.

    Snapshotting AT SPAWN is the point: a bunfig written after ``bun install`` has
    already started is not a floor, it is a file. Asserting on the post-call
    filesystem would pass for that bug.
    """
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        bunfig = tmp_path / bsc.BUILD_BUNFIG_REL
        observed["present"] = bunfig.is_file()
        observed["contents"] = bunfig.read_text(encoding="utf-8") if bunfig.is_file() else ""
        return await real_exec(
            *_OK_ARGV,
            stdout=kwargs.get("stdout", asyncio.subprocess.PIPE),
            stderr=kwargs.get("stderr", asyncio.subprocess.PIPE),
            cwd=kwargs.get("cwd"),
            start_new_session=kwargs.get("start_new_session", False),
        )

    return _fake_exec


@pytest.mark.asyncio
async def test_install_writes_the_supply_chain_bunfig_before_spawning(tmp_path, monkeypatch):
    """The reproduced bug: the local runner installed with no floor at all.

    THE MUTATION THAT BREAKS THIS: delete the ``_write_build_bunfig`` call from
    ``install``. The snapshot then reports no bunfig at spawn and this fails — which
    is precisely the state every Coolify-hosted publish was in before this change.
    """
    (tmp_path / "package.json").write_text('{"name":"paw-site-x"}', encoding="utf-8")

    observed: dict = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_observer(tmp_path, observed))

    ok, msg = await asyncio.wait_for(_SubprocessRunner().install(str(tmp_path)), timeout=10)
    assert ok is True, msg

    assert observed["present"] is True, (
        "bun install spawned with no bunfig.toml in the project dir — the local build "
        "path has no release-age floor and no ignoreScripts"
    )
    # Assert the two controls by VALUE, not by "a file exists". A bunfig that omits
    # either one is the same exposure wearing the right filename.
    assert "minimumReleaseAge = 604800" in observed["contents"]
    assert "ignoreScripts = true" in observed["contents"]


@pytest.mark.asyncio
async def test_a_project_supplied_bunfig_is_displaced_so_the_floor_wins(tmp_path, monkeypatch):
    """A generated project that emits its own bunfig must not be able to lower the floor.

    No generated project emits one today, so this is a guard against a future template
    change rather than a live hole — the same reason ``daytona_runner`` displaces rather
    than merges. The hostile file sets both controls to their OFF values, so a merge
    (or a skip-if-present) fails here while a displace passes.
    """
    (tmp_path / "package.json").write_text('{"name":"paw-site-x"}', encoding="utf-8")
    (tmp_path / bsc.BUILD_BUNFIG_REL).write_text(
        "[install]\nminimumReleaseAge = 0\nignoreScripts = false\n", encoding="utf-8"
    )

    observed: dict = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_observer(tmp_path, observed))

    ok, msg = await asyncio.wait_for(_SubprocessRunner().install(str(tmp_path)), timeout=10)
    assert ok is True, msg

    assert "minimumReleaseAge = 0" not in observed["contents"], (
        "the project's own bunfig survived into the install — a template change could "
        "switch the floor off"
    )
    assert "ignoreScripts = false" not in observed["contents"]
    assert "minimumReleaseAge = 604800" in observed["contents"]
    assert "ignoreScripts = true" in observed["contents"]


def test_the_floor_participates_in_the_install_cache_decision(tmp_path) -> None:
    """Introducing or changing the floor must invalidate a cached ``node_modules``.

    PERF-3 skips ``bun install`` when the fingerprint of the install inputs is
    unchanged. A ``node_modules`` installed with no floor is exactly as stale as one
    installed from a different package.json — but ``bunfig.toml`` declares no
    dependency, so the obvious input list leaves it out, and then introducing the
    floor moves no hash, skips the install, and reaches none of the already-built
    fleet. The fix is silent and total: everything stays green and nothing is floored.

    THE MUTATION THAT BREAKS THIS: drop ``BUILD_BUNFIG_REL`` from
    ``_INSTALL_INPUT_FILES``. All three hashes below collapse to one and this fails.
    """
    from pocketpaw_ee.sites.generator_client import _install_inputs_hash

    (tmp_path / "package.json").write_text('{"name":"paw-site-x"}', encoding="utf-8")
    unfloored = _install_inputs_hash(str(tmp_path))

    bsc.write_build_bunfig(tmp_path)
    floored = _install_inputs_hash(str(tmp_path))

    (tmp_path / bsc.BUILD_BUNFIG_REL).write_text(
        bsc.BUILD_BUNFIG.replace("604800", "60"), encoding="utf-8"
    )
    weakened = _install_inputs_hash(str(tmp_path))

    assert floored != unfloored, (
        "adding the supply-chain floor did not move the install fingerprint — every "
        "already-built site would keep its unfloored node_modules"
    )
    assert weakened != floored, "changing the floor's VALUE did not move the fingerprint"


def test_both_runners_enforce_one_policy_not_two() -> None:
    """The floor is one constant with two call sites, not two constants that agree today.

    ``daytona_runner`` re-exports these under its original ``SANDBOX_*`` names (the same
    re-export pattern ``sites_create`` uses for ``react_paths``), so nothing that imported
    them from there had to change. Identity, not equality: two separately-maintained
    strings that happen to match is the drift this asserts against.
    """
    from pocketpaw_ee.sites import daytona_runner as dr

    assert dr.SANDBOX_BUNFIG is bsc.BUILD_BUNFIG
    assert dr.SANDBOX_BUNFIG_REL is bsc.BUILD_BUNFIG_REL
