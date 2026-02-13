# Tests for Feature 6: CreateSkillTool
# Created: 2026-02-06

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketclaw.tools.builtin.skill_gen import _VALID_SKILL_NAME, CreateSkillTool


@pytest.fixture
def tool():
    return CreateSkillTool()


@pytest.fixture
def temp_skills_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestCreateSkillTool:
    """Tests for CreateSkillTool."""

    def test_name(self, tool):
        assert tool.name == "create_skill"

    def test_trust_level(self, tool):
        assert tool.trust_level == "high"

    def test_parameters_schema(self, tool):
        params = tool.parameters
        assert "skill_name" in params["properties"]
        assert "description" in params["properties"]
        assert "instructions" in params["properties"]
        assert "allowed_tools" in params["properties"]
        assert "user_invocable" in params["properties"]

    def test_valid_skill_names(self):
        assert _VALID_SKILL_NAME.match("my-skill")
        assert _VALID_SKILL_NAME.match("summarize_pr")
        assert _VALID_SKILL_NAME.match("a")
        assert _VALID_SKILL_NAME.match("test123")

    def test_invalid_skill_names(self):
        assert not _VALID_SKILL_NAME.match("")
        assert not _VALID_SKILL_NAME.match("My-Skill")  # uppercase
        assert not _VALID_SKILL_NAME.match("123start")  # starts with number
        assert not _VALID_SKILL_NAME.match("has space")
        assert not _VALID_SKILL_NAME.match("-starts-with-dash")

    @patch("pocketclaw.tools.builtin.skill_gen._get_skills_dir")
    async def test_create_skill_success(self, mock_dir, tool, temp_skills_dir):
        mock_dir.return_value = temp_skills_dir

        with patch("pocketclaw.skills.get_skill_loader", side_effect=ImportError):
            result = await tool.execute(
                skill_name="test-skill",
                description="A test skill",
                instructions="Do the thing.\nStep 1.\nStep 2.",
            )

        assert "created successfully" in result
        skill_file = temp_skills_dir / "test-skill" / "SKILL.md"
        assert skill_file.exists()

        content = skill_file.read_text()
        assert "---" in content
        assert "name: test-skill" in content
        assert "description: A test skill" in content
        assert "user-invocable: true" in content
        assert "Do the thing." in content

    @patch("pocketclaw.tools.builtin.skill_gen._get_skills_dir")
    async def test_create_skill_with_allowed_tools(self, mock_dir, tool, temp_skills_dir):
        mock_dir.return_value = temp_skills_dir

        with patch("pocketclaw.skills.get_skill_loader", side_effect=ImportError):
            result = await tool.execute(
                skill_name="code-review",
                description="Review code",
                instructions="Review the code changes.",
                allowed_tools=["read_file", "shell"],
            )

        assert "created successfully" in result
        content = (temp_skills_dir / "code-review" / "SKILL.md").read_text()
        assert "allowed-tools:" in content
        assert "  - read_file" in content
        assert "  - shell" in content

    @patch("pocketclaw.tools.builtin.skill_gen._get_skills_dir")
    async def test_create_skill_not_user_invocable(self, mock_dir, tool, temp_skills_dir):
        mock_dir.return_value = temp_skills_dir

        with patch("pocketclaw.skills.get_skill_loader", side_effect=ImportError):
            result = await tool.execute(
                skill_name="internal-skill",
                description="Internal only",
                instructions="Do internal stuff.",
                user_invocable=False,
            )

        assert "created successfully" in result
        content = (temp_skills_dir / "internal-skill" / "SKILL.md").read_text()
        assert "user-invocable: false" in content

    async def test_invalid_skill_name_rejected(self, tool):
        result = await tool.execute(
            skill_name="Invalid Name!",
            description="Bad name",
            instructions="Nope.",
        )
        assert "Error" in result
        assert "Invalid skill name" in result

    @patch("pocketclaw.tools.builtin.skill_gen._get_skills_dir")
    async def test_overwrite_protection(self, mock_dir, tool, temp_skills_dir):
        mock_dir.return_value = temp_skills_dir

        # Pre-create the skill
        skill_dir = temp_skills_dir / "existing-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("existing content")

        result = await tool.execute(
            skill_name="existing-skill",
            description="Overwrite attempt",
            instructions="Should fail.",
        )

        assert "Error" in result
        assert "already exists" in result
        # Original content preserved
        assert (skill_dir / "SKILL.md").read_text() == "existing content"

    @patch("pocketclaw.tools.builtin.skill_gen._get_skills_dir")
    async def test_skill_loader_reload_called(self, mock_dir, tool, temp_skills_dir):
        mock_dir.return_value = temp_skills_dir

        mock_loader = MagicMock()
        with patch("pocketclaw.skills.get_skill_loader", return_value=mock_loader):
            await tool.execute(
                skill_name="reloaded-skill",
                description="Test reload",
                instructions="Content.",
            )

        mock_loader.reload.assert_called_once()

    @patch("pocketclaw.tools.builtin.skill_gen._get_skills_dir")
    async def test_yaml_frontmatter_format(self, mock_dir, tool, temp_skills_dir):
        mock_dir.return_value = temp_skills_dir

        with patch("pocketclaw.skills.get_skill_loader", side_effect=ImportError):
            await tool.execute(
                skill_name="fmt-test",
                description="Format test",
                instructions="Instruction body here.",
            )

        content = (temp_skills_dir / "fmt-test" / "SKILL.md").read_text()
        # Check frontmatter delimiters
        parts = content.split("---")
        assert len(parts) >= 3  # before, frontmatter, after
