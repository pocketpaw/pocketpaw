import asyncio

from pocketpaw.config import get_settings
from pocketpaw.tools.builtin.web_search import WebSearchTool


async def main():
    settings = get_settings()
    print("Provider:", settings.web_search_provider)
    print("API Key:", settings.tavily_api_key)

    tool = WebSearchTool()
    res = await tool.execute("what is the weather in tokyo")
    print("\nResult:")
    print(res)


if __name__ == "__main__":
    asyncio.run(main())
