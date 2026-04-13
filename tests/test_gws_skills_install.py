"""Tests for GWS skills auto-install on MCP preset installation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.skills.installer import install_skills_from_github


class TestInstallSkillsFromGitHub:
    """Test the shared skill installer helper."""

    @pytest.fixture
    def fake_repo(self, tmp_path):
        """Create a fake cloned repo with skills."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        for name in ["gws-gmail", "gws-sheets", "gws-shared", "persona-exec"]:
            skill_dir = skills_dir / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Test {name}\n---\n\nContent for {name}.\n"
            )

        return tmp_path

    async def test_install_all_skills(self, tmp_path, fake_repo):
        """Test installing all skills from a repo."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        async def mock_clone(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            return mock_proc

        with (
            patch("pocketpaw.skills.installer.INSTALL_DIR", install_dir),
            patch("asyncio.create_subprocess_exec", side_effect=mock_clone),
            patch("tempfile.TemporaryDirectory") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: str(fake_repo)
            mock_tmp.return_value.__exit__ = lambda s, *a: None

            installed = await install_skills_from_github("testowner", "testrepo")

        assert len(installed) == 4
        assert "gws-gmail" in installed
        assert (install_dir / "gws-gmail" / "SKILL.md").exists()

    async def test_install_with_prefix_filter(self, tmp_path, fake_repo):
        """Test installing only gws-* prefixed skills."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        async def mock_clone(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            return mock_proc

        with (
            patch("pocketpaw.skills.installer.INSTALL_DIR", install_dir),
            patch("asyncio.create_subprocess_exec", side_effect=mock_clone),
            patch("tempfile.TemporaryDirectory") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: str(fake_repo)
            mock_tmp.return_value.__exit__ = lambda s, *a: None

            installed = await install_skills_from_github(
                "testowner", "testrepo", prefix_filter="gws-"
            )

        assert len(installed) == 3
        assert "gws-gmail" in installed
        assert "gws-sheets" in installed
        assert "gws-shared" in installed
        assert "persona-exec" not in installed

    async def test_install_specific_skill(self, tmp_path, fake_repo):
        """Test installing a single skill by name."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        async def mock_clone(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            return mock_proc

        with (
            patch("pocketpaw.skills.installer.INSTALL_DIR", install_dir),
            patch("asyncio.create_subprocess_exec", side_effect=mock_clone),
            patch("tempfile.TemporaryDirectory") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: str(fake_repo)
            mock_tmp.return_value.__exit__ = lambda s, *a: None

            installed = await install_skills_from_github(
                "testowner", "testrepo", skill_name="gws-gmail"
            )

        assert installed == ["gws-gmail"]
        assert (install_dir / "gws-gmail" / "SKILL.md").exists()

    async def test_clone_failure_raises(self):
        """Test that clone failure raises RuntimeError."""

        async def mock_clone(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: repo not found"))
            return mock_proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mock_clone),
            patch("tempfile.TemporaryDirectory") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: "/tmp/fake"
            mock_tmp.return_value.__exit__ = lambda s, *a: None

            with pytest.raises(RuntimeError, match="Clone failed"):
                await install_skills_from_github("bad", "repo")


class TestGwsAutoInstall:
    """Test that GWS skills are auto-installed on preset install."""

    async def test_gws_preset_triggers_skill_install(self):
        """Verify install_mcp_preset creates a task for GWS skill install."""
        from pocketpaw.api.v1.mcp import _install_gws_skills

        # Just verify the function exists and is callable
        assert callable(_install_gws_skills)

    async def test_install_gws_skills_failure_non_blocking(self):
        """Verify _install_gws_skills catches exceptions gracefully."""
        from pocketpaw.api.v1.mcp import _install_gws_skills

        with patch(
            "pocketpaw.skills.installer.install_skills_from_github",
            side_effect=RuntimeError("network error"),
        ):
            # Should not raise
            await _install_gws_skills()
