import logging
from typing import Any
import httpx

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com/repos"


class GitHubRepoAnalyzerTool(BaseTool):
    """Analyze a GitHub repository and return useful statistics."""

    @property
    def name(self) -> str:
        return "github_repo_analyzer"

    @property
    def description(self) -> str:
        return (
            "Analyze a public GitHub repository and return key insights such as "
            "stars, forks, description, primary language, last update time, "
            "top contributor, and pull request statistics."
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
                    "description": "GitHub repository URL (e.g., https://github.com/owner/repo)",
                }
            },
            "required": ["repo_url"],
        }

    async def execute(self, repo_url: str) -> str:
        if "github.com" not in repo_url:
            return self._error("Invalid GitHub URL.")

        try:
            parts = repo_url.rstrip("/").split("/")
            if len(parts) < 2:
                return self._error("Invalid GitHub repository URL format.")

            owner = parts[-2]
            repo = parts[-1].replace(".git", "")

            async with httpx.AsyncClient(timeout=15) as client:
                # Fetch repository metadata
                repo_resp = await client.get(f"{_GITHUB_API_BASE}/{owner}/{repo}")
                repo_resp.raise_for_status()
                repo_data = repo_resp.json()

                # Fetch contributors
                contrib_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/contributors"
                )
                contrib_resp.raise_for_status()
                contributors = contrib_resp.json()

                # Fetch pull requests (all states)
                pr_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/pulls?state=all&per_page=100"
                )
                pr_resp.raise_for_status()
                pulls = pr_resp.json()

            # Extract repository data
            stars = repo_data.get("stargazers_count", 0)
            forks = repo_data.get("forks_count", 0)
            description = repo_data.get("description") or "No description"
            language = repo_data.get("language") or "Unknown"
            updated = repo_data.get("updated_at") or "Unknown"

            # Contributor stats
            top_contributor = (
                contributors[0]["login"] if contributors else "N/A"
            )

            # Pull request stats
            total_prs = len(pulls)
            open_prs = len([p for p in pulls if p.get("state") == "open"])
            closed_prs = total_prs - open_prs

            return (
                f"📊 Repository Analysis: {owner}/{repo}\n\n"
                f"📝 Description: {description}\n"
                f"⭐ Stars: {stars}\n"
                f"🍴 Forks: {forks}\n"
                f"💻 Primary Language: {language}\n"
                f"🕒 Last Updated: {updated}\n\n"
                f"👤 Top Contributor: {top_contributor}\n"
                f"🔀 Pull Requests: {total_prs} "
                f"(Open: {open_prs}, Closed: {closed_prs})"
            )

        except httpx.HTTPStatusError as e:
            return self._error(f"GitHub API error: {e.response.status_code}")
        except httpx.RequestError as e:
            return self._error(f"Network error: {str(e)}")
        except Exception as e:
            logger.exception("GitHubRepoAnalyzerTool failed")
            return self._error(str(e))
