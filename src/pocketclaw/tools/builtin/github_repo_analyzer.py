import logging
from typing import Any, List, Tuple
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
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
            "Professional GitHub repository analyzer with dashboard-style "
            "metrics including health score, contributor ranking, activity "
            "status, risk level, and PR analytics."
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
                },
            },
            "required": ["repo_url"],
        }

    async def execute(self, repo_url: str) -> str:

        parsed = urlparse(repo_url)

        if parsed.netloc != "github.com":
            return self._error("Invalid GitHub repository URL.")

        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            return self._error("Invalid GitHub repository format.")

        owner, repo = parts[0], parts[1]

        try:
            async with httpx.AsyncClient(timeout=15) as client:

                # ---------------- Repo Metadata ----------------
                repo_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}"
                )

                if repo_resp.status_code == 404:
                    return self._error("Repository not found.")
                if repo_resp.status_code == 403:
                    return self._error("GitHub API rate limit exceeded.")

                repo_data = repo_resp.json()

                # ---------------- Contributors ----------------
                contrib_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/contributors"
                )

                contributors_data = (
                    contrib_resp.json()
                    if contrib_resp.status_code == 200
                    else []
                )

                contributor_count = len(contributors_data)
                top_contributors = self._top_contributors(contributors_data)

                # ---------------- PR Stats ----------------
                pr_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/pulls?state=all&per_page=100"
                )

                merged, total = self._analyze_prs(
                    pr_resp.json() if pr_resp.status_code == 200 else []
                )

                merge_ratio = (
                    round((merged / total) * 100, 2)
                    if total > 0
                    else 0
                )

                # ---------------- Recent Commits ----------------
                since = (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat()

                commit_resp = await client.get(
                    f"{_GITHUB_API_BASE}/{owner}/{repo}/commits?since={since}"
                )

                recent_commits = (
                    len(commit_resp.json())
                    if commit_resp.status_code == 200
                    else 0
                )

            # Extract metrics
            stars = repo_data.get("stargazers_count", 0)
            forks = repo_data.get("forks_count", 0)
            issues = repo_data.get("open_issues_count", 0)
            language = repo_data.get("language", "Unknown")
            description = repo_data.get("description", "No description")
            default_branch = repo_data.get("default_branch", "main")
            updated_at = repo_data.get("updated_at")

            health_score = self._calculate_health_score(
                stars, forks, issues, recent_commits
            )

            project_status = self._project_status(recent_commits)
            risk_level = self._risk_level(
                issues, merge_ratio, recent_commits
            )

            # ---------------- Dashboard Output ----------------
            output = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 REPOSITORY DASHBOARD\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Repository: {owner}/{repo}\n\n"
                f"⭐ Stars: {stars}\n"
                f"🍴 Forks: {forks}\n"
                f"🐛 Open Issues: {issues}\n"
                f"👥 Contributors: {contributor_count}\n"
                f"💻 Language: {language}\n"
                f"🌿 Default Branch: {default_branch}\n"
                f"📝 Description: {description}\n\n"
                "📈 Activity Metrics\n"
                "────────────────────────\n"
                f"🔄 Commits (Last 30d): {recent_commits}\n"
                f"🔀 PR Merge Ratio: {merge_ratio}%\n\n"
                "🏆 Top Contributors\n"
                "────────────────────────\n"
                f"{top_contributors}\n\n"
                "🧠 Project Intelligence\n"
                "────────────────────────\n"
                f"💚 Health Score: {health_score}/100\n"
                f"🚦 Status: {project_status}\n"
                f"⚠ Risk Level: {risk_level}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            return output

        except httpx.RequestError:
            return self._error("Network error while contacting GitHub.")
        except Exception as e:
            logger.exception("GitHubRepoAnalyzerTool failed")
            return self._error(str(e))

    # =====================================================
    # Helper Methods
    # =====================================================

    def _analyze_prs(self, prs: List[dict]) -> Tuple[int, int]:
        total = len(prs)
        merged = sum(1 for pr in prs if pr.get("merged_at"))
        return merged, total

    def _top_contributors(self, contributors: List[dict]) -> str:
        if not contributors:
            return "No contributors data available."

        top = sorted(
            contributors,
            key=lambda x: x.get("contributions", 0),
            reverse=True,
        )[:3]

        medals = ["🥇", "🥈", "🥉"]
        lines = []

        for i, contributor in enumerate(top):
            name = contributor.get("login", "Unknown")
            commits = contributor.get("contributions", 0)
            lines.append(f"{medals[i]} {name} – {commits} commits")

        return "\n".join(lines)

    def _calculate_health_score(
        self,
        stars: int,
        forks: int,
        issues: int,
        commits: int,
    ) -> int:

        score = 0
        score += min(stars / 1000 * 40, 40)
        score += min(forks / 500 * 20, 20)
        score += 20 if issues < 20 else 10
        score += 20 if commits > 10 else 10

        return int(min(score, 100))

    def _project_status(self, commits: int) -> str:
        if commits > 10:
            return "Active 🚀"
        elif commits > 3:
            return "Moderately Active ⚡"
        else:
            return "Low Activity 💤"

    def _risk_level(
        self,
        issues: int,
        merge_ratio: float,
        commits: int,
    ) -> str:

        if issues > 100 or merge_ratio < 40 or commits == 0:
            return "High 🔴"
        elif issues > 50 or merge_ratio < 60:
            return "Medium 🟠"
        else:
            return "Low 🟢"
