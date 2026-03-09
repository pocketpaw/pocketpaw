# Tests for GitHub integration

from unittest.mock import AsyncMock, patch


class TestGitHubToolSchemas:
    """Test GitHub tool properties and schemas."""

    def test_search_tool(self):
        from pocketpaw.tools.builtin.github import GitHubSearchTool

        tool = GitHubSearchTool()
        assert tool.name == "github_search"
        assert tool.trust_level == "standard"
        assert "query" in tool.parameters["properties"]
        assert "language" in tool.parameters["properties"]
        assert "sort" in tool.parameters["properties"]
        assert "query" in tool.parameters["required"]

    def test_issues_tool(self):
        from pocketpaw.tools.builtin.github import GitHubIssuesTool

        tool = GitHubIssuesTool()
        assert tool.name == "github_issues"
        assert tool.trust_level == "standard"
        assert "owner" in tool.parameters["properties"]
        assert "repo" in tool.parameters["properties"]
        assert "issue_number" in tool.parameters["properties"]
        assert "owner" in tool.parameters["required"]
        assert "repo" in tool.parameters["required"]

    def test_pulls_tool(self):
        from pocketpaw.tools.builtin.github import GitHubPullsTool

        tool = GitHubPullsTool()
        assert tool.name == "github_pulls"
        assert tool.trust_level == "standard"
        assert "owner" in tool.parameters["properties"]
        assert "repo" in tool.parameters["properties"]
        assert "pr_number" in tool.parameters["properties"]
        assert "owner" in tool.parameters["required"]
        assert "repo" in tool.parameters["required"]


class TestGitHubClientFormatters:
    """Test GitHubClient static format helpers."""

    def test_format_repo(self):
        from pocketpaw.integrations.github import GitHubClient

        repo = {
            "full_name": "pocketpaw/pocketpaw",
            "description": "AI agent that runs on your machine",
            "stargazers_count": 1500,
            "forks_count": 200,
            "language": "Python",
            "open_issues_count": 42,
            "html_url": "https://github.com/pocketpaw/pocketpaw",
            "updated_at": "2026-03-09T00:00:00Z",
            "topics": ["ai", "agent"],
        }
        result = GitHubClient._format_repo(repo)
        assert result["name"] == "pocketpaw/pocketpaw"
        assert result["stars"] == 1500
        assert result["language"] == "Python"
        assert "github.com" in result["url"]

    def test_format_repo_empty(self):
        from pocketpaw.integrations.github import GitHubClient

        result = GitHubClient._format_repo({})
        assert result["name"] == ""
        assert result["stars"] == 0
        assert result["language"] == ""

    def test_format_issue(self):
        from pocketpaw.integrations.github import GitHubClient

        issue = {
            "number": 123,
            "title": "Bug: agent crashes",
            "state": "open",
            "user": {"login": "testuser"},
            "labels": [{"name": "bug"}, {"name": "priority"}],
            "comments": 5,
            "created_at": "2026-03-01T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
            "html_url": "https://github.com/pocketpaw/pocketpaw/issues/123",
        }
        result = GitHubClient._format_issue(issue)
        assert result["number"] == 123
        assert result["title"] == "Bug: agent crashes"
        assert result["author"] == "testuser"
        assert "bug" in result["labels"]
        assert result["comments_count"] == 5

    def test_format_issue_deleted_user(self):
        from pocketpaw.integrations.github import GitHubClient

        result = GitHubClient._format_issue({"user": {}})
        assert result["author"] == "[deleted]"

    def test_format_pull_request(self):
        from pocketpaw.integrations.github import GitHubClient

        pr = {
            "number": 456,
            "title": "feat: add GitHub tool",
            "state": "open",
            "user": {"login": "contributor"},
            "labels": [{"name": "enhancement"}],
            "draft": False,
            "head": {"ref": "feat/github"},
            "base": {"ref": "dev"},
            "created_at": "2026-03-09T00:00:00Z",
            "updated_at": "2026-03-09T12:00:00Z",
            "html_url": "https://github.com/pocketpaw/pocketpaw/pull/456",
        }
        result = GitHubClient._format_pull_request(pr)
        assert result["number"] == 456
        assert result["head"] == "feat/github"
        assert result["base"] == "dev"
        assert result["draft"] is False


