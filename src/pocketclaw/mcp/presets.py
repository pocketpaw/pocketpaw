"""MCP Server Presets — curated catalog of one-click MCP server integrations.

Provides a registry of pre-configured MCP servers that users can install
from the dashboard with just an API key paste.

Created: 2026-02-09
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pocketclaw.mcp.config import MCPServerConfig


@dataclass
class EnvKeySpec:
    """Specification for an environment variable required by an MCP preset.

    If ``transform`` is set, the user's raw input is substituted into it
    via ``transform.replace("{value}", user_input)`` before being written
    to the env dict.  This lets the UI show a simple "API Token" field
    while the backend builds the actual env value automatically
    (e.g. wrapping a token in a JSON Authorization header).
    """

    key: str  # e.g. "GITHUB_PERSONAL_ACCESS_TOKEN"
    label: str  # e.g. "Personal Access Token"
    required: bool = True
    placeholder: str = ""
    secret: bool = True
    transform: str = ""  # e.g. '{{"Authorization": "Bearer {value}"}}'


@dataclass
class MCPPreset:
    """A pre-configured MCP server template."""

    id: str  # e.g. "github"
    name: str  # e.g. "GitHub"
    description: str
    icon: str  # lucide icon name
    category: str  # "dev" | "productivity" | "data" | "search" | "devops"
    package: str  # npm package name or "" for hosted servers
    command: str = ""  # e.g. "npx" (stdio only)
    args: list[str] = field(default_factory=list)
    env_keys: list[EnvKeySpec] = field(default_factory=list)
    transport: str = "stdio"  # "stdio" | "http" | "sse"
    url: str = ""  # For http/sse transports
    docs_url: str = ""
    needs_args: bool = False  # True if preset requires extra positional args (path, URL, etc.)


# ---------------------------------------------------------------------------
# Preset Registry
# ---------------------------------------------------------------------------

_PRESETS: list[MCPPreset] = [
    # ── Remote HTTP (OAuth) ─────────────────────────────────────────────
    MCPPreset(
        id="github",
        name="GitHub",
        description="Manage repos, issues, PRs, and files (OAuth)",
        icon="github",
        category="dev",
        package="",
        transport="http",
        url="https://api.githubcopilot.com/mcp/",
        docs_url="https://github.com/github/github-mcp-server",
    ),
    MCPPreset(
        id="notion",
        name="Notion",
        description="Search, read, and update pages and databases (OAuth)",
        icon="book-open",
        category="productivity",
        package="",
        transport="http",
        url="https://mcp.notion.com/mcp",
        docs_url="https://developers.notion.com/guides/mcp/get-started-with-mcp",
    ),
    MCPPreset(
        id="atlassian",
        name="Atlassian",
        description="Jira issues and Confluence pages (OAuth)",
        icon="kanban",
        category="productivity",
        package="",
        transport="http",
        url="https://mcp.atlassian.com/v1/mcp",
        docs_url="https://github.com/atlassian/atlassian-mcp-server",
    ),
    MCPPreset(
        id="stripe",
        name="Stripe",
        description="Manage payments, customers, and subscriptions (OAuth)",
        icon="credit-card",
        category="devops",
        package="",
        transport="http",
        url="https://mcp.stripe.com",
        docs_url="https://docs.stripe.com/mcp",
    ),
    MCPPreset(
        id="cloudflare",
        name="Cloudflare",
        description="Workers, bindings, and observability (OAuth)",
        icon="cloud",
        category="devops",
        package="",
        transport="http",
        url="https://bindings.mcp.cloudflare.com/mcp",
        docs_url="https://github.com/cloudflare/mcp-server-cloudflare",
    ),
    MCPPreset(
        id="supabase",
        name="Supabase",
        description="Database, auth, and storage management (OAuth)",
        icon="database",
        category="data",
        package="",
        transport="http",
        url="https://mcp.supabase.com/mcp",
        docs_url="https://supabase.com/docs/guides/getting-started/mcp",
    ),
    MCPPreset(
        id="vercel",
        name="Vercel",
        description="Projects, deployments, and docs (OAuth)",
        icon="triangle",
        category="devops",
        package="",
        transport="http",
        url="https://mcp.vercel.com",
        docs_url="https://vercel.com/docs/mcp/vercel-mcp",
    ),
    MCPPreset(
        id="gitlab",
        name="GitLab",
        description="Repos, merge requests, and CI pipelines (OAuth)",
        icon="git-merge",
        category="dev",
        package="",
        transport="http",
        url="https://gitlab.com/api/v4/mcp",
        docs_url="https://docs.gitlab.com/user/gitlab_duo/model_context_protocol/",
    ),
    MCPPreset(
        id="figma",
        name="Figma",
        description="Inspect designs and Dev Mode layouts (OAuth)",
        icon="figma",
        category="dev",
        package="",
        transport="http",
        url="https://mcp.figma.com/mcp",
        docs_url="https://developers.figma.com/docs/figma-mcp-server/",
    ),
    # ── Stdio (npm packages) ────────────────────────────────────────────
    MCPPreset(
        id="playwright",
        name="Playwright",
        description="Browser automation via accessibility snapshots (by Microsoft)",
        icon="monitor",
        category="dev",
        package="@playwright/mcp",
        command="npx",
        args=["-y", "@playwright/mcp@latest"],
        docs_url="https://github.com/microsoft/playwright-mcp",
    ),
    MCPPreset(
        id="context7",
        name="Context7",
        description="Up-to-date library docs and code examples for LLMs",
        icon="book-copy",
        category="dev",
        package="@upstash/context7-mcp",
        command="npx",
        args=["-y", "@upstash/context7-mcp@latest"],
        docs_url="https://github.com/upstash/context7",
    ),
    MCPPreset(
        id="shopify",
        name="Shopify Dev",
        description="Search Shopify docs, explore API schemas, build Functions",
        icon="shopping-bag",
        category="dev",
        package="@shopify/dev-mcp",
        command="npx",
        args=["-y", "@shopify/dev-mcp@latest"],
        docs_url="https://shopify.dev/docs/apps/build/devmcp",
    ),
    MCPPreset(
        id="linear",
        name="Linear",
        description="Manage issues, projects, and teams in Linear",
        icon="layout-list",
        category="dev",
        package="mcp-linear",
        command="npx",
        args=["-y", "mcp-linear"],
        env_keys=[
            EnvKeySpec(
                key="LINEAR_API_KEY",
                label="API Key",
                placeholder="lin_api_...",
            ),
        ],
    ),
    MCPPreset(
        id="sentry",
        name="Sentry",
        description="Query issues, events, and releases from Sentry",
        icon="bug",
        category="devops",
        package="@sentry/mcp-server",
        command="npx",
        args=["-y", "@sentry/mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="SENTRY_ACCESS_TOKEN",
                label="Access Token",
                placeholder="sntrys_...",
            ),
        ],
        docs_url="https://github.com/getsentry/sentry-mcp",
    ),
    MCPPreset(
        id="mongodb",
        name="MongoDB",
        description="Query and manage MongoDB databases and Atlas clusters",
        icon="database",
        category="data",
        package="mongodb-mcp-server",
        command="npx",
        args=["-y", "mongodb-mcp-server@latest"],
        env_keys=[
            EnvKeySpec(
                key="MDB_MCP_CONNECTION_STRING",
                label="Connection String",
                placeholder="mongodb+srv://user:pass@cluster...",
            ),
        ],
        docs_url="https://github.com/mongodb-js/mongodb-mcp-server",
    ),
    MCPPreset(
        id="brave-search",
        name="Brave Search",
        description="Web and local search powered by the Brave Search API",
        icon="search",
        category="search",
        package="@modelcontextprotocol/server-brave-search",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        env_keys=[
            EnvKeySpec(
                key="BRAVE_API_KEY",
                label="API Key",
                placeholder="BSA...",
            ),
        ],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="exa-search",
        name="Exa Search",
        description="Neural web search, code context, and company research",
        icon="radar",
        category="search",
        package="exa-mcp-server",
        command="npx",
        args=["-y", "exa-mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="EXA_API_KEY",
                label="API Key",
                placeholder="exa-...",
            ),
        ],
        docs_url="https://github.com/exa-labs/exa-mcp-server",
    ),
    MCPPreset(
        id="google-maps",
        name="Google Maps",
        description="Geocoding, places, directions, and distance matrix",
        icon="map-pin",
        category="search",
        package="@googlemaps/code-assist-mcp",
        command="npx",
        args=["-y", "@googlemaps/code-assist-mcp@latest"],
        env_keys=[
            EnvKeySpec(
                key="GOOGLE_MAPS_API_KEY",
                label="API Key",
                placeholder="AIzaSy...",
            ),
        ],
        docs_url="https://developers.google.com/maps/ai/mcp",
    ),
    MCPPreset(
        id="fetch",
        name="Web Fetch",
        description="Fetch and convert web pages to markdown for LLM consumption",
        icon="globe",
        category="search",
        package="@modelcontextprotocol/server-fetch",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-fetch"],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="slack",
        name="Slack",
        description="Read channels, post messages, and manage workspaces",
        icon="hash",
        category="productivity",
        package="@modelcontextprotocol/server-slack",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        env_keys=[
            EnvKeySpec(
                key="SLACK_BOT_TOKEN",
                label="Bot Token",
                placeholder="xoxb-...",
            ),
            EnvKeySpec(
                key="SLACK_TEAM_ID",
                label="Team ID",
                placeholder="T0123456789",
            ),
        ],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="asana",
        name="Asana",
        description="Manage tasks, projects, and workspaces in Asana",
        icon="check-square",
        category="productivity",
        package="@roychri/mcp-server-asana",
        command="npx",
        args=["-y", "@roychri/mcp-server-asana"],
        env_keys=[
            EnvKeySpec(
                key="ASANA_ACCESS_TOKEN",
                label="Personal Access Token",
                placeholder="1/1234567890:abcdef...",
            ),
        ],
        docs_url="https://github.com/roychri/mcp-server-asana",
    ),
    MCPPreset(
        id="memory",
        name="Memory (KG)",
        description="Persistent knowledge graph memory for conversations",
        icon="brain",
        category="productivity",
        package="@modelcontextprotocol/server-memory",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="sequential-thinking",
        name="Thinking",
        description="Step-by-step sequential thinking for complex reasoning",
        icon="lightbulb",
        category="productivity",
        package="@modelcontextprotocol/server-sequential-thinking",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="filesystem",
        name="Filesystem",
        description="Read, write, and manage files in allowed directories",
        icon="folder",
        category="data",
        package="@modelcontextprotocol/server-filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem"],
        docs_url="https://github.com/modelcontextprotocol/servers",
        needs_args=True,
    ),
    MCPPreset(
        id="postgres",
        name="PostgreSQL",
        description="Query and inspect PostgreSQL databases",
        icon="database",
        category="data",
        package="@modelcontextprotocol/server-postgres",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres"],
        docs_url="https://github.com/modelcontextprotocol/servers",
        needs_args=True,
    ),
    MCPPreset(
        id="sqlite",
        name="SQLite",
        description="Query and manage SQLite databases",
        icon="database",
        category="data",
        package="@modelcontextprotocol/server-sqlite",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-sqlite"],
        docs_url="https://github.com/modelcontextprotocol/servers",
        needs_args=True,
    ),
    MCPPreset(
        id="git",
        name="Git",
        description="Read, search, and inspect local Git repositories",
        icon="git-branch",
        category="data",
        package="@modelcontextprotocol/server-git",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-git"],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="everart",
        name="Image Gen",
        description="Generate images using the Everart API",
        icon="image",
        category="productivity",
        package="@modelcontextprotocol/server-everart",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everart"],
        env_keys=[
            EnvKeySpec(
                key="EVERART_API_KEY",
                label="API Key",
                placeholder="ev_...",
            ),
        ],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
]

# Build lookup dicts once
_PRESETS_BY_ID: dict[str, MCPPreset] = {p.id: p for p in _PRESETS}
_PRESETS_BY_CATEGORY: dict[str, list[MCPPreset]] = {}
for _p in _PRESETS:
    _PRESETS_BY_CATEGORY.setdefault(_p.category, []).append(_p)


def get_all_presets() -> list[MCPPreset]:
    """Return all presets in the catalog."""
    return list(_PRESETS)


def get_preset(preset_id: str) -> MCPPreset | None:
    """Return a preset by ID, or None if not found."""
    return _PRESETS_BY_ID.get(preset_id)


def get_presets_by_category(category: str) -> list[MCPPreset]:
    """Return presets filtered by category."""
    return list(_PRESETS_BY_CATEGORY.get(category, []))


def preset_to_config(
    preset: MCPPreset,
    env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> MCPServerConfig:
    """Convert a preset + user-supplied env values into an MCPServerConfig.

    Applies ``EnvKeySpec.transform`` templates so the user can provide
    a plain token and the final env value is built automatically.
    """
    args = list(preset.args)
    if extra_args:
        args.extend(extra_args)

    resolved_env: dict[str, str] = {}
    if env:
        transform_map = {ek.key: ek.transform for ek in preset.env_keys if ek.transform}
        for key, value in env.items():
            if key in transform_map and value:
                resolved_env[key] = transform_map[key].replace("{value}", value)
            else:
                resolved_env[key] = value

    return MCPServerConfig(
        name=preset.id,
        transport=preset.transport,
        command=preset.command,
        args=args,
        url=preset.url,
        env=resolved_env,
        enabled=True,
    )
