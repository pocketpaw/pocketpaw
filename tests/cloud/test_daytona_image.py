# tests/cloud/test_daytona_image.py — pins the Daytona sandbox image's toolchain
# contract (ee/pocketpaw_ee/cloud/daytona/image.py).
#
# Created 2026-08-09 (SG-9i). The image was previously untested, which is how it came
# to ship Node without bun while the Paw Sites build pipeline is entirely bun-shaped
# (``bun install`` + ``bun run build``). A sandbox missing bun does not fail at build
# time with a useful message — it fails per-build, in the sandbox, as a command-not-
# found buried in a build log.
#
# These are dockerfile-TEXT assertions, deliberately. Actually building the image needs
# Docker and a Daytona account, so it cannot run in unit tests; the dockerfile is the
# closest artifact to the contract that is cheap to assert. It catches the regression
# that matters (a toolchain silently dropped from the image) without pretending to
# verify the built result.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.daytona.image import (
    build_paw_dev_image,
    resolve_sandbox_image,
    sandbox_image_override,
)


@pytest.fixture
def dockerfile() -> str:
    return build_paw_dev_image().dockerfile()


class TestToolchainIsPresent:
    def test_bun_is_installed(self, dockerfile: str) -> None:
        """The load-bearing one. Removing the bun block makes every source-lane build
        fail inside the sandbox with a command-not-found."""
        assert "bun.sh/install" in dockerfile

    def test_bun_is_on_path_for_non_login_shells(self, dockerfile: str) -> None:
        """``execute_command`` does not run a login shell, so a bun that only exists in
        ``~/.bashrc``'s PATH is invisible to it. The symlink is what makes it reachable —
        this is the difference between "bun is installed" and "bun can be run"."""
        assert "/usr/local/bin/bun" in dockerfile

    def test_bun_install_is_verified_at_image_build_time(self, dockerfile: str) -> None:
        """``bun --version`` in the image build turns a broken install into a failed
        IMAGE build (one loud failure) instead of a failed build per site (many quiet
        ones)."""
        assert "bun --version" in dockerfile

    def test_node_is_still_installed(self, dockerfile: str) -> None:
        """bun was ADDED, not substituted: the generated project's build script invokes
        ``node`` directly (``vite build && node scripts/prune-client.mjs``), and the
        vendored paw-sites CLI runs under node."""
        assert "nodesource.com" in dockerfile

    def test_python_base_is_retained(self, dockerfile: str) -> None:
        """The in-sandbox build wrapper serializes its result sentinel with python3
        rather than hand-rolled shell JSON. Losing the python base would silently break
        the sentinel — and a missing sentinel is read as infrastructure loss, so the
        lane would retry forever instead of reporting anything."""
        assert "python" in dockerfile.lower()


class TestOverrideContract:
    def test_unset_uses_the_prebuilt_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DAYTONA_SANDBOX_IMAGE", raising=False)
        assert sandbox_image_override() is None

    @pytest.mark.parametrize("value", ["standard", "default", "STANDARD", "  "])
    def test_standard_and_blank_mean_the_prebuilt_image(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("DAYTONA_SANDBOX_IMAGE", value)
        assert sandbox_image_override() is None

    def test_an_explicit_image_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator override is honoured — but note it replaces the WHOLE image, so
        pointing this at e.g. ``oven/bun`` would strip python/uv/git that other Daytona
        consumers rely on. It must not be set globally to get bun; bun is in the
        prebuilt image for exactly that reason."""
        monkeypatch.setenv("DAYTONA_SANDBOX_IMAGE", "oven/bun:1.2")
        assert sandbox_image_override() == "oven/bun:1.2"

    def test_resolver_returns_an_image_object_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DAYTONA_SANDBOX_IMAGE", raising=False)
        resolved = resolve_sandbox_image()
        assert not isinstance(resolved, str)

    def test_resolver_returns_the_string_when_overridden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DAYTONA_SANDBOX_IMAGE", "python:3.12-slim")
        assert resolve_sandbox_image() == "python:3.12-slim"
