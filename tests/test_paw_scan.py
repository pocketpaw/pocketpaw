# Tests for paw module heuristic_scan and SCAN_PROMPT.
# Created: 2026-03-02
# Covers: heuristic_scan() with README.md, pyproject.toml, package.json,
#         .env.example; SCAN_PROMPT placeholder validation.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketpaw.paw.scan import SCAN_PROMPT, heuristic_scan

# ---------------------------------------------------------------------------
# Shared soul fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_soul():
    soul = MagicMock()
    soul.remember = AsyncMock(return_value="mem_id")
    return soul


# ---------------------------------------------------------------------------
# SCAN_PROMPT
# ---------------------------------------------------------------------------


class TestScanPrompt:
    def test_scan_prompt_has_project_path_placeholder(self):
        assert "{project_path}" in SCAN_PROMPT

    def test_scan_prompt_formats_without_error(self, tmp_path):
        formatted = SCAN_PROMPT.format(project_path=tmp_path)

        assert str(tmp_path) in formatted

    def test_scan_prompt_mentions_readme(self):
        assert "README" in SCAN_PROMPT

    def test_scan_prompt_mentions_pyproject(self):
        assert "pyproject.toml" in SCAN_PROMPT

    def test_scan_prompt_mentions_soul_remember(self):
        assert "soul_remember" in SCAN_PROMPT


# ---------------------------------------------------------------------------
# heuristic_scan — README
# ---------------------------------------------------------------------------


class TestHeuristicScanReadme:
    @pytest.mark.asyncio
    async def test_readme_md_content_stored_in_soul(self, tmp_path, mock_soul):
        (tmp_path / "README.md").write_text("# MyProject\nAwesome project.")

        await heuristic_scan(tmp_path, mock_soul)

        stored_facts = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("MyProject" in f for f in stored_facts)

    @pytest.mark.asyncio
    async def test_readme_rst_fallback(self, tmp_path, mock_soul):
        (tmp_path / "README.rst").write_text("MyRstProject docs")

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("MyRstProject" in f for f in stored)

    @pytest.mark.asyncio
    async def test_readme_md_takes_precedence_over_rst(self, tmp_path, mock_soul):
        (tmp_path / "README.md").write_text("MD content")
        (tmp_path / "README.rst").write_text("RST content")

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        # Only one README *content* fact should be stored — the first match (README.md).
        # The directory structure fact may also mention the filenames but won't have the content.
        readme_content_facts = [f for f in stored if "Project README" in f]
        assert len(readme_content_facts) == 1
        assert "MD content" in readme_content_facts[0]
        assert "RST content" not in readme_content_facts[0]

    @pytest.mark.asyncio
    async def test_no_readme_does_not_error(self, tmp_path, mock_soul):
        # No README at all — should not raise
        await heuristic_scan(tmp_path, mock_soul)

    @pytest.mark.asyncio
    async def test_readme_stored_with_high_importance(self, tmp_path, mock_soul):
        (tmp_path / "README.md").write_text("# Project")

        await heuristic_scan(tmp_path, mock_soul)

        importance_values = [
            call.kwargs.get("importance") for call in mock_soul.remember.call_args_list
        ]
        readme_importances = [
            imp
            for call, imp in zip(mock_soul.remember.call_args_list, importance_values)
            if "README" in call.args[0]
        ]
        assert all(imp >= 8 for imp in readme_importances)


# ---------------------------------------------------------------------------
# heuristic_scan — pyproject.toml
# ---------------------------------------------------------------------------


class TestHeuristicScanPyproject:
    @pytest.mark.asyncio
    async def test_pyproject_content_stored(self, tmp_path, mock_soul):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "coolapp"\n')

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("coolapp" in f for f in stored)

    @pytest.mark.asyncio
    async def test_pyproject_label_in_stored_fact(self, tmp_path, mock_soul):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("pyproject.toml" in f for f in stored)


# ---------------------------------------------------------------------------
# heuristic_scan — package.json
# ---------------------------------------------------------------------------


class TestHeuristicScanPackageJson:
    @pytest.mark.asyncio
    async def test_package_json_content_stored(self, tmp_path, mock_soul):
        (tmp_path / "package.json").write_text('{"name": "my-app", "version": "1.0.0"}')

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("my-app" in f for f in stored)

    @pytest.mark.asyncio
    async def test_package_json_label_in_stored_fact(self, tmp_path, mock_soul):
        (tmp_path / "package.json").write_text('{"name": "x"}')

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("package.json" in f for f in stored)


# ---------------------------------------------------------------------------
# heuristic_scan — .env.example
# ---------------------------------------------------------------------------


class TestHeuristicScanEnvExample:
    @pytest.mark.asyncio
    async def test_env_var_names_extracted(self, tmp_path, mock_soul):
        (tmp_path / ".env.example").write_text(
            "OPENAI_API_KEY=your-key-here\nDATABASE_URL=postgres://...\n"
        )

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        assert any("OPENAI_API_KEY" in f for f in stored)
        assert any("DATABASE_URL" in f for f in stored)

    @pytest.mark.asyncio
    async def test_commented_env_vars_ignored(self, tmp_path, mock_soul):
        (tmp_path / ".env.example").write_text("# This is a comment\nACTIVE_KEY=value\n")

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        env_facts = [f for f in stored if "ACTIVE_KEY" in f]
        assert len(env_facts) >= 1
        # The commented key should not appear as a var name
        comment_facts = [f for f in stored if "This is a comment" in f]
        assert not comment_facts

    @pytest.mark.asyncio
    async def test_no_env_example_does_not_error(self, tmp_path, mock_soul):
        await heuristic_scan(tmp_path, mock_soul)


# ---------------------------------------------------------------------------
# heuristic_scan — directory structure
# ---------------------------------------------------------------------------


class TestHeuristicScanDirectoryStructure:
    @pytest.mark.asyncio
    async def test_top_level_dirs_stored(self, tmp_path, mock_soul):
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        await heuristic_scan(tmp_path, mock_soul)

        stored = [call.args[0] for call in mock_soul.remember.call_args_list]
        dir_fact = " ".join(stored)
        assert "src" in dir_fact
        assert "tests" in dir_fact

    @pytest.mark.asyncio
    async def test_soul_remember_called_at_least_once(self, tmp_path, mock_soul):
        # Even in a minimal directory, structure fact should be stored
        await heuristic_scan(tmp_path, mock_soul)

        assert mock_soul.remember.call_count >= 1

    @pytest.mark.asyncio
    async def test_soul_remember_failure_does_not_raise(self, tmp_path, mock_soul):
        (tmp_path / "README.md").write_text("Test")
        mock_soul.remember = AsyncMock(side_effect=RuntimeError("storage error"))

        # Should complete without raising even if remember fails
        await heuristic_scan(tmp_path, mock_soul)
