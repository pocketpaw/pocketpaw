import asyncio
from pocketclaw.tools.builtin import GitHubRepoAnalyzerTool

async def main():
    tool = GitHubRepoAnalyzerTool()

    repos = [
        "https://github.com/pocketpaw/pocketpaw",
        "https://github.com/psf/requests",
        "invalid-url",
        "https://google.com/test/repo",
        "https://github.com/someuser/reponotexists123456",
    ]

    for repo in repos:
        print("\n====================================")
        print(f"Testing: {repo}")
        print("====================================")

        result = await tool.execute(repo_url=repo)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
