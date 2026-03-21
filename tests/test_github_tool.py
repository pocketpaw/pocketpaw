from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.tools.builtin.github import GitHubTool


@pytest.mark.asyncio
async def test_github_tool_get_repo():
    tool = GitHubTool()

    mock_repo_data = {
        "full_name": "pocketpaw/pocketpaw",
        "description": "Your AI agent in 30 seconds.",
        "stargazers_count": 1000,
        "forks_count": 100,
        "open_issues_count": 50,
        "html_url": "https://github.com/pocketpaw/pocketpaw",
    }

    with patch(
        "pocketpaw.integrations.github.GitHubClient.get_repo", new_callable=AsyncMock
    ) as mock_get_repo:
        mock_get_repo.return_value = mock_repo_data

        result = await tool.execute(action="get_repo", owner="pocketpaw", repo="pocketpaw")

        assert "pocketpaw/pocketpaw" in result
        assert "Stars: 1000" in result
        assert "html_url" not in result  # Should be formatted as URL: ...


@pytest.mark.asyncio
async def test_github_tool_list_issues():
    tool = GitHubTool()

    mock_issues = [
        {"number": 1, "title": "Issue 1", "user": {"login": "user1"}},
        {"number": 2, "title": "PR 1", "user": {"login": "user2"}, "pull_request": {}},
    ]

    with patch(
        "pocketpaw.integrations.github.GitHubClient.list_issues", new_callable=AsyncMock
    ) as mock_list:
        mock_list.return_value = mock_issues

        result = await tool.execute(action="list_issues", owner="pocketpaw", repo="pocketpaw")

        assert "#1: [Issue] Issue 1 (@user1)" in result
        assert "#2: [PR] PR 1 (@user2)" in result


@pytest.mark.asyncio
async def test_github_tool_get_issue():
    tool = GitHubTool()

    mock_issue_detail = {
        "number": 1,
        "title": "Title",
        "state": "open",
        "user": {"login": "author"},
        "html_url": "https://github.com/...",
        "body": "Body content",
        "comments_data": [{"user": {"login": "commenter"}, "body": "Comment text"}],
    }

    with patch(
        "pocketpaw.integrations.github.GitHubClient.get_issue", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = mock_issue_detail

        result = await tool.execute(
            action="get_issue", owner="pocketpaw", repo="pocketpaw", issue_number=1
        )

        assert "Issue #1: Title" in result
        assert "Body content" in result
        assert "@commenter: Comment text" in result


@pytest.mark.asyncio
async def test_github_tool_error_handling():
    tool = GitHubTool()

    with patch(
        "pocketpaw.integrations.github.GitHubClient.get_repo",
        side_effect=Exception("API limit exceeded"),
    ):
        result = await tool.execute(action="get_repo", owner="pocketpaw", repo="pocketpaw")
        assert "Error: GitHub API error: API limit exceeded" in result
