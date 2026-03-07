"""
Tests for the Skills module.
"""

from pathlib import Path

import pytest

from pocketpaw.skills.loader import (
    Skill,
    SkillLoader,
    parse_skill_md,
)


class TestSkillParsing:
    """Test SKILL.md parsing."""

    def test_parse_basic_skill(self, tmp_path):
        """Test parsing a basic SKILL.md file."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("""---
name: test-skill
description: A test skill for unit testing
---

# Test Skill

This is the skill content.

Use it like this: `/test-skill arg1 arg2`
""")

        skill = parse_skill_md(skill_md)

        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill for unit testing"
        assert "# Test Skill" in skill.content
        assert skill.user_invocable is True

    def test_parse_skill_with_options(self, tmp_path):
        """Test parsing skill with optional frontmatter fields."""
        skill_md = tmp_path / "advanced-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("""---
name: advanced-skill
description: An advanced skill
user-invocable: false
disable-model-invocation: true
argument-hint: "[filename]"
---

Advanced content here.
""")

        skill = parse_skill_md(skill_md)

        assert skill is not None
        assert skill.name == "advanced-skill"
        assert skill.user_invocable is False
        assert skill.disable_model_invocation is True
        assert skill.argument_hint == "[filename]"

    def test_parse_skill_no_frontmatter(self, tmp_path):
        """Test parsing skill without frontmatter returns None."""
        skill_md = tmp_path / "bad-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("Just content, no frontmatter")

        skill = parse_skill_md(skill_md)

        assert skill is None

    def test_parse_skill_fallback_name(self, tmp_path):
        """Test skill name falls back to directory name."""
        skill_md = tmp_path / "my-custom-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("""---
description: Skill without name field
---

Content here.
""")

        skill = parse_skill_md(skill_md)

        assert skill is not None
        assert skill.name == "my-custom-skill"


class TestSkillPromptBuilding:
    """Test prompt building with argument substitution."""

    def test_build_prompt_no_args(self):
        """Test building prompt without arguments."""
        skill = Skill(
            name="test",
            description="Test skill",
            content="Do something useful.",
            path=Path("/tmp/test/SKILL.md"),
        )

        prompt = skill.build_prompt()

        assert prompt == "Do something useful."

    def test_build_prompt_with_arguments(self):
        """Test $ARGUMENTS substitution."""
        skill = Skill(
            name="test",
            description="Test skill",
            content="Process: $ARGUMENTS",
            path=Path("/tmp/test/SKILL.md"),
        )

        prompt = skill.build_prompt("hello world")

        assert prompt == "Process: hello world"

    def test_build_prompt_positional_args(self):
        """Test $0, $1, $2 substitution."""
        skill = Skill(
            name="test",
            description="Test skill",
            content="First: $0, Second: $1, Third: $2",
            path=Path("/tmp/test/SKILL.md"),
        )

        prompt = skill.build_prompt("apple banana cherry")

        assert prompt == "First: apple, Second: banana, Third: cherry"


class TestSkillLoader:
    """Test SkillLoader."""

    @pytest.fixture
    def loader_with_temp_path(self, tmp_path):
        """Create loader with temporary skill path."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create a test skill
        skill_dir = skills_dir / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test-skill
description: Test skill
---

Test content.
""")

        return SkillLoader(extra_paths=[skills_dir])

    def test_load_skills(self, loader_with_temp_path):
        """Test loading skills from paths."""
        loader = loader_with_temp_path
        skills = loader.load()

        assert "test-skill" in skills
        assert skills["test-skill"].description == "Test skill"

    def test_get_skill_by_name(self, loader_with_temp_path):
        """Test getting skill by name."""
        loader = loader_with_temp_path

        skill = loader.get("test-skill")

        assert skill is not None
        assert skill.name == "test-skill"

    def test_get_nonexistent_skill(self, loader_with_temp_path):
        """Test getting nonexistent skill returns None."""
        loader = loader_with_temp_path

        skill = loader.get("nonexistent")

        assert skill is None

    def test_list_skill_names(self, loader_with_temp_path):
        """Test listing skill names."""
        loader = loader_with_temp_path

        names = loader.list_names()

        assert "test-skill" in names

    def test_reload_skills(self, loader_with_temp_path, tmp_path):
        """Test reloading skills picks up changes."""
        loader = loader_with_temp_path

        # Initial load
        skills = loader.load()
        assert len(skills) >= 1

        # Add another skill
        skills_dir = tmp_path / "skills"
        new_skill_dir = skills_dir / "new-skill"
        new_skill_dir.mkdir()
        (new_skill_dir / "SKILL.md").write_text("""---
name: new-skill
description: New skill added
---

New content.
""")

        # Reload
        skills = loader.reload()

        assert "new-skill" in skills


class TestSkillLoaderIntegration:
    """Integration tests with real skill paths."""

    def test_loads_from_agents_skills(self):
        """Test loading from ~/.agents/skills/ if it exists."""
        agents_path = Path.home() / ".agents" / "skills"

        if not agents_path.exists():
            pytest.skip("~/.agents/skills/ does not exist")

        loader = SkillLoader()
        skills = loader.load()

        # Should find at least one skill if the directory exists
        assert len(skills) >= 0  # May be empty but shouldn't error
