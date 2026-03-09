# GitHub Client — read-only access via GitHub REST API.
# Created: 2026-03-09
#
# Uses GitHub's public REST API (https://api.github.com).
# Works without authentication for public repos (60 req/hour).
# Set POCKETPAW_GITHUB_TOKEN for higher rate limits (5000/hour) and private repos.

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_USER_AGENT = "PocketPaw/1.0 (AI Assistant; +https://github.com/pocketpaw/pocketpaw)"

# Simple rate limiter — 1 req/sec to be polite
_last_request_time: float = 0


async def _rate_limit():
    """Ensure we don't hit GitHub too fast."""
    global _last_request_time
    now = asyncio.get_event_loop().time()
    elapsed = now - _last_request_time
    if elapsed < 1.0:
        await asyncio.sleep(1.0 - elapsed)
    _last_request_time = asyncio.get_event_loop().time()


def _get_token() -> str | None:
    """Get GitHub token from PocketPaw settings (lazy import to avoid circular deps)."""
    try:
        from pocketpaw.config import get_settings

        return get_settings().github_token or None
    except Exception:
        return None


class GitHubClient:
    """Read-only client for the GitHub REST API.

    No API key required for public repos. Optionally uses a personal access
    token for higher rate limits (5000/hour) and private repo access.
    """

    def _headers(self) -> dict[str, str]:
        """Build request headers with optional auth."""
        headers: dict[str, str] = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = _get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def search_repos(
        self,
        query: str,
        sort: str = "best_match",
        language: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search GitHub repositories.

        Args:
            query: Search query.
            sort: Sort — 'best_match', 'stars', 'forks', 'updated'.
            language: Filter by programming language (optional).
            limit: Maximum results (max 30).

        Returns:
            List of repo dicts.
        """
        await _rate_limit()

        q = query
        if language:
            q += f" language:{language}"

        params: dict[str, Any] = {
            "q": q,
            "sort": sort if sort != "best_match" else "",
            "per_page": min(limit, 30),
        }
        # Remove empty sort to use default
        if not params["sort"]:
            del params["sort"]

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GITHUB_API}/search/repositories",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        repos = []
        for item in data.get("items", [])[:limit]:
            repos.append(self._format_repo(item))

        return repos

    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List issues for a repository.

        Args:
            owner: Repository owner (user or org).
            repo: Repository name.
            state: Issue state — 'open', 'closed', 'all'.
            limit: Maximum results (max 30).

        Returns:
            List of issue dicts.
        """
        await _rate_limit()

        params: dict[str, Any] = {
            "state": state,
            "per_page": min(limit, 30),
            "sort": "updated",
            "direction": "desc",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/issues",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        issues = []
        for item in data:
            # GitHub API returns PRs in the issues endpoint — skip them
            if item.get("pull_request"):
                continue
            issues.append(self._format_issue(item))

        return issues[:limit]

    async def get_issue(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> dict[str, Any]:
        """Get issue details with top comments.

        Args:
            owner: Repository owner.
            repo: Repository name.
            number: Issue number.

        Returns:
            Dict with issue data and comments.
        """
        await _rate_limit()

        async with httpx.AsyncClient(timeout=15) as client:
            # Get the issue
            resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            issue_data = resp.json()

            # Get comments (separate request)
            await _rate_limit()
            comments_resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments",
                params={"per_page": 10},
                headers=self._headers(),
            )
            comments_resp.raise_for_status()
            comments_data = comments_resp.json()

        issue = self._format_issue(issue_data)
        issue["body"] = (issue_data.get("body") or "")[:3000]

        comments = []
        for c in comments_data[:10]:
            comments.append(
                {
                    "author": c.get("user", {}).get("login", "[deleted]"),
                    "body": (c.get("body") or "")[:500],
                    "created_at": c.get("created_at", ""),
                    "reactions": c.get("reactions", {}).get("total_count", 0),
                }
            )
        issue["comments"] = comments

        return issue

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List pull requests for a repository.

        Args:
            owner: Repository owner.
            repo: Repository name.
            state: PR state — 'open', 'closed', 'all'.
            limit: Maximum results (max 30).

        Returns:
            List of PR dicts.
        """
        await _rate_limit()

        params: dict[str, Any] = {
            "state": state,
            "per_page": min(limit, 30),
            "sort": "updated",
            "direction": "desc",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/pulls",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        pulls = []
        for item in data[:limit]:
            pulls.append(self._format_pull_request(item))

        return pulls

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> dict[str, Any]:
        """Get pull request details with review comments.

        Args:
            owner: Repository owner.
            repo: Repository name.
            number: PR number.

        Returns:
            Dict with PR data and review comments.
        """
        await _rate_limit()

        async with httpx.AsyncClient(timeout=15) as client:
            # Get the PR
            resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{number}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            pr_data = resp.json()

            # Get review comments
            await _rate_limit()
            comments_resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments",
                params={"per_page": 10},
                headers=self._headers(),
            )
            comments_resp.raise_for_status()
            comments_data = comments_resp.json()

        pr = self._format_pull_request(pr_data)
        pr["body"] = (pr_data.get("body") or "")[:3000]
        pr["additions"] = pr_data.get("additions", 0)
        pr["deletions"] = pr_data.get("deletions", 0)
        pr["changed_files"] = pr_data.get("changed_files", 0)
        pr["mergeable"] = pr_data.get("mergeable")
        pr["merged"] = pr_data.get("merged", False)

        comments = []
        for c in comments_data[:10]:
            comments.append(
                {
                    "author": c.get("user", {}).get("login", "[deleted]"),
                    "body": (c.get("body") or "")[:500],
                    "created_at": c.get("created_at", ""),
                    "reactions": c.get("reactions", {}).get("total_count", 0),
                }
            )
        pr["comments"] = comments

        return pr

    @staticmethod
    def _format_repo(repo: dict) -> dict[str, Any]:
        """Format a GitHub repo into a clean dict."""
        return {
            "name": repo.get("full_name", ""),
            "description": repo.get("description", "") or "",
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "language": repo.get("language", ""),
            "open_issues": repo.get("open_issues_count", 0),
            "url": repo.get("html_url", ""),
            "updated_at": repo.get("updated_at", ""),
            "topics": repo.get("topics", []),
        }

    @staticmethod
    def _format_issue(issue: dict) -> dict[str, Any]:
        """Format a GitHub issue into a clean dict."""
        labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        return {
            "number": issue.get("number", 0),
            "title": issue.get("title", ""),
            "state": issue.get("state", ""),
            "author": issue.get("user", {}).get("login", "[deleted]"),
            "labels": labels,
            "comments_count": issue.get("comments", 0),
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
            "url": issue.get("html_url", ""),
        }

    @staticmethod
    def _format_pull_request(pr: dict) -> dict[str, Any]:
        """Format a GitHub PR into a clean dict."""
        labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]
        return {
            "number": pr.get("number", 0),
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "author": pr.get("user", {}).get("login", "[deleted]"),
            "labels": labels,
            "draft": pr.get("draft", False),
            "head": pr.get("head", {}).get("ref", ""),
            "base": pr.get("base", {}).get("ref", ""),
            "created_at": pr.get("created_at", ""),
            "updated_at": pr.get("updated_at", ""),
            "url": pr.get("html_url", ""),
        }
