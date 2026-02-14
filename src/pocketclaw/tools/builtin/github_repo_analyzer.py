import logging
from typing import Any
import httpx

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com/repos"


class GitHubRepoAnalyzerTool(BaseTool):

    @property
    def name(self) -> str:
        return "github_repo_analyzer"

    @property
    def description(self) -> str:
        return (
            "Analyze a GitHub repository and return key insights "
            "such as stars, forks, contributors, issues, and metadata."
        )

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
        try:
            # ------------------------------
            # Validate URL
            # ------------------------------
            if not repo_url.startswith("https://github.com/"):
                return self._error("Invalid GitHub repository URL.")

            parts = repo_url.rstrip("/").split("/")
            if len(parts) < 5:
                return self._error("Invalid GitHub repository URL.")

            owner = parts[-2]
            repo = parts[-1]

            async with httpx.AsyncClient(timeout=15) as client:

                # ------------------------------
                # Fetch repo metadata
                # ------------------------------
                repo_resp = await client.get(
                    f"{GITHUB_API_BASE}/{owner}/{repo}"
                )

                if repo_resp.status_code == 404:
                    return self._error("Repository not found.")

                if repo_resp.status_code == 403:
                    return self._error("GitHub API rate limit exceeded.")

                repo_resp.raise_for_status()

                repo_data = repo_resp.json()

                # ------------------------------
                # Fetch contributors
                # ------------------------------
                contrib_resp = await client.get(
                    f"{GITHUB_API_BASE}/{owner}/{repo}/contributors"
                )

                contributors = 0
                if contrib_resp.status_code == 200:
                    contributors_data = contrib_resp.json()
                    if isinstance(contributors_data, list):
                        contributors = len(contributors_data)

                # ------------------------------
                # Extract fields safely
                # ------------------------------
                stars = repo_data.get("stargazers_count", 0)
                forks = repo_data.get("forks_count", 0)
                issues = repo_data.get("open_issues_count", 0)
                language = repo_data.get("language", "Unknown")
                description = repo_data.get("description") or "No description"
                default_branch = repo_data.get("default_branch", "main")
                updated_at = repo_data.get("updated_at", "Unknown")

                # ------------------------------
                # Format response
                # ------------------------------
                return (
                    f"📊 Repository Analysis: {owner}/{repo}\n\n"
                    f"📝 Description: {description}\n"
                    f"⭐ Stars: {stars}\n"
                    f"🍴 Forks: {forks}\n"
                    f"🐞 Open Issues: {issues}\n"
                    f"👥 Contributors: {contributors}\n"
                    f"💻 Primary Language: {language}\n"
                    f"🌿 Default Branch: {default_branch}\n"
                    f"🕒 Last Updated: {updated_at}"
                )

        except httpx.RequestError:
            return self._error("Network error while contacting GitHub.")

        except Exception as e:
            logger.exception("GitHubRepoAnalyzerTool failed")
            return self._error(str(e))
