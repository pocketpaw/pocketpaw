# GitHub tools — search repos, issues, pull requests.
# Created: 2026-03-09

import logging
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class GitHubSearchTool(BaseTool):
    """Search GitHub repositories."""

    @property
    def name(self) -> str:
        return "github_search"

    @property
    def description(self) -> str:
        return (
            "Search GitHub repositories by query. Can filter by programming language "
            "and sort by stars, forks, or recent updates. "
            "Works without API key for public repos."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g. 'machine learning', 'todo app react')",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by programming language (optional, e.g. 'python', 'rust')",
                },
                "sort": {
                    "type": "string",
                    "description": "Sort by: best_match, stars, forks, updated (default: best_match)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 30)",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        language: str | None = None,
        sort: str = "best_match",
        limit: int = 10,
    ) -> str:
        try:
            from pocketpaw.integrations.github import GitHubClient

            client = GitHubClient()
            repos = await client.search_repos(query, sort=sort, language=language, limit=limit)

            if not repos:
                lang_str = f" ({language})" if language else ""
                return f"No repositories found for '{query}'{lang_str}."

            lang_str = f" ({language})" if language else ""
            lines = [f"GitHub repositories for '{query}'{lang_str}:\n"]
            for i, r in enumerate(repos, 1):
                desc = r["description"][:100] if r["description"] else "No description"
                lang = f" [{r['language']}]" if r["language"] else ""
                topics = f" Topics: {', '.join(r['topics'][:5])}" if r.get("topics") else ""
                lines.append(
                    f"{i}. **{r['name']}**{lang}\n"
                    f"   ⭐ {r['stars']:,} | 🍴 {r['forks']:,} | "
                    f"Issues: {r['open_issues']:,}\n"
                    f"   {desc}{topics}\n"
                    f"   {r['url']}"
                )
            return "\n".join(lines)

        except Exception as e:
            return self._error(f"GitHub search failed: {e}")


class GitHubIssuesTool(BaseTool):
    """List or read GitHub issues."""

    @property
    def name(self) -> str:
        return "github_issues"

    @property
    def description(self) -> str:
        return (
            "List issues from a GitHub repository, or read a specific issue with comments. "
            "Provide owner and repo to list issues, add issue_number to read a specific one."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Repository owner (user or organization, e.g. 'facebook')",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository name (e.g. 'react')",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "Specific issue number to read with comments (optional)",
                },
                "state": {
                    "type": "string",
                    "description": "Filter: open, closed, all (default: open)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results when listing (default 10, max 30)",
                },
            },
            "required": ["owner", "repo"],
        }

    async def execute(
        self,
        owner: str,
        repo: str,
        issue_number: int | None = None,
        state: str = "open",
        limit: int = 10,
    ) -> str:
        try:
            from pocketpaw.integrations.github import GitHubClient

            client = GitHubClient()

            # Read a specific issue
            if issue_number is not None:
                issue = await client.get_issue(owner, repo, issue_number)

                labels = f" [{', '.join(issue['labels'])}]" if issue.get("labels") else ""
                lines = [
                    f"**#{issue['number']} {issue['title']}** ({issue['state']}){labels}",
                    f"by @{issue['author']} | "
                    f"Comments: {issue['comments_count']} | "
                    f"Created: {issue['created_at'][:10]}",
                ]

                body = issue.get("body", "")
                if body:
                    lines.append(f"\n{body}")

                comments = issue.get("comments", [])
                if comments:
                    lines.append(f"\n**Top {len(comments)} comments:**\n")
                    for c in comments:
                        body = c.get("body", "")[:300]
                        lines.append(
                            f"- **@{c.get('author', '[deleted]')}** "
                            f"({c.get('created_at', '')[:10]}): {body}"
                        )

                lines.append(f"\n{issue['url']}")
                return "\n".join(lines)

            # List issues
            issues = await client.list_issues(owner, repo, state=state, limit=limit)

            if not issues:
                return f"No {state} issues found in {owner}/{repo}."

            lines = [f"Issues in **{owner}/{repo}** ({state}):\n"]
            for i, iss in enumerate(issues, 1):
                labels = f" [{', '.join(iss['labels'][:3])}]" if iss.get("labels") else ""
                lines.append(
                    f"{i}. **#{iss['number']} {iss['title']}**{labels}\n"
                    f"   by @{iss['author']} | "
                    f"Comments: {iss['comments_count']} | "
                    f"Updated: {iss['updated_at'][:10]}"
                )
            return "\n".join(lines)

        except Exception as e:
            return self._error(f"GitHub issues failed: {e}")


class GitHubPullsTool(BaseTool):
    """List or read GitHub pull requests."""

    @property
    def name(self) -> str:
        return "github_pulls"

    @property
    def description(self) -> str:
        return (
            "List pull requests from a GitHub repository, or read a specific PR with comments. "
            "Provide owner and repo to list PRs, add pr_number to read a specific one."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Repository owner (user or organization)",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository name",
                },
                "pr_number": {
                    "type": "integer",
                    "description": "Specific PR number to read with details (optional)",
                },
                "state": {
                    "type": "string",
                    "description": "Filter: open, closed, all (default: open)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results when listing (default 10, max 30)",
                },
            },
            "required": ["owner", "repo"],
        }

    async def execute(
        self,
        owner: str,
        repo: str,
        pr_number: int | None = None,
        state: str = "open",
        limit: int = 10,
    ) -> str:
        try:
            from pocketpaw.integrations.github import GitHubClient

            client = GitHubClient()

            # Read a specific PR
            if pr_number is not None:
                pr = await client.get_pull_request(owner, repo, pr_number)

                labels = f" [{', '.join(pr['labels'])}]" if pr.get("labels") else ""
                draft = " (DRAFT)" if pr.get("draft") else ""
                merged = " ✅ MERGED" if pr.get("merged") else ""
                lines = [
                    f"**#{pr['number']} {pr['title']}** ({pr['state']}){draft}{merged}{labels}",
                    f"by @{pr['author']} | {pr['head']} → {pr['base']}",
                    f"+{pr.get('additions', 0):,} / -{pr.get('deletions', 0):,} "
                    f"across {pr.get('changed_files', 0)} files",
                ]

                body = pr.get("body", "")
                if body:
                    lines.append(f"\n{body}")

                comments = pr.get("comments", [])
                if comments:
                    lines.append(f"\n**Comments ({len(comments)}):**\n")
                    for c in comments:
                        body = c.get("body", "")[:300]
                        lines.append(
                            f"- **@{c.get('author', '[deleted]')}** "
                            f"({c.get('created_at', '')[:10]}): {body}"
                        )

                lines.append(f"\n{pr['url']}")
                return "\n".join(lines)

            # List PRs
            pulls = await client.list_pull_requests(owner, repo, state=state, limit=limit)

            if not pulls:
                return f"No {state} pull requests found in {owner}/{repo}."

            lines = [f"Pull requests in **{owner}/{repo}** ({state}):\n"]
            for i, pr in enumerate(pulls, 1):
                labels = f" [{', '.join(pr['labels'][:3])}]" if pr.get("labels") else ""
                draft = " 📝DRAFT" if pr.get("draft") else ""
                lines.append(
                    f"{i}. **#{pr['number']} {pr['title']}**{draft}{labels}\n"
                    f"   by @{pr['author']} | "
                    f"{pr['head']} → {pr['base']} | "
                    f"Updated: {pr['updated_at'][:10]}"
                )
            return "\n".join(lines)

        except Exception as e:
            return self._error(f"GitHub pull requests failed: {e}")
