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

        files_data = self._scan_files(path)
        git_data = self._git_stats(path)
        health = self._calculate_health(git_data)

        report = self._build_report(files_data, git_data, health)

        return self._success(report)

    def _scan_files(self, path):
        total_files = 0
        total_lines = 0
        extensions = Counter()

        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                total_files += 1

                extensions[os.path.splitext(f)[1]] += 1

            try:
                with open(fp, "r", errors="ignore") as fh:
                    total_lines += sum(1 for _ in fh)
            except Exception as e:
                print(f"Skipped file: {fp} → {e}")

        return {
                "files": total_files,
                "lines": total_lines,
                "extensions": extensions,
        }

    def _git_stats(self, path):
        def safe_git(cmd):
            try:
                return subprocess.check_output(
                    ["git", "-C", path] + cmd,
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                return ""

        contributors = safe_git(["shortlog", "-sn"]).splitlines()[:5]
        recent = safe_git(["log", "--oneline", "-5"]).splitlines()

        hotspot_raw = safe_git(["log", "--name-only", "--pretty=format:"])
        hotspots = Counter(
            f for f in hotspot_raw.splitlines() if f.strip()
        ).most_common(5)

        return {
        "contributors": contributors,
        "recent": recent,
        "hotspots": hotspots,
        "last_commit": safe_git(["log", "-1", "--format=%cr"]).strip(),
    }

    def _calculate_health(self, git_data):
        churn = sum(c for _, c in git_data["hotspots"])
        activity = 30 if git_data["recent"] else 5
        collab = min(len(git_data["contributors"]) * 10, 30)
        stability = max(40 - churn // 3, 5)

        return min(activity + collab + stability, 100)

def _build_report(self, files, git, health):
    report = f"""
📊 Repository Insights

Files: {files['files']}
Lines: {files['lines']}
Last activity: {git['last_commit']}

🏥 Health Score: {health}/100

Top file types:
"""

    for ext, count in files["extensions"].most_common(5):
        report += f"  {ext or '[no ext]'} → {count}\n"

    return report
