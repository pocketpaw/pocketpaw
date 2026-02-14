import logging
from typing import Any
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
            "Analyze a GitHub repository and return key insights such as "
            "stars, forks, issues, contributors, language, and health score."
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

        # -----------------------------
        # Validate URL
        # -----------------------------
        parsed = urlparse(repo_url)

        if parsed.netloc != "github.com":
            return self._error("Invalid GitHub repository URL.")

        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            return self._error("Invalid GitHub repository format.")

        owner, repo = parts[0], parts[1]

        try:
            async with httpx.AsyncClient(timeout=15) as client:

                # -----------------------------
                # Fetch repository metadata
                # -----------------------------
                repo_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}"
                )

                if repo_resp.status_code == 404:
                    return self._error("Repository not found.")
                if repo_resp.status_code == 403:
                    return self._error("GitHub API rate limit exceeded.")

                repo_data = repo_resp.json()

                # -----------------------------
                # Fetch contributors
                # -----------------------------
                contrib_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/contributors"
                )

                contributors = (
                    len(contrib_resp.json())
                    if contrib_resp.status_code == 200
                    else 0
                )

            # -----------------------------
            # Extract key fields
            # -----------------------------
            stars = repo_data.get("stargazers_count", 0)
            forks = repo_data.get("forks_count", 0)
            issues = repo_data.get("open_issues_count", 0)
            language = repo_data.get("language", "Unknown")
            updated_at = repo_data.get("updated_at")
            description = repo_data.get("description", "No description")
            default_branch = repo_data.get("default_branch", "main")

            # -----------------------------
            # Calculate Health Score
            # -----------------------------
            score, grade = self._calculate_health_score(
                stars, forks, issues, updated_at
            )

            # -----------------------------
            # Format Output
            # -----------------------------
            output = (
                f"📊 Repository Analysis: {owner}/{repo}\n\n"
                f"⭐ Stars: {stars}\n"
                f"🍴 Forks: {forks}\n"
                f"🐛 Open Issues: {issues}\n"
                f"👥 Contributors: {contributors}\n"
                f"💻 Primary Language: {language}\n"
                f"🌿 Default Branch: {default_branch}\n"
                f"💚 Health Score: {score}/100 ({grade})\n"
                f"📝 Description: {description}\n"
            )

            return output

        except httpx.RequestError:
            return self._error("Network error while contacting GitHub.")
        except Exception as e:
            logger.exception("GitHubRepoAnalyzerTool failed")
            return self._error(str(e))

    # -------------------------------------------------
    # Health Score Logic
    # -------------------------------------------------

    def _calculate_health_score(
        self,
        stars: int,
        forks: int,
        issues: int,
        updated_at: str | None,
    ) -> tuple[int, str]:

        score = 0

        # Stars contribution (max 40)
        score += min((stars / 1000) * 40, 40)

        # Forks contribution (max 20)
        score += min((forks / 500) * 20, 20)

        # Issue health (max 20)
        if issues < 10:
            score += 20
        elif issues < 50:
            score += 10

        # Recent activity (max 20)
        if updated_at:
            try:
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
            except Exception:
                pass

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
