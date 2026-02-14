import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

from pocketclaw.tools.builtin.github_repo_analyzer import (
    GitHubRepoAnalyzerTool,
)


# =========================================================
# Proper Mock Response Helper
# =========================================================

def make_mock_response(status=200, json_data=None, headers=None):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = json_data or {}

    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message="Error",
            request=None,
            response=response,
        )
    else:
        response.raise_for_status.return_value = None

    return response


# =========================================================
# TESTS
# =========================================================

@pytest.mark.asyncio
async def test_valid_repository():
    tool = GitHubRepoAnalyzerTool()

    repo_data = {
        "stargazers_count": 100,
        "forks_count": 20,
        "open_issues_count": 5,
        "language": "Python",
        "updated_at": "2026-01-01T00:00:00Z",
        "description": "Test repo",
        "default_branch": "main",
    }

    contributors_data = [{}, {}, {}]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [
            make_mock_response(200, repo_data),
            make_mock_response(200, contributors_data),
        ]

        result = await tool.execute(
            repo_url="https://github.com/testuser/testrepo"
        )

        assert "Repository Analysis" in result
        assert "Stars: 100" in result
        assert "Forks: 20" in result
        assert "Python" in result


@pytest.mark.asyncio
async def test_invalid_url_format():
    tool = GitHubRepoAnalyzerTool()
    result = await tool.execute(repo_url="not-a-url")
    assert "Invalid GitHub repository URL" in result


@pytest.mark.asyncio
async def test_non_github_url():
    tool = GitHubRepoAnalyzerTool()
    result = await tool.execute(repo_url="https://google.com/repo")
    assert "Invalid GitHub repository URL" in result


@pytest.mark.asyncio
async def test_repository_not_found():
    tool = GitHubRepoAnalyzerTool()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_mock_response(404)

        result = await tool.execute(
            repo_url="https://github.com/user/nonexistent"
        )

        assert "Repository not found" in result


@pytest.mark.asyncio
async def test_rate_limit_exceeded():
    tool = GitHubRepoAnalyzerTool()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_mock_response(403)

        result = await tool.execute(
            repo_url="https://github.com/user/repo"
        )

        assert "rate limit" in result.lower()


@pytest.mark.asyncio
async def test_network_error():
    tool = GitHubRepoAnalyzerTool()

    with patch(
        "httpx.AsyncClient.get",
        side_effect=httpx.RequestError("Network down"),
    ):
        result = await tool.execute(
            repo_url="https://github.com/user/repo"
        )

        assert "Network error" in result


@pytest.mark.asyncio
async def test_unexpected_exception():
    tool = GitHubRepoAnalyzerTool()

    with patch(
        "httpx.AsyncClient.get",
        side_effect=Exception("Boom"),
    ):
        result = await tool.execute(
            repo_url="https://github.com/user/repo"
        )

        assert "Boom" in result
