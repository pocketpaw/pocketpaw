# Tests for Feature 2: USER.md user profile in bootstrap
# Created: 2026-02-06

import tempfile
from pathlib import Path

import pytest

from pocketclaw.bootstrap.default_provider import DefaultBootstrapProvider
from pocketclaw.bootstrap.protocol import BootstrapContext


@pytest.fixture
def temp_identity_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestUserProfile:
    """Tests for USER.md in bootstrap system."""

    def test_user_profile_field_default_empty(self):
        ctx = BootstrapContext(name="Test", identity="id", soul="soul", style="style")
        assert ctx.user_profile == ""

    def test_user_profile_not_in_prompt_when_empty(self):
        ctx = BootstrapContext(name="Test", identity="id", soul="soul", style="style")
        prompt = ctx.to_system_prompt()
        assert "# User Profile" not in prompt

    def test_user_profile_in_prompt_when_set(self):
        ctx = BootstrapContext(
            name="Test",
            identity="id",
            soul="soul",
            style="style",
            user_profile="Name: Alice\nTimezone: PST",
        )
        prompt = ctx.to_system_prompt()
        assert "# User Profile" in prompt
        assert "Name: Alice" in prompt
        assert "Timezone: PST" in prompt

    async def test_user_md_created_by_default(self, temp_identity_path):
        DefaultBootstrapProvider(base_path=temp_identity_path)
        user_file = temp_identity_path / "USER.md"
        assert user_file.exists()
        content = user_file.read_text()
        assert "# User Profile" in content
        assert "Name:" in content
        assert "Timezone:" in content

    async def test_user_md_not_overwritten(self, temp_identity_path):
        # Pre-create USER.md with custom content
        (temp_identity_path / "USER.md").write_text("Name: Bob")
        DefaultBootstrapProvider(base_path=temp_identity_path)
        content = (temp_identity_path / "USER.md").read_text()
        assert content == "Name: Bob"

    async def test_user_profile_loaded_into_context(self, temp_identity_path):
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)
        # Overwrite with custom profile
        (temp_identity_path / "USER.md").write_text("Name: Charlie\nTimezone: UTC+5")
        ctx = await provider.get_context()
        assert ctx.user_profile == "Name: Charlie\nTimezone: UTC+5"

    async def test_user_profile_in_system_prompt(self, temp_identity_path):
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)
        (temp_identity_path / "USER.md").write_text("Name: Charlie")
        ctx = await provider.get_context()
        prompt = ctx.to_system_prompt()
        assert "# User Profile" in prompt
        assert "Name: Charlie" in prompt

    async def test_missing_user_md_no_error(self, temp_identity_path):
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)
        # Delete the USER.md that was created by default
        (temp_identity_path / "USER.md").unlink()
        ctx = await provider.get_context()
        assert ctx.user_profile == ""
        prompt = ctx.to_system_prompt()
        assert "# User Profile" not in prompt
