"""Manual smoke test — create a CFO agent and ingest knowledge docs.

This is a development helper, not a pytest test. It lives under ``scripts/``
(alongside ``a2a_smoke_test.py``) rather than ``tests/`` so pytest doesn't
try to collect it and so it doesn't pollute CI.

Run with:

    uv run python scripts/knowledge_smoke_test.py

Prerequisites:
    1. Backend running: ``uv run pocketpaw serve``
    2. Paste a bearer token into ``TOKEN`` below
    3. Place the three sample docs — ``nexwrk-financials.md``,
       ``nexwrk-product.md``, ``nexwrk-team.md`` — in the same directory
       as this script before running.
"""

import asyncio
from pathlib import Path

import httpx

BASE = "http://localhost:8888/api/v1"
TOKEN = ""  # Paste your bearer token here before running

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

FIXTURES = Path(__file__).parent


async def main():
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as c:
        # 1. Create the CFO agent
        print("\n1. Creating CFO agent...")
        resp = await c.post(
            f"{BASE}/agents",
            json={
                "name": "CFO",
                "slug": "cfo",
                "persona": (
                    "You are the Chief Financial Officer of NexWrk. You speak in numbers, "
                    "always tie back to revenue impact, and give concise financial analysis. "
                    "You know the company's financials, metrics, runway, and forecasts inside out. "
                    "When asked about spending, you evaluate ROI. When asked about hiring, "
                    "you calculate cost-to-revenue impact. You're sharp, direct, and data-driven."
                ),
                "backend": "claude_agent_sdk",
                "model": "",
                "visibility": "workspace",
                "soul_enabled": True,
                "soul_archetype": "The Strategic CFO",
                "soul_ocean": {
                    "openness": 0.5,
                    "conscientiousness": 0.95,
                    "extraversion": 0.4,
                    "agreeableness": 0.6,
                    "neuroticism": 0.15,
                },
                "tools": ["web_search", "research"],
            },
        )
        if resp.status_code != 200:
            print(f"   FAILED: {resp.status_code} {resp.text}")
            return
        agent = resp.json()
        agent_id = agent["_id"]
        print(f"   Created: {agent['name']} (ID: {agent_id})")

        # 2. Ingest knowledge docs
        print("\n2. Ingesting knowledge...")

        for filename in ["nexwrk-financials.md", "nexwrk-product.md", "nexwrk-team.md"]:
            filepath = FIXTURES / filename
            text = filepath.read_text(encoding="utf-8")
            resp = await c.post(
                f"{BASE}/agents/{agent_id}/knowledge/text",
                json={
                    "text": text,
                    "source": filename,
                },
            )
            result = resp.json()
            print(f"   {filename}: {result}")

        # 3. Check stats
        print("\n3. Knowledge stats:")
        # Search to verify
        resp = await c.get(
            f"{BASE}/agents/{agent_id}/knowledge/search",
            params={"q": "revenue", "limit": 3},
        )
        results = resp.json()
        print(f"   Search 'revenue': {len(results.get('results', []))} results")

        # 4. Print test commands
        print(f"""
===========================================
   CFO Agent ready!
===========================================

Agent ID: {agent_id}
Agent slug: cfo

To test in the UI:
  1. Open paw-enterprise
  2. Go to Chat > create a new group
  3. Add the CFO agent to the group (respond_mode: auto)
  4. Send: "What's our current runway?"
  5. The agent should respond using the financial knowledge

To test via API:
  curl -X POST {BASE}/chat/groups -H "Authorization: Bearer $TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{{"name": "Finance Room", "type": "public"}}'

  # Then add the CFO agent to the group:
  curl -X POST {BASE}/chat/groups/GROUP_ID/agents \\
    -H "Authorization: Bearer $TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{{"agent_id": "{agent_id}", "respond_mode": "auto", "role": "assistant"}}'
""")


if __name__ == "__main__":
    asyncio.run(main())