async def test_github_search_success():
    from pocketpaw.tools.builtin.github import GitHubSearchTool

    tool = GitHubSearchTool()

    mock_repos = [
        {
            "name": "pocketpaw/pocketpaw",
            "description": "AI agent that runs locally",
            "stars": 1500,
            "forks": 200,
            "language": "Python",
            "open_issues": 42,
            "url": "https://github.com/pocketpaw/pocketpaw",
            "updated_at": "2026-03-09T00:00:00Z",
            "topics": ["ai", "agent"],
        }
    ]

    with patch(
        "pocketpaw.integrations.github.GitHubClient.search_repos",
        new_callable=AsyncMock,
        return_value=mock_repos,
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(query="ai agent", language="python")

    assert "pocketpaw/pocketpaw" in result
    assert "1,500" in result


async def test_github_search_no_results():
    from pocketpaw.tools.builtin.github import GitHubSearchTool

    tool = GitHubSearchTool()

    with patch(
        "pocketpaw.integrations.github.GitHubClient.search_repos",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(query="xyznonexistent999")

    assert "No repositories found" in result


async def test_github_issues_list_success():
    from pocketpaw.tools.builtin.github import GitHubIssuesTool

    tool = GitHubIssuesTool()

    mock_issues = [
        {
            "number": 1,
            "title": "Bug report",
            "state": "open",
            "author": "user1",
            "labels": ["bug"],
            "comments_count": 3,
            "created_at": "2026-03-01T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
            "url": "https://github.com/test/repo/issues/1",
        }
    ]

    with patch(
        "pocketpaw.integrations.github.GitHubClient.list_issues",
        new_callable=AsyncMock,
        return_value=mock_issues,
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(owner="test", repo="repo")

    assert "Bug report" in result
    assert "#1" in result


async def test_github_issues_read_success():
    from pocketpaw.tools.builtin.github import GitHubIssuesTool

    tool = GitHubIssuesTool()

    mock_issue = {
        "number": 42,
        "title": "Feature request: GitHub tools",
        "state": "open",
        "author": "contributor",
        "labels": ["enhancement"],
        "comments_count": 5,
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-09T00:00:00Z",
        "url": "https://github.com/test/repo/issues/42",
        "body": "It would be great to have GitHub integration.",
        "comments": [
            {
                "author": "maintainer",
                "body": "Great idea! PRs welcome.",
                "created_at": "2026-03-02T00:00:00Z",
                "reactions": 5,
            },
        ],
    }

    with patch(
        "pocketpaw.integrations.github.GitHubClient.get_issue",
        new_callable=AsyncMock,
        return_value=mock_issue,
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(owner="test", repo="repo", issue_number=42)

    assert "Feature request" in result
    assert "GitHub integration" in result
    assert "Great idea" in result


async def test_github_issues_empty():
    from pocketpaw.tools.builtin.github import GitHubIssuesTool

    tool = GitHubIssuesTool()

    with patch(
        "pocketpaw.integrations.github.GitHubClient.list_issues",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(owner="test", repo="empty-repo")

    assert "No open issues" in result


async def test_github_pulls_list_success():
    from pocketpaw.tools.builtin.github import GitHubPullsTool

    tool = GitHubPullsTool()

    mock_pulls = [
        {
            "number": 10,
            "title": "feat: add new feature",
            "state": "open",
            "author": "dev1",
            "labels": ["feature"],
            "draft": False,
            "head": "feat/new-feature",
            "base": "dev",
            "created_at": "2026-03-08T00:00:00Z",
            "updated_at": "2026-03-09T00:00:00Z",
            "url": "https://github.com/test/repo/pull/10",
        }
    ]

    with patch(
        "pocketpaw.integrations.github.GitHubClient.list_pull_requests",
        new_callable=AsyncMock,
        return_value=mock_pulls,
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(owner="test", repo="repo")

    assert "add new feature" in result
    assert "#10" in result
    assert "feat/new-feature" in result


async def test_github_pulls_read_success():
    from pocketpaw.tools.builtin.github import GitHubPullsTool

    tool = GitHubPullsTool()

    mock_pr = {
        "number": 99,
        "title": "fix: memory leak",
        "state": "open",
        "author": "fixer",
        "labels": ["bugfix"],
        "draft": False,
        "head": "fix/memory-leak",
        "base": "main",
        "created_at": "2026-03-09T00:00:00Z",
        "updated_at": "2026-03-09T12:00:00Z",
        "url": "https://github.com/test/repo/pull/99",
        "body": "Fixes the WebSocket connection leak.",
        "additions": 15,
        "deletions": 3,
        "changed_files": 2,
        "merged": False,
        "comments": [
            {
                "author": "reviewer",
                "body": "Looks good, one nit.",
                "created_at": "2026-03-09T06:00:00Z",
                "reactions": 1,
            },
        ],
    }

    with patch(
        "pocketpaw.integrations.github.GitHubClient.get_pull_request",
        new_callable=AsyncMock,
        return_value=mock_pr,
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(owner="test", repo="repo", pr_number=99)

    assert "memory leak" in result
    assert "+15" in result
    assert "Looks good" in result


async def test_github_pulls_empty():
    from pocketpaw.tools.builtin.github import GitHubPullsTool

    tool = GitHubPullsTool()

    with patch(
        "pocketpaw.integrations.github.GitHubClient.list_pull_requests",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(owner="test", repo="repo")

    assert "No open pull requests" in result


async def test_github_search_error():
    from pocketpaw.tools.builtin.github import GitHubSearchTool

    tool = GitHubSearchTool()

    with patch(
        "pocketpaw.integrations.github.GitHubClient.search_repos",
        new_callable=AsyncMock,
        side_effect=Exception("API rate limit exceeded"),
    ):
        with patch("pocketpaw.integrations.github._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(query="test")

    assert result.startswith("Error:")
    assert "rate limit" in result


class TestGitHubToolPolicy:
    """Test that GitHub tools are registered in the policy system."""

    def test_github_group_exists(self):
        from pocketpaw.tools.policy import TOOL_GROUPS

        assert "group:github" in TOOL_GROUPS
        assert "github_search" in TOOL_GROUPS["group:github"]
        assert "github_issues" in TOOL_GROUPS["group:github"]
        assert "github_pulls" in TOOL_GROUPS["group:github"]

    def test_github_tools_allowed_in_full_profile(self):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="full")
        assert policy.is_tool_allowed("github_search")
        assert policy.is_tool_allowed("github_issues")
        assert policy.is_tool_allowed("github_pulls")

    def test_github_tools_can_be_denied(self):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="full", deny=["group:github"])
        assert not policy.is_tool_allowed("github_search")
        assert not policy.is_tool_allowed("github_issues")
        assert not policy.is_tool_allowed("github_pulls")
