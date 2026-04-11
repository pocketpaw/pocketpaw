"""Maps frontend tool category IDs to backend tool group names."""

from __future__ import annotations

CATEGORY_TO_GROUPS: dict[str, list[str]] = {
    "google_workspace": ["group:gmail", "group:calendar", "group:drive", "group:docs"],
    "web_research": ["group:browser", "group:search", "group:research"],
    "media": ["group:media", "group:voice", "group:translate"],
    "social": ["group:spotify", "group:reddit", "group:discord"],
    "execution": ["group:shell", "group:packages"],
    "delegation": ["group:delegation"],
    "file_system": ["group:fs"],
    "enterprise": [],  # Enterprise tools don't have groups — handled by direct names
}

# Direct tool names for categories without groups (enterprise)
CATEGORY_DIRECT_TOOLS: dict[str, list[str]] = {
    "enterprise": [
        "instinct_propose",
        "instinct_pending",
        "instinct_audit",
        "fabric_create",
        "fabric_query",
        "fabric_stats",
    ],
}
