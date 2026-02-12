import os
import subprocess
from collections import Counter
from typing import Any

from pocketclaw.tools.protocol import BaseTool


class RepoInsightsTool(BaseTool):
    @property
    def name(self) -> str:
        return "repo_insights"

    @property
    def description(self) -> str:
        return "Analyze a local Git repository and return useful statistics."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local filesystem path to the Git repository",
                }
            },
            "required": ["path"],
        }

    async def execute(self, path: str) -> str:
        if not os.path.exists(path):
            return self._error("Repository path does not exist.")

        total_files = 0
        total_lines = 0
        extensions = Counter()

        for root, _, files in os.walk(path):
            for f in files:
                file_path = os.path.join(root, f)
                total_files += 1

                ext = os.path.splitext(f)[1]
                extensions[ext] += 1

                try:
                    with open(file_path, "r", errors="ignore") as fh:
                        total_lines += sum(1 for _ in fh)
                except Exception:
                    pass

        def safe_git(cmd):
            try:
                return subprocess.check_output(
                    ["git", "-C", path] + cmd,
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                return ""

        commits = safe_git(["rev-list", "--count", "HEAD"]).strip() or "N/A"

        contributors = safe_git(["shortlog", "-sn"]).splitlines()[:5]
        recent_commits = safe_git(["log", "--oneline", "-5"]).splitlines()

        hotspot_raw = safe_git(["log", "--name-only", "--pretty=format:"])
        hotspot_counter = Counter(
            f for f in hotspot_raw.splitlines() if f.strip()
        )
        hotspots = hotspot_counter.most_common(5)
        # --- Health scoring ---

        contributor_count = len(contributors)

        # Last commit date
        last_commit = safe_git(["log", "-1", "--format=%cr"]).strip() or "Unknown"

        # Churn = number of hotspot entries
        churn_score = min(sum(count for _, count in hotspots), 100)

        # Activity scoring
        activity_score = 30 if recent_commits else 5

        # Contributor scoring
        collab_score = min(contributor_count * 10, 30)

        # Stability scoring (lower churn = better)
        stability_score = max(40 - churn_score // 3, 5)

        health_score = min(activity_score + collab_score + stability_score, 100)

        report = f"""
📊 Repository Insights

Files: {total_files}
Lines: {total_lines}
Commits: {commits}
Last activity: {last_commit}


🏥 Repo Health Score: {health_score}/100

Signals:
  Activity → {activity_score}
  Collaboration → {collab_score}
  Stability → {stability_score}


Top file types:
"""

        for ext, count in extensions.most_common(5):
            report += f"  {ext or '[no ext]'} → {count}\n"

        report += "\n👥 Top contributors:\n"
        for c in contributors:
            report += f"  {c}\n"

        report += "\n🕒 Recent commits:\n"
        for c in recent_commits:
            report += f"  {c}\n"

        report += "\n🔥 Hotspot files:\n"
        for file, count in hotspots:
            report += f"  {file} → {count} changes\n"

        return self._success(report)
