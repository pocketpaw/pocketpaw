# GitHub Client — interaction with GitHub API via httpx.
# Created: 2026-03-22
# Part of GitHub Integration Feature

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_USER_AGENT = "PocketPaw/1.0 (AI Assistant; +https://github.com/pocketpaw/pocketpaw)"


class GitHubClient:
    """Client for interacting with the GitHub REST API."""

    def __init__(self, token: str | None = None):
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """Get repository information."""
        url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers, follow_redirects=True)
            resp.raise_for_status()
            return resp.json()

    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """List issues in a repository."""
        url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/issues"
        params = {"state": state, "per_page": min(per_page, 30)}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url, headers=self._headers, params=params, follow_redirects=True
            )
            resp.raise_for_status()
            return resp.json()

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        """Get a specific issue with its comments."""
        url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers, follow_redirects=True)
            resp.raise_for_status()
            issue = resp.json()

            # Fetch comments too
            comments_url = issue.get("comments_url")
            if comments_url:
                c_resp = await client.get(comments_url, headers=self._headers)
                if c_resp.status_code == 200:
                    issue["comments_data"] = c_resp.json()

            return issue
