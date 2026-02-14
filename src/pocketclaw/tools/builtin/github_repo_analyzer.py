import logging
from typing import Any, List
from urllib.parse import urlparse
from datetime import datetime, timezone
import httpx

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com/repos"


class GitHubRepoAnalyzerTool(BaseTool):

    @property
    def name(self) -> str:
        return "github_repo_analyzer"

    @property
    def description(self) -> str:
        return (
            "Analyze a GitHub repository and return key insights "
            "such as stars, forks, issues, contributor rankings, "
            "and repository health score."
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

        parsed = urlparse(repo_url)

        if parsed.netloc != "github.com":
            return self._error("Invalid GitHub repository URL.")

        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            return self._error("Invalid GitHub repository format.")

        owner, repo = path_parts[0], path_parts[1]

        try:
            async with httpx.AsyncClient(timeout=15) as client:

                repo_resp = await client.get(f"{_GITHUB_API_BASE}/{owner}/{repo}")

                if repo_resp.status_code == 404:
                    return self._error("Repository not found.")
                if repo_resp.status_code == 403:
                    return self._error("GitHub API rate limit exceeded.")

                repo_data = repo_resp.json()

                contrib_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/contributors"
                )

                contributors_data = (
                    contrib_resp.json()
                    if contrib_resp.status_code == 200
                    else []
                )

            stars = repo_data.get("stargazers_count", 0)
            forks = repo_data.get("forks_count", 0)
            issues = repo_data.get("open_issues_count", 0)
            language = repo_data.get("language", "Unknown")
            updated_at = repo_data.get("updated_at")
            description = repo_data.get("description", "No description")
            default_branch = repo_data.get("default_branch", "main")

            # Contributor ranking
            contributor_output = self._format_contributors(contributors_data)

            score, grade = self._calculate_health_score(
                stars, forks, issues, updated_at
            )

            output = (
                f"📊 Repository Analysis: {owner}/{repo}\n\n"
                f"⭐ Stars: {stars}\n"
                f"🍴 Forks: {forks}\n"
                f"🐛 Open Issues: {issues}\n"
                f"💻 Primary Language: {language}\n"
                f"🌿 Default Branch: {default_branch}\n"
                f"📝 Description: {description}\n\n"
                f"{contributor_output}\n"
                f"\n💚 Health Score: {score}/100 ({grade})\n"
            )

            return output

        except httpx.RequestError:
            return self._error("Network error while contacting GitHub.")
        except Exception as e:
            logger.exception("GitHubRepoAnalyzerTool failed")
            return self._error(str(e))

    # -------------------------------------------------
    # Contributor Ranking
    # -------------------------------------------------

    def _format_contributors(self, contributors: List[dict]) -> str:

        if not contributors:
            return "👥 Contributors: No contributor data available."

        top = sorted(
            contributors,
            key=lambda x: x.get("contributions", 0),
            reverse=True,
        )[:5]

        lines = ["👥 Top Contributors:"]
        for i, c in enumerate(top, start=1):
            name = c.get("login", "unknown")
            commits = c.get("contributions", 0)
            lines.append(f"   {i}. {name} ({commits} commits)")

        return "\n".join(lines)

    # -------------------------------------------------
    # Health Score
    # -------------------------------------------------

    def _calculate_health_score(
        self,
        stars: int,
        forks: int,
        issues: int,
        updated_at: str | None,
    ) -> tuple[int, str]:

        score = 0

        score += min(stars / 1000 * 40, 40)
        score += min(forks / 500 * 20, 20)

        if issues < 10:
            score += 20
        elif issues < 50:
            score += 10

        if updated_at:
            last_update = datetime.fromisoformat(
                updated_at.replace("Z", "+00:00")
            )
            days_since_update = (
                datetime.now(timezone.utc) - last_update
            ).days

            if days_since_update < 30:
                score += 20
            elif days_since_update < 90:
                score += 10

        final_score = int(min(score, 100))

        if final_score >= 80:
            grade = "Excellent"
        elif final_score >= 60:
            grade = "Good"
        elif final_score >= 40:
            grade = "Average"
        else:
            grade = "Needs Attention"

        return final_score, grade
