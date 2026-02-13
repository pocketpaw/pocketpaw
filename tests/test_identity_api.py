"""Tests for Identity API — GET + PUT /api/identity.

Covers:
  - GET /api/identity returns all 4 identity files
  - PUT /api/identity saves edits to disk
  - PUT /api/identity partial update (only some files)
  - Agent picks up file changes on next prompt build

Created: 2026-02-12
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.bootstrap.default_provider import DefaultBootstrapProvider


class TestGetIdentity:
    """Tests for GET /api/identity."""

    async def test_returns_all_four_files(self):
        """GET /api/identity returns identity, soul, style, and user_file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            provider = DefaultBootstrapProvider(base_path=base)
            # Write known content
            (base / "IDENTITY.md").write_text("I am PocketPaw")
            (base / "SOUL.md").write_text("I value privacy")
            (base / "STYLE.md").write_text("Be concise")
            (base / "USER.md").write_text("Name: Alice")

            with (
                patch("pocketclaw.dashboard.get_config_path") as mock_path,
                patch(
                    "pocketclaw.dashboard.DefaultBootstrapProvider",
                    return_value=provider,
                ),
            ):
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import get_identity

                result = await get_identity()

            assert result["identity_file"] == "I am PocketPaw"
            assert result["soul_file"] == "I value privacy"
            assert result["style_file"] == "Be concise"
            assert result["user_file"] == "Name: Alice"

    async def test_returns_default_user_profile(self):
        """GET /api/identity returns default USER.md when not customized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            provider = DefaultBootstrapProvider(base_path=base)

            with (
                patch("pocketclaw.dashboard.get_config_path") as mock_path,
                patch(
                    "pocketclaw.dashboard.DefaultBootstrapProvider",
                    return_value=provider,
                ),
            ):
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import get_identity

                result = await get_identity()

            assert "user_file" in result
            assert "# User Profile" in result["user_file"]


class TestSaveIdentity:
    """Tests for PUT /api/identity."""

    async def test_saves_all_files(self):
        """PUT /api/identity writes all 4 files to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            identity_dir = base / "identity"
            identity_dir.mkdir()

            request = MagicMock()
            request.json = AsyncMock(
                return_value={
                    "identity_file": "New identity",
                    "soul_file": "New soul",
                    "style_file": "New style",
                    "user_file": "Name: Bob\nTimezone: EST",
                }
            )

            with patch("pocketclaw.dashboard.get_config_path") as mock_path:
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import save_identity

                result = await save_identity(request)

            assert result["ok"] is True
            assert set(result["updated"]) == {
                "IDENTITY.md",
                "SOUL.md",
                "STYLE.md",
                "USER.md",
            }
            assert (identity_dir / "IDENTITY.md").read_text() == "New identity"
            assert (identity_dir / "SOUL.md").read_text() == "New soul"
            assert (identity_dir / "STYLE.md").read_text() == "New style"
            assert (identity_dir / "USER.md").read_text() == "Name: Bob\nTimezone: EST"

    async def test_partial_update(self):
        """PUT /api/identity with only user_file updates only that file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            identity_dir = base / "identity"
            identity_dir.mkdir()
            (identity_dir / "IDENTITY.md").write_text("Original identity")
            (identity_dir / "USER.md").write_text("Name: Old")

            request = MagicMock()
            request.json = AsyncMock(
                return_value={
                    "user_file": "Name: Updated",
                }
            )

            with patch("pocketclaw.dashboard.get_config_path") as mock_path:
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import save_identity

                result = await save_identity(request)

            assert result["ok"] is True
            assert result["updated"] == ["USER.md"]
            # Original identity untouched
            assert (identity_dir / "IDENTITY.md").read_text() == "Original identity"
            # User file updated
            assert (identity_dir / "USER.md").read_text() == "Name: Updated"

    async def test_ignores_non_string_values(self):
        """PUT /api/identity ignores non-string values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            identity_dir = base / "identity"
            identity_dir.mkdir()

            request = MagicMock()
            request.json = AsyncMock(
                return_value={
                    "identity_file": 42,  # not a string
                    "soul_file": "Valid soul",
                }
            )

            with patch("pocketclaw.dashboard.get_config_path") as mock_path:
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import save_identity

                result = await save_identity(request)

            assert result["ok"] is True
            assert result["updated"] == ["SOUL.md"]

    async def test_creates_identity_dir_if_missing(self):
        """PUT /api/identity creates the identity/ directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            request = MagicMock()
            request.json = AsyncMock(
                return_value={"user_file": "Name: New User"}
            )

            with patch("pocketclaw.dashboard.get_config_path") as mock_path:
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import save_identity

                result = await save_identity(request)

            assert result["ok"] is True
            assert (base / "identity" / "USER.md").read_text() == "Name: New User"

    async def test_ignores_unknown_keys(self):
        """PUT /api/identity ignores keys not in the file_map."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            identity_dir = base / "identity"
            identity_dir.mkdir()

            request = MagicMock()
            request.json = AsyncMock(
                return_value={
                    "user_file": "Name: Valid",
                    "malicious_key": "should be ignored",
                }
            )

            with patch("pocketclaw.dashboard.get_config_path") as mock_path:
                mock_path.return_value = base / "config.json"
                from pocketclaw.dashboard import save_identity

                result = await save_identity(request)

            assert result["updated"] == ["USER.md"]
            assert not (identity_dir / "malicious_key").exists()


class TestIdentityAgentIntegration:
    """Tests verifying that saved identity changes are picked up by the agent."""

    async def test_saved_user_profile_in_system_prompt(self):
        """After saving USER.md, the next get_context() picks it up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            provider = DefaultBootstrapProvider(base_path=base)

            # Initially has default content
            ctx = await provider.get_context()
            assert "(your name)" in ctx.user_profile

            # Simulate saving via API (write directly)
            (base / "USER.md").write_text("Name: Charlie\nTimezone: UTC+5")

            # Next call picks up the change
            ctx2 = await provider.get_context()
            assert ctx2.user_profile == "Name: Charlie\nTimezone: UTC+5"
            assert "Name: Charlie" in ctx2.to_system_prompt()

    async def test_saved_identity_in_system_prompt(self):
        """After saving IDENTITY.md, the next get_context() picks it up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            provider = DefaultBootstrapProvider(base_path=base)

            (base / "IDENTITY.md").write_text("I am a custom agent named Luna.")

            ctx = await provider.get_context()
            assert ctx.identity == "I am a custom agent named Luna."
            assert "Luna" in ctx.to_system_prompt()

    async def test_all_files_in_system_prompt(self):
        """All 4 identity files appear in the system prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            provider = DefaultBootstrapProvider(base_path=base)

            (base / "IDENTITY.md").write_text("CUSTOM_IDENTITY")
            (base / "SOUL.md").write_text("CUSTOM_SOUL")
            (base / "STYLE.md").write_text("CUSTOM_STYLE")
            (base / "USER.md").write_text("CUSTOM_USER")

            ctx = await provider.get_context()
            prompt = ctx.to_system_prompt()
            assert "CUSTOM_IDENTITY" in prompt
            assert "CUSTOM_SOUL" in prompt
            assert "CUSTOM_STYLE" in prompt
            assert "CUSTOM_USER" in prompt
