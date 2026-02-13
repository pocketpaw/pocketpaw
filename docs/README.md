# PocketPaw Documentation

The official documentation for [PocketPaw](https://github.com/pocketpaw/pocketpaw) — a self-hosted AI agent controlled via Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Teams, Google Chat, or a web dashboard.

**Live site:** [docs.pocketpaw.xyz](https://docs.pocketpaw.xyz)

## Stack

Built with [Lito Docs](https://lito.rohitk06.in) — an open-source tool that converts Markdown/MDX into beautiful, searchable documentation sites with zero configuration. Lito provides the Astro-based SSG, Pagefind search, 20+ MDX components, and light/dark theming out of the box.

## Structure

```
docs/
├── docs-config.json          # Site config (nav, branding, SEO, search)
├── _landing/                  # Custom HTML/CSS landing page
├── public/                    # Static assets (logos, OG images)
├── introduction/              # Welcome & overview
├── getting-started/           # Install, quick-start, config, project structure
├── concepts/                  # Architecture, message bus, agent loop, memory, tools, security
├── channels/                  # 9+ channel guides (Telegram, Discord, Slack, WhatsApp, etc.)
├── backends/                  # Claude Agent SDK, PocketPaw Native, Open Interpreter
├── tools/                     # 50+ built-in tools
├── integrations/              # OAuth, Gmail, Calendar, Drive, Docs, Spotify, Reddit, MCP
├── security/                  # Guardian AI, injection scanner, audit log/CLI/daemon
├── memory/                    # File store, Mem0, sessions, context building, isolation
├── advanced/                  # Model router, plan mode, scheduler, skills, Deep Work, Mission Control
├── deployment/                # Self-hosting, Docker, systemd
└── api/                       # 39 REST endpoint docs + WebSocket protocol
```

## Local Development

No `package.json` or Astro config needed — Lito handles everything.

### Preview

```bash
npx --yes @litodocs/cli dev -i .
```

### Build

```bash
npx --yes @litodocs/cli build -i .
```

Output goes to `./dist/`.

## Deployment

The site auto-deploys to GitHub Pages on push to `main` via the workflow in `.github/workflows/deploy-docs.yml`.

To deploy manually, build and upload the `dist/` folder to any static hosting provider (Vercel, Netlify, Cloudflare Pages, etc.).

## Adding Pages

1. Create an `.mdx` file in the appropriate directory with YAML frontmatter:
   ```mdx
   ---
   title: Page Title
   description: "A 150-160 character description with front-loaded keywords."
   section: Section Name
   ogType: article
   keywords: ["keyword1", "keyword2", "keyword3"]
   tags: ["tag1", "tag2"]
   ---

   Content here...
   ```
2. Add the page to the `navigation.sidebar` array in `docs-config.json`.

## MDX Components

Provided by Lito — no local definitions needed:

| Component | Usage |
|---|---|
| `<Card>`, `<CardGroup>` | Feature cards with icons and links |
| `<Steps>`, `<Step>` | Numbered step sequences |
| `<Tabs>`, `<Tab>` | Tabbed content blocks |
| `<Callout>` | Info/warning/tip callouts |
| `<ResponseField>` | API field documentation |
| `<RequestExample>`, `<ResponseExample>` | API example blocks |

## License

MIT
