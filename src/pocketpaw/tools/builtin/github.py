# GitHub tool — interact with GitHub repositories, issues, and PRs.
# Created: 2026-03-22

import logging
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.integrations.github import GitHubClient
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class GitHubTool(BaseTool):
    """Tool for interacting with GitHub repositories and issues."""

    @property
    def name(self) -> str:
        return "github"

    @property
    def description(self) -> str:
        return (
            "Interact with GitHub. Supported actions:\n"
            "- 'get_repo': Get repository information (stars, forks, description).\n"
            "- 'list_issues': List open issues and pull requests.\n"
            "- 'get_issue': Get details and comments for a specific issue or PR.\n"
            "Requires 'owner' and 'repo' for all actions. 'issue_number' required for 'get_issue'."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform: 'get_repo', 'list_issues', 'get_issue'",
                    "enum": ["get_repo", "list_issues", "get_issue"],
                },
                "owner": {
                    "type": "string",
                    "description": "Repository owner (e.g., 'pocketpaw')",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository name (e.g., 'pocketpaw')",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "Issue or Pull Request number (required for 'get_issue')",
                },
            },
            "required": ["action", "owner", "repo"],
        }

    async def execute(self, action: str, owner: str, repo: str, **params: Any) -> str:
        """Execute the GitHub action."""
        settings = get_settings()
        client = GitHubClient(token=settings.github_api_token)

        try:
            if action == "get_repo":
                data = await client.get_repo(owner, repo)
                return self._format_repo(data)

            elif action == "list_issues":
                data = await client.list_issues(owner, repo)
                return self._format_issues(owner, repo, data)

            elif action == "get_issue":
                issue_num = params.get("issue_number")
                if not issue_num:
                    return self._error(
                        "Parameter 'issue_number' is required for action 'get_issue'"
                    )
                data = await client.get_issue(owner, repo, issue_num)
                return self._format_issue_detail(data)

            else:
                return self._error(f"Unknown action: {action}")

        except Exception as e:
            logger.error(f"GitHub tool error: {e}")
            return self._error(f"GitHub API error: {str(e)}")

    def _format_repo(self, data: dict) -> str:
        return (
            f"**GitHub Repository: {data.get('full_name')}**\n"
            f"Description: {data.get('description', 'No description')}\n"
            f"Stars: {data.get('stargazers_count')} | Forks: {data.get('forks_count')} | "
            f"Open Issues: {data.get('open_issues_count')}\n"
            f"URL: {data.get('html_url')}"
        )

    def _format_issues(self, owner: str, repo: str, issues: list) -> str:
        if not issues:
            return f"No open issues found in {owner}/{repo}."

        lines = [f"**Open Issues in {owner}/{repo}:**\n"]
        for issue in issues:
            type_label = "[PR]" if "pull_request" in issue else "[Issue]"
            login = issue.get("user", {}).get("login")
            lines.append(
                f"- #{issue.get('number')}: {type_label} {issue.get('title')} (@{login})"
            )

        return "\n".join(lines)

    def _format_issue_detail(self, data: dict) -> str:
        type_label = "Pull Request" if "pull_request" in data else "Issue"
        body = data.get("body", "No description provided.")
        if len(body) > 1000:
            body = body[:1000] + "..."

        res = (
            f"**{type_label} #{data.get('number')}: {data.get('title')}**\n"
            f"Status: {data.get('state')} | Author: @{data.get('user', {}).get('login')}\n"
            f"URL: {data.get('html_url')}\n\n"
            f"{body}\n"
        )

        comments = data.get("comments_data", [])
        if comments:
            res += "\n---\n**Top Comments:**\n"
            for c in comments[:3]:
                c_body = c.get("body", "")[:200]
                res += f"- @{c.get('user', {}).get('login')}: {c_body}...\n"

        return res
