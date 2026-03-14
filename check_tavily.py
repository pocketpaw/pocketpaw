import asyncio
import httpx
from pocketpaw.config import get_settings

async def main():
    settings = get_settings()
    key = settings.tavily_api_key
    print(f"Using key: {key}")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": "test",
                "max_results": 1,
                "include_answer": False,
            },
        )
        print("Status code:", resp.status_code)
        print("Response:", resp.text)

if __name__ == "__main__":
    asyncio.run(main())
