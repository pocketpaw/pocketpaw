import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com/repos"


class GitHubRepoAnalyzerTool(BaseTool):
    """Analyze a GitHub repository and return useful repository insights."""

    @property
    def name(self) -> str:
        return "github_repo_analyzer"

    @property
    def description(self) -> str:
        return (
            "Analyze a public GitHub repository and return key insights such as "
            "stars, forks, open issues, pull requests, contributors, language, "
            "and last updated date."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "GitHub repository URL (https://github.com/owner/repo)",
                }
            },
            "required": ["repo_url"],
        }

    async def execute(self, repo_url: str) -> str:
        """Execute GitHub repository analysis."""

        owner, repo = self._parse_repo_url(repo_url)
        if not owner or not repo:
            return self._error(
                "Invalid GitHub repository URL. Use format: https://github.com/owner/repo"
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Repo metadata
                repo_resp = await client.get(f"{_GITHUB_API_BASE}/{owner}/{repo}")
                repo_resp.raise_for_status()
                repo_data = repo_resp.json()

                # Contributors count
                contrib_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/contributors?per_page=1"
                )
                contributors = 0
                if "link" in contrib_resp.headers:
                    # Extract last page number
                    link_header = contrib_resp.headers["link"]
                    if 'rel="last"' in link_header:
                        last_page = link_header.split('page=')[-1].split(">")[0]
                        contributors = int(last_page)
                elif contrib_resp.status_code == 200:
                    contributors = len(contrib_resp.json())

                # Pull requests (open only)
                pr_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/pulls?state=open&per_page=1"
                )
                open_prs = 0
                if "link" in pr_resp.headers:
                    link_header = pr_resp.headers["link"]
                    if 'rel="last"' in link_header:
                        last_page = link_header.split('page=')[-1].split(">")[0]
                        open_prs = int(last_page)
                elif pr_resp.status_code == 200:
                    open_prs = len(pr_resp.json())

            # Extract data safely
            stars = repo_data.get("stargazers_count", 0)
            forks = repo_data.get("forks_count", 0)
            issues = repo_data.get("open_issues_count", 0)
            language = repo_data.get("language", "Unknown")
            updated_at = repo_data.get("updated_at", "Unknown")
            description = repo_data.get("description") or "No description provided."
            default_branch = repo_data.get("default_branch", "main")

            return (
                f"📊 Repository Analysis: {owner}/{repo}\n\n"
                f"📝 Description: {description}\n\n"
                f"⭐ Stars: {stars}\n"
                f"🍴 Forks: {forks}\n"
                f"🐛 Open Issues: {issues}\n"
                f"🔀 Open Pull Requests: {open_prs}\n"
                f"👥 Contributors: {contributors}\n"
                f"💻 Primary Language: {language}\n"
                f"🌿 Default Branch: {default_branch}\n"
                f"🕒 Last Updated: {updated_at}"
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return self._error("Repository not found or private.")
            elif e.response.status_code == 403:
                return self._error("GitHub API rate limit exceeded.")
            return self._error(f"GitHub API error: {e.response.status_code}")

        except httpx.RequestError:
            return self._error("Network error while contacting GitHub.")

        except Exception as e:
            logger.exception("GitHubRepoAnalyzerTool failed")
            return self._error(str(e))

    def _parse_repo_url(self, repo_url: str) -> tuple[str | None, str | None]:
        """Extract owner and repo from GitHub URL."""
        try:
            parsed = urlparse(repo_url)
            if parsed.netloc != "github.com":
                return None, None

            parts = parsed.path.strip("/").split("/")
            if len(parts) < 2:
                return None, None

            return parts[0], parts[1]
        except Exception:
            return None, None
