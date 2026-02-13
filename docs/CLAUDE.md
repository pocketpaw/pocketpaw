# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

This is the documentation site for PocketPaw, a self-hosted AI agent. The docs are 120+ MDX files consumed by an Astro-based static site generator. The built output lives in `../docs-dist/` (Astro SSG with Pagefind search and Partytown).

There is no `astro.config.*` or `package.json` in this repo — the Astro build tooling is external. The docs themselves are pure content (MDX + config JSON).

## Documentation Structure

All content is MDX (Markdown + JSX components). Every file has YAML frontmatter with `title` and `description`. API endpoint pages additionally have `api`, `baseUrl`, `layout`, and `auth` fields.

```
docs/
├── docs-config.json          # Site metadata, nav, branding, search, SEO
├── _landing/                  # Custom HTML/CSS/JS landing page (not MDX)
├── public/                    # Static assets (logos, favicon SVGs)
├── introduction/              # 2 pages — welcome, why-pocketpaw
├── getting-started/           # 4 pages — install, quick-start, config, project structure
├── concepts/                  # 6 pages — architecture, message bus, agent loop, memory, tools, security
├── channels/                  # 10 pages — overview + 9 channel guides
├── backends/                  # 4 pages — overview + 3 backend guides
├── tools/                     # 14 pages — overview + 13 tool guides + custom tools
├── integrations/              # 9 pages — overview, OAuth, Gmail, Calendar, Drive, Docs, Spotify, Reddit, MCP
├── security/                  # 6 pages — overview, Guardian AI, injection scanner, audit log/CLI/daemon
├── memory/                    # 6 pages — overview, file store, mem0, sessions, context building, memory isolation
├── advanced/                  # 7 pages — model router, plan mode, scheduler, skills, deep work, mission control, autonomous messaging
├── deployment/                # 3 pages — self-hosting, Docker, systemd
└── api/                       # 51 pages — REST endpoint docs + WebSocket protocol + config reference
```

## MDX Components Used

The docs use custom Astro/MDX components (provided by the external build tooling, not defined here):

- `<Card>`, `<CardGroup>` — feature cards with icons and links
- `<Steps>`, `<Step>` — numbered step sequences
- `<Tabs>`, `<Tab>` — tabbed content blocks (used heavily in API docs for cURL/JS/Python examples)
- `<ResponseField>` — API response field documentation (supports nesting)
- `<RequestExample>`, `<ResponseExample>` — API example request/response blocks
- `<Callout>` — info/warning/tip callouts

Icon convention: Lucide icons referenced as `lucide:icon-name` strings.

## docs-config.json

Central configuration file controlling:

- **`metadata`** — site name, description, version
- **`branding`** — colors (primary `#0A84FF`, accent `#d49a5c`), fonts (Plus Jakarta Sans / JetBrains Mono), logos
- **`navigation.sidebar`** — 11 top-level sections defining the full sidebar tree. This is the source of truth for page ordering and hierarchy
- **`navigation.navbar`** — top nav links + Guides dropdown + GitHub star + CTA button
- **`seo`** — OG image, JSON-LD, canonical URLs, breadcrumbs
- **`search`** — local Pagefind search
- **`integrations`** — edit-on-GitHub links (`/edit/main/docs/{path}`), last-updated timestamps, feedback widget

When adding new pages, they must be added to the appropriate `navigation.sidebar` section in this file to appear in the sidebar.

## API Endpoint Pages

The endpoint pages in `api/` follow a strict pattern:

```mdx
---
title: Get Channel Status
description: ...
api: GET /api/channels/status
baseUrl: http://localhost:8000
layout: '@/layouts/APIEndpointLayout.astro'
auth: bearer
---

## Overview
...

## Response
<ResponseField name="..." type="...">...</ResponseField>

<RequestExample>
<Tabs items={["cURL", "JavaScript", "Python"]}>
  <Tab title="cURL">...</Tab>
  ...
</Tabs>
</RequestExample>

<ResponseExample>
<Tabs items={["200"]}>
  <Tab title="200">...</Tab>
</Tabs>
</ResponseExample>
```

## Key Conventions

- **Package name duality**: Internal Python package is `pocketclaw`, public-facing name in docs is `PocketPaw`. Import paths use `pocketclaw` (e.g., `from pocketclaw.tools.registry import ...`).
- **Channel count**: "9+" channels (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Teams, Google Chat, Web Dashboard).
- **Tool count**: "50+" built-in tools across search, media, integrations, sessions, desktop, and coding categories.
- **Backend count**: 3 backends (Claude Agent SDK, PocketPaw Native, Open Interpreter).
- **`_images/` and `_assets/`**: Currently empty placeholder directories for future media.
- **Landing page**: `_landing/` is standalone HTML/CSS/JS, not MDX. Separate from the docs content pipeline.
- The legacy `../documentation/features/` directory has older markdown docs — the `docs/` MDX files are the canonical source.
