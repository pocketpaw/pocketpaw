import pytest
from pocketclaw.tools.builtin.github_repo_analyzer import GitHubRepoAnalyzerTool


@pytest.mark.asyncio
async def test_invalid_url():
    tool = GitHubRepoAnalyzerTool()
    result = await tool.execute(repo_url="invalid-url")
    assert "Error" in result

@pytest.mark.asyncio
async def test_valid_format_url_only_parsing():
    tool = GitHubRepoAnalyzerTool()
    # This only checks parsing format, not actual API
    result = await tool.execute(repo_url="https://github.com/test/test")
    # It may fail API call but should not crash
    assert isinstance(result, str)