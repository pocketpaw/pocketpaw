"""MCP Server Presets — curated catalog of one-click MCP server integrations.

Provides a registry of pre-configured MCP servers that users can install
from the dashboard with just an API key paste.

Created: 2026-02-09
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pocketpaw.mcp.config import MCPServerConfig


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
    transport: str = "stdio"  # "stdio" | "http" (auto) | "streamable-http" | "sse"
    url: str = ""  # For http/sse transports
    docs_url: str = ""
    needs_args: bool = False  # True if preset requires extra positional args (path, URL, etc.)
    oauth: bool = False  # True if preset uses OAuth authentication (remote HTTP servers)


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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        oauth=True,
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
        package="@brave/brave-search-mcp-server",
        command="npx",
        args=["-y", "@brave/brave-search-mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="BRAVE_API_KEY",
                label="API Key",
                placeholder="BSA...",
            ),
        ],
        docs_url="https://github.com/anthropics/brave-search-mcp-server",
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
        package="mcp-server-fetch",
        command="uvx",
        args=["mcp-server-fetch"],
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
        category="ai",
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
        category="ai",
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
        package="mcp-server-git",
        command="uvx",
        args=["mcp-server-git"],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    # ── New verified presets ─────────────────────────────────────────────
    MCPPreset(
        id="tavily-search",
        name="Tavily Search",
        description="AI-optimized search engine with web and news search",
        icon="search",
        category="search",
        package="tavily-mcp",
        command="npx",
        args=["-y", "tavily-mcp@latest"],
        env_keys=[
            EnvKeySpec(
                key="TAVILY_API_KEY",
                label="API Key",
                placeholder="tvly-...",
            ),
        ],
        docs_url="https://github.com/tavily-ai/tavily-mcp",
    ),
    MCPPreset(
        id="firecrawl",
        name="Firecrawl",
        description="Web scraping and crawling with structured data extraction",
        icon="flame",
        category="search",
        package="firecrawl-mcp",
        command="npx",
        args=["-y", "firecrawl-mcp"],
        env_keys=[
            EnvKeySpec(
                key="FIRECRAWL_API_KEY",
                label="API Key",
                placeholder="fc-...",
            ),
        ],
        docs_url="https://github.com/mendableai/firecrawl-mcp-server",
    ),
    MCPPreset(
        id="perplexity",
        name="Perplexity",
        description="AI-powered search with citations and real-time information",
        icon="sparkles",
        category="search",
        package="@perplexity-ai/mcp-server",
        command="npx",
        args=["-y", "@perplexity-ai/mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="PERPLEXITY_API_KEY",
                label="API Key",
                placeholder="pplx-...",
            ),
        ],
        docs_url="https://docs.perplexity.ai/guides/mcp-server",
    ),
    MCPPreset(
        id="time",
        name="Time",
        description="Get current time and timezone conversions",
        icon="clock",
        category="productivity",
        package="mcp-server-time",
        command="uvx",
        args=["mcp-server-time"],
        docs_url="https://github.com/modelcontextprotocol/servers",
    ),
    MCPPreset(
        id="neon-postgres",
        name="Neon Postgres",
        description="Manage Neon serverless Postgres databases and branches",
        icon="database",
        category="data",
        package="@neondatabase/mcp-server-neon",
        command="npx",
        args=["-y", "@neondatabase/mcp-server-neon"],
        env_keys=[
            EnvKeySpec(
                key="NEON_API_KEY",
                label="API Key",
                placeholder="neon_...",
            ),
        ],
        needs_args=True,
        docs_url="https://github.com/neondatabase/mcp-server-neon",
    ),
    MCPPreset(
        id="upstash-redis",
        name="Upstash Redis",
        description="Manage Upstash Redis databases and key-value operations",
        icon="database",
        category="data",
        package="@upstash/mcp-server",
        command="npx",
        args=["-y", "@upstash/mcp-server@latest"],
        env_keys=[
            EnvKeySpec(
                key="UPSTASH_EMAIL",
                label="Email",
                placeholder="user@example.com",
                secret=False,
            ),
            EnvKeySpec(
                key="UPSTASH_API_KEY",
                label="API Key",
                placeholder="...",
            ),
        ],
        docs_url="https://github.com/upstash/mcp-server",
    ),
    MCPPreset(
        id="netlify",
        name="Netlify",
        description="Deploy sites and manage Netlify projects via CLI auth",
        icon="globe",
        category="devops",
        package="@netlify/mcp",
        command="npx",
        args=["-y", "@netlify/mcp"],
        docs_url="https://github.com/netlify/mcp",
    ),
    MCPPreset(
        id="cloudflare-stdio",
        name="Cloudflare CLI",
        description="Manage Workers, KV, R2, and D1 via Cloudflare CLI",
        icon="cloud",
        category="devops",
        package="@cloudflare/mcp-server-cloudflare",
        command="npx",
        args=["-y", "@cloudflare/mcp-server-cloudflare"],
        env_keys=[
            EnvKeySpec(
                key="CLOUDFLARE_API_TOKEN",
                label="API Token",
                placeholder="...",
            ),
            EnvKeySpec(
                key="CLOUDFLARE_ACCOUNT_ID",
                label="Account ID",
                placeholder="...",
                secret=False,
            ),
        ],
        docs_url="https://github.com/cloudflare/mcp-server-cloudflare",
    ),
    MCPPreset(
        id="gcloud",
        name="Google Cloud",
        description="Manage GCP resources via gcloud CLI authentication",
        icon="cloud",
        category="devops",
        package="@google-cloud/gcloud-mcp",
        command="npx",
        args=["-y", "@google-cloud/gcloud-mcp"],
        docs_url="https://github.com/GoogleCloudPlatform/google-cloud-mcp",
    ),
    MCPPreset(
        id="docker",
        name="Docker",
        description="Manage containers, images, volumes, and networks",
        icon="box",
        category="devops",
        package="@docker/mcp-server-docker",
        command="npx",
        args=["-y", "@docker/mcp-server-docker"],
        docs_url="https://github.com/docker/mcp-server-docker",
    ),
    MCPPreset(
        id="circleci",
        name="CircleCI",
        description="Manage pipelines, workflows, and build insights",
        icon="circle",
        category="devops",
        package="@circleci/mcp-server-circleci",
        command="npx",
        args=["-y", "@circleci/mcp-server-circleci"],
        env_keys=[
            EnvKeySpec(
                key="CIRCLECI_TOKEN",
                label="API Token",
                placeholder="CCIPAT_...",
            ),
        ],
        docs_url="https://github.com/CircleCI-Public/mcp-server-circleci",
    ),
    MCPPreset(
        id="kubernetes",
        name="Kubernetes",
        description="Manage pods, deployments, services, and cluster resources",
        icon="server",
        category="devops",
        package="kubernetes-mcp-server",
        command="npx",
        args=["-y", "kubernetes-mcp-server@latest"],
        docs_url="https://github.com/containers/kubernetes-mcp-server",
    ),
    MCPPreset(
        id="pulumi",
        name="Pulumi",
        description="Infrastructure as code with Pulumi Cloud integration",
        icon="layers",
        category="devops",
        package="@pulumi/mcp-server",
        command="npx",
        args=["-y", "@pulumi/mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="PULUMI_ACCESS_TOKEN",
                label="Access Token",
                placeholder="pul-...",
            ),
        ],
        docs_url="https://www.pulumi.com/docs/iac/guides/ai-integration/mcp-server/",
    ),
    MCPPreset(
        id="aws",
        name="AWS",
        description="Manage AWS resources, CDK, and read AWS documentation",
        icon="cloud",
        category="devops",
        package="awslabs.core-mcp-server",
        command="uvx",
        args=["awslabs.core-mcp-server@latest"],
        docs_url="https://github.com/awslabs/mcp",
    ),
    # ── Finance ──────────────────────────────────────────────────────────
    MCPPreset(
        id="coinbase",
        name="Coinbase",
        description="Crypto payments, transfers, and wallet management",
        icon="coins",
        category="finance",
        package="@coinbase/payments-mcp",
        command="npx",
        args=["-y", "@coinbase/payments-mcp"],
        docs_url="https://github.com/coinbase/payments-mcp",
    ),
    # ── Design ───────────────────────────────────────────────────────────
    MCPPreset(
        id="canva-dev",
        name="Canva Dev",
        description="Search Canva developer docs and build Canva apps",
        icon="palette",
        category="design",
        package="@canva/cli",
        command="npx",
        args=["-y", "@canva/cli@latest", "mcp"],
        docs_url="https://www.canva.dev/docs/apps/mcp-server/",
    ),
    # ── Communication ────────────────────────────────────────────────────
    MCPPreset(
        id="twilio",
        name="Twilio",
        description="SMS, voice, and 1400+ communication APIs",
        icon="phone",
        category="communication",
        package="@twilio-alpha/mcp",
        command="npx",
        args=["-y", "@twilio-alpha/mcp"],
        env_keys=[
            EnvKeySpec(
                key="TWILIO_ACCOUNT_SID",
                label="Account SID",
                placeholder="AC...",
                secret=False,
            ),
            EnvKeySpec(
                key="TWILIO_API_KEY",
                label="API Key",
                placeholder="SK...",
            ),
            EnvKeySpec(
                key="TWILIO_API_SECRET",
                label="API Secret",
                placeholder="...",
            ),
        ],
        docs_url="https://github.com/twilio-labs/mcp",
    ),
    MCPPreset(
        id="resend",
        name="Resend",
        description="Send emails, manage contacts, domains, and broadcasts",
        icon="mail",
        category="communication",
        package="resend-mcp",
        command="npx",
        args=["-y", "resend-mcp"],
        env_keys=[
            EnvKeySpec(
                key="RESEND_API_KEY",
                label="API Key",
                placeholder="re_...",
            ),
        ],
        docs_url="https://github.com/resend/resend-mcp",
    ),
    MCPPreset(
        id="intercom",
        name="Intercom",
        description="Customer conversations, help center, and ticket management (OAuth)",
        icon="message-circle",
        category="communication",
        package="",
        transport="http",
        url="https://mcp.intercom.com/mcp",
        docs_url="https://developers.intercom.com/docs/guides/mcp",
        oauth=True,
    ),
    # ── Analytics ─────────────────────────────────────────────────────────
    MCPPreset(
        id="posthog",
        name="PostHog",
        description="Product analytics, feature flags, and error tracking (OAuth)",
        icon="bar-chart-3",
        category="analytics",
        package="",
        transport="http",
        url="https://mcp.posthog.com/mcp",
        docs_url="https://posthog.com/docs/model-context-protocol",
        oauth=True,
    ),
    MCPPreset(
        id="amplitude",
        name="Amplitude",
        description="Product analytics, cohorts, and behavioral insights (OAuth)",
        icon="activity",
        category="analytics",
        package="",
        transport="http",
        url="https://mcp.amplitude.com/mcp",
        docs_url="https://amplitude.com/docs/amplitude-ai/amplitude-mcp",
        oauth=True,
    ),
    # ── CMS / Content ────────────────────────────────────────────────────
    MCPPreset(
        id="sanity",
        name="Sanity",
        description="Structured content platform with 40+ management tools (OAuth)",
        icon="file-text",
        category="cms",
        package="",
        transport="http",
        url="https://mcp.sanity.io",
        docs_url="https://www.sanity.io/docs/compute-and-ai/mcp-server",
        oauth=True,
    ),
    MCPPreset(
        id="contentful",
        name="Contentful",
        description="Headless CMS — manage content types, entries, and assets",
        icon="layout",
        category="cms",
        package="@contentful/mcp-server",
        command="npx",
        args=["-y", "@contentful/mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="CONTENTFUL_MANAGEMENT_ACCESS_TOKEN",
                label="Management Token",
                placeholder="CFPAT-...",
            ),
            EnvKeySpec(
                key="SPACE_ID",
                label="Space ID",
                placeholder="abc123...",
                secret=False,
            ),
            EnvKeySpec(
                key="ENVIRONMENT_ID",
                label="Environment ID",
                placeholder="master",
                secret=False,
                required=False,
            ),
        ],
        docs_url="https://github.com/contentful/contentful-mcp-server",
    ),
    # ── Marketing ─────────────────────────────────────────────────────────
    MCPPreset(
        id="hubspot",
        name="HubSpot",
        description="CRM contacts, deals, tickets, and marketing tools",
        icon="target",
        category="marketing",
        package="@hubspot/mcp-server",
        command="npx",
        args=["-y", "@hubspot/mcp-server"],
        env_keys=[
            EnvKeySpec(
                key="PRIVATE_APP_ACCESS_TOKEN",
                label="Private App Token",
                placeholder="pat-...",
            ),
        ],
        docs_url="https://developers.hubspot.com/mcp",
    ),
    # ── Monitoring / Observability ────────────────────────────────────────
    MCPPreset(
        id="grafana",
        name="Grafana",
        description="Dashboards, alerting, and observability queries",
        icon="gauge",
        category="monitoring",
        package="mcp-grafana",
        command="uvx",
        args=["mcp-grafana"],
        env_keys=[
            EnvKeySpec(
                key="GRAFANA_URL",
                label="Grafana URL",
                placeholder="https://grafana.example.com",
                secret=False,
            ),
            EnvKeySpec(
                key="GRAFANA_SERVICE_ACCOUNT_TOKEN",
                label="Service Account Token",
                placeholder="glsa_...",
            ),
        ],
        docs_url="https://github.com/grafana/mcp-grafana",
    ),
    MCPPreset(
        id="pagerduty",
        name="PagerDuty",
        description="Incidents, on-call schedules, and alert management",
        icon="bell-ring",
        category="monitoring",
        package="pagerduty-mcp",
        command="uvx",
        args=["pagerduty-mcp"],
        env_keys=[
            EnvKeySpec(
                key="PAGERDUTY_USER_API_KEY",
                label="User API Key",
                placeholder="u+...",
            ),
        ],
        docs_url="https://github.com/PagerDuty/pagerduty-mcp-server",
    ),
    # ── Security / Identity ───────────────────────────────────────────────
    MCPPreset(
        id="auth0",
        name="Auth0",
        description="Identity management — apps, users, roles, and logs",
        icon="shield",
        category="security",
        package="@auth0/auth0-mcp-server",
        command="npx",
        args=["-y", "@auth0/auth0-mcp-server"],
        docs_url="https://github.com/auth0/auth0-mcp-server",
    ),
    # ── AI / ML ──────────────────────────────────────────────────────────
    MCPPreset(
        id="huggingface",
        name="Hugging Face",
        description="Search models, datasets, Spaces, and run Gradio apps (OAuth)",
        icon="cpu",
        category="ai",
        package="",
        transport="http",
        url="https://huggingface.co/mcp",
        docs_url="https://huggingface.co/docs/hub/en/hf-mcp-server",
        oauth=True,
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
        oauth=preset.oauth,
    )
