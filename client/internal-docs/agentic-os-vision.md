# PocketPaw: The Agentic OS

> **Vision**: The experience layer that turns any computer into an AI-native machine.

Not a new operating system. A new shell. The OS already manages files, processes, networking, hardware. What's missing is the intent layer — the thing that sits between what you want and how the system does it.

PocketPaw is that layer.

---

## The Landscape

Three products are converging on the same idea from different angles.

### Claude Cowork (Anthropic)

Launched January 2026. Built on the same foundation as Claude Code.

**What it is**: A desktop app where you give Claude access to a folder, describe what you want, and it works through it — creating documents, organizing files, filling forms, running multi-step workflows. "Much less like a back-and-forth and much more like leaving messages for a coworker."

**Architecture**: Sandboxed via Apple's VZVirtualMachine framework — boots a custom Linux filesystem inside a VM. Files mounted at `/sessions/{name}/mnt/{folder}`. Explicit opt-in per folder.

**Strengths**:
- Backed by Anthropic. Same agent SDK that powers Claude Code.
- Sandboxing is serious — VM-level isolation, not just file permissions.
- Parallel task queuing. Skills for document types. Browser integration via Chrome.
- $20/mo (Pro) to $200/mo (Max). Free tier with waitlist.

**Weaknesses**:
- Closed source. Locked to Anthropic's models. No Ollama for the core product.
- Desktop-only. No mobile. No Telegram. No Discord. If you're away from your laptop, you can't interact.
- Not always-on. You open the app, give it a task, wait. No daemon mode, no proactive notifications, no background missions.
- Simon Willison flagged: prompt injection is real, and "monitor Claude for suspicious actions" is unrealistic for normal users. No Guardian AI equivalent.
- $20-200/month with no self-hosted option. Your data routes through Anthropic (for non-local models).

### Open Interpreter

Pivoted from CLI tool to desktop agent for document work.

**What it is**: "Work alongside agents that can edit your documents, fill PDF forms, and more." Full editors for Word, Excel, PDF. Describe what you need or edit manually. GUI control via computer vision.

**Strengths**:
- Open source roots. Ollama support. Free tier with own API keys.
- Document-focused UX — Word with tracked changes, Excel with pivot tables, PDF form filling.
- $20/mo paid tier. Simple pricing.
- GUI control capabilities (screen reading, mouse/keyboard automation).

**Weaknesses**:
- No channel adapters. Desktop-only interaction. No mobile access.
- No security layer. No audit trail. No permission system beyond basic file access.
- No memory system. No persistent context across sessions.
- No background execution. No daemon mode. No scheduler.
- Pivoted from open source CLI to commercial desktop app — community trust eroded.

### The Gap Both Miss

Neither product is always-on. Neither works from your phone. Neither has a security stack that makes autonomous execution safe. Neither compounds knowledge over time. Neither orchestrates across multiple services.

They're both **apps you open**. PocketPaw should be **infrastructure that's already running**.

---

## PocketPaw's Position

PocketPaw already has primitives that map to OS concepts:

| OS Concept | Traditional OS | PocketPaw Today | Cowork | Open Interpreter |
|---|---|---|---|---|
| Shell | bash, Terminal | Chat (Telegram/Discord/Web) | Desktop chat window | Desktop chat window |
| Programs | /usr/bin/ | Skills system | Skills (doc types) | Built-in editors |
| App store | apt, brew | Skills marketplace (planned) | None | None |
| Process manager | systemd | Mission Control / AgentLoop | Parallel task queue | Single task |
| Daemons | Background services | Scheduler, self-audit | None | None |
| File system | ext4, APFS | Memory system (local) | Folder access (VM) | File access |
| Permissions | chmod, sudo | Guardian AI + tool policy | VM sandbox | None |
| Notifications | Notification Center | Channel adapters (push to you) | In-app only | In-app only |
| Pipes | stdout, stdin | MessageBus (events) | None | None |
| Cron | crontab | Scheduled workflows | None | None |
| Multi-user | /etc/passwd | User lock (team planned) | Per-subscription | None |
| Remote access | SSH | Telegram/Discord from anywhere | None | None |

The insight: **Cowork and Open Interpreter are apps. PocketPaw is infrastructure.**

---

## Three Clients, One Core

PocketPaw serves three user profiles through three interfaces — all powered by the same Python core.

```
┌─────────────────────────────────────────────────────────┐
│                    User Interfaces                       │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  TUI        │  │  Desktop     │  │  Web          │  │
│  │  (Terminal)  │  │  (Tauri)     │  │  Dashboard    │  │
│  │             │  │              │  │               │  │
│  │  Power      │  │  Consumer    │  │  Mid-range    │  │
│  │  users,     │  │  track,      │  │  users,       │  │
│  │  devs,      │  │  non-tech,   │  │  remote       │  │
│  │  servers    │  │  daily       │  │  access,      │  │
│  │             │  │  driver      │  │  teams        │  │
│  └──────┬──────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                │                   │          │
│         └────────────────┼───────────────────┘          │
│                          │                              │
│              ┌───────────▼────────────┐                 │
│              │    Svelte Component    │                 │
│              │       Library          │                 │
│              │  (shared UI layer)     │                 │
│              └───────────┬────────────┘                 │
│                          │                              │
├──────────────────────────┼──────────────────────────────┤
│              ┌───────────▼────────────┐                 │
│              │    PocketPaw Core      │                 │
│              │   (Python backend)     │                 │
│              │                        │                 │
│              │  MessageBus            │                 │
│              │  AgentLoop (6 backends)│                 │
│              │  Memory System         │                 │
│              │  Skills Engine         │                 │
│              │  Guardian AI           │                 │
│              │  Channel Adapters      │                 │
│              │  REST API + WebSocket  │                 │
│              └────────────────────────┘                 │
└─────────────────────────────────────────────────────────┘
```

### TUI — Terminal User Interface (power users)

`pocketpaw --tui` launches a rich terminal interface. Think: Lazygit or K9s, but for your agent.

- Chat pane + activity log side by side
- Keyboard-driven (vim-like bindings)
- Works over SSH — manage your server's agent from anywhere
- Zero GUI dependencies — runs on headless servers, WSL, containers
- Built with Textual (Python TUI framework) — same language as core, no extra build chain
- First-class citizen, not a fallback. Developers prefer this.

**Who uses it**: developers, sysadmins, server deployments, anyone who lives in the terminal.

### Desktop Client — Tauri App (consumer track)

The primary "Canva for AI agents" surface. Minimal, chat-first, approachable.

**Like Telegram Desktop, not VS Code.** A chat window with a sidebar. Non-tech users download it, open it, start chatting. No terminal. No config files. No pip.

- System tray presence — starts on boot, always reachable
- Native OS notifications with action buttons
- Chat-first interface with visual skill cards (see: desktop-client-ux.md)
- Three-screen onboarding wizard (see: desktop-client-ux.md)
- Drag-and-drop file handling — richer than any chat channel
- Embeds the same Svelte components as the web dashboard

**The killer feature: Universal AI Side Panel.** A floating chat panel (separate Tauri window) that docks to the screen edge and works alongside ANY application — Word, Excel, VS Code, Chrome, Figma, Terminal. Detects the active file via OS APIs, gives the agent context automatically. User says "add a budget section" and the agent edits the .docx on disk; Word prompts to reload. No built-in editor needed — the agent meets users wherever they already work. Full spec in `desktop-client-ux.md`.

**Who uses it**: non-technical users, knowledge workers, the "Canva" audience.

### Web Dashboard — Browser-based (current)

The existing web interface, upgraded from Alpine.js to Svelte. Serves two purposes:

1. **Remote access**: access PocketPaw from any device with a browser (phone, tablet, work laptop)
2. **Team interface**: shared dashboard for team deployments
3. **Fallback**: users who don't want to install anything

**Who uses it**: remote users, teams, people who just want a browser tab.

### Shared Component Architecture (Svelte)

The current Alpine.js + Jinja2 frontend has served well but is hitting complexity limits. Svelte solves this and enables component reuse across all three clients.

**Why Svelte:**
- Compiled, not runtime — smallest bundle size of any framework
- No virtual DOM overhead — fast on low-end hardware
- Svelte components compile to vanilla JS — works in Tauri's webview, web dashboard, even embedded in the TUI via a webview panel
- SvelteKit for the web dashboard, raw Svelte components for Tauri
- Growing ecosystem, strong community trajectory

**The reuse pattern:**

```
src/ui/                          # Shared Svelte component library
├── components/
│   ├── Chat.svelte              # Chat interface (messages, input, streaming)
│   ├── SkillCard.svelte         # Visual skill browser
│   ├── SessionList.svelte       # Session sidebar
│   ├── ActivityFeed.svelte      # Agent activity log
│   ├── SettingsPanel.svelte     # Configuration UI
│   ├── MemoryViewer.svelte      # Knowledge base browser
│   └── OnboardingWizard.svelte  # First-run setup
├── stores/
│   ├── agent.ts                 # Agent state (via WebSocket)
│   ├── settings.ts              # Settings sync
│   └── sessions.ts              # Session management
└── lib/
    └── websocket.ts             # WebSocket client (shared)

apps/
├── desktop/                     # Tauri shell
│   ├── src-tauri/               # Rust: system tray, hotkey, notifications
│   └── src/                     # Svelte: imports from src/ui/
├── web/                         # SvelteKit web dashboard
│   └── src/                     # Svelte: imports from src/ui/
└── tui/                         # Textual TUI (Python)
    └── src/                     # Python: own UI, calls same REST/WS API
```

**One component library. Three rendering targets.** The Chat component, SkillCard, SessionList — all written once in Svelte, used in both the Tauri desktop app and the SvelteKit web dashboard. The TUI is Python-native (Textual) but consumes the same REST API and WebSocket protocol, so the backend doesn't care which client is connected.

### What the Desktop App Adds Over the Web Dashboard

The desktop client isn't just "the web dashboard in a window." Tauri's Rust backend provides OS-level capabilities:

| Capability | Web Dashboard | Desktop (Tauri) | TUI |
|---|---|---|---|
| Chat interface | Yes | Yes | Yes |
| Skill cards | Yes | Yes | Text-based |
| Settings | Yes | Yes | Yes |
| System tray / menu bar | No | Yes | No |
| Start on boot | No | Yes | Via systemd/launchd |
| Native notifications | Browser only | OS-native with actions | Terminal bell |
| Global hotkey | No | Yes (Cmd+Shift+P) | No |
| File drag-and-drop | Limited | Full native | No |
| Offline indicator | Yes | Yes + tray icon color | Yes |
| Auto-update | N/A | Built-in (Tauri updater) | pip/uv |

The desktop app is the premium experience. The web dashboard is the accessible fallback. The TUI is the power tool. All three are first-class.

---

## Feature Breakdown: What to Build, When

### Phase 0: Foundation (v0.5) — "The daemon"

Before the desktop app, PocketPaw needs to be always-on.

- **Daemon mode**: `pocketpaw --daemon` runs in background, no browser needed
- **Systemd/launchd integration**: auto-start on boot, auto-restart on crash
- **Scheduler v2**: cron-like scheduled workflows (morning briefing, nightly backup check, weekly review)
- **Reply-to context in Telegram**: verify `reply_to_message` passthrough for "Ok Fix This" pattern
- **Knowledge ingestion**: drop URLs/files/screenshots → auto-embed, locally searchable

This is buildable today with existing architecture. No desktop app needed yet.

### Phase 1: Desktop Shell (v0.6) — "The side panel"

The minimum desktop app that's genuinely different from a browser tab.

- **Main window**: Telegram-like chat client with skill cards (Svelte, shared components)
- **Universal AI Side Panel**: floating chat window docked to screen edge, works alongside any app. Detects active file/app via OS APIs. User chats to edit documents, code, data — agent writes to disk, native app reloads. The defining feature.
- **System tray agent**: starts on boot, lives in menu bar, status indicator
- **Native notifications**: OS-level with action buttons
- **Global hotkey**: Cmd+Shift+P for quick ask, Cmd+Shift+L to toggle side panel
- **Three-screen onboarding**: download → choose AI (Ollama free / API key) → start chatting

Tech choice: **Tauri v2** (~5MB binary, Rust sidecar for OS integration, webview renders same Svelte components as web dashboard). Full UX spec in `desktop-client-ux.md`.

### Phase 2: OS Integration (v0.7) — "The aware agent"

The agent starts noticing things without being asked.

- **File system watcher**: monitor granted folders for changes
  - "You downloaded 3 invoices. Want me to organize them?"
  - "New screenshot detected. Want me to extract the text?"
  - "config.json was modified outside PocketPaw. Review changes?"
- **Clipboard monitor** (opt-in): copy a URL → agent summarizes in background. Copy code → agent explains. Copy an error → agent diagnoses.
- **Calendar integration**: "You have a meeting with Sarah in 20 min. Last time you discussed the pricing rewrite."
- **Proactive notifications**: agent surfaces things it noticed, not just responses to commands

### Phase 3: Blueprints & Command Centers (v0.7-0.8) — "The platform"

The defining shift from "app" to "platform." Users build their own AI-powered dashboards through conversation. Domain experts publish them as installable templates (Blueprints).

- **Configurable layouts** (v0.7): Panel components (table, chart, kanban, metrics, feed), layout renderer, Blueprint YAML schema. Current Deep Work becomes the "Project Orchestrator" Blueprint.
- **Build through conversation** (v0.7): "I need a command center for my Etsy shop" → PocketPaw generates the dashboard, workflows, and skills. User refines via chat.
- **Blueprint Marketplace** (v0.8): Browse, install, publish. Free tier first, paid Blueprints later. 5-10 official Blueprints at launch.

This is the "Canva templates" moment. Templates turn blank canvases into useful tools. Blueprints turn a blank agent into a purpose-built command center. Full strategy in `blueprints-strategy.md`.

### Phase 4: Cross-App Orchestration + Marketplace v2 (v0.9-1.0) — "The OS"

The agent works across app boundaries. The marketplace matures.

- **Integration mesh**: Gmail + Calendar + GitHub + Slack + Notion + Todoist
- **Trigger → Action workflows**: "When I get an email from client X, summarize it and create a task in Todoist"
- **Cross-app context**: "Take the action items from today's meeting notes, create GitHub issues for engineering, add business items to Todoist, and post a summary in #team-updates"
- **Background missions**: persistent multi-day tasks that monitor and act
- **Premium Blueprints**: paid marketplace with revenue split (70/30), creator profiles, version management
- **Pattern learning**: agent notices your habits and suggests automation

---

## Use Cases That Sell It

### "Ok Fix This" (Developer daily driver)
Server sends error to Telegram. You reply "fix this." PocketPaw diagnoses, Guardian AI reviews the fix, applies it, restarts the service. From your phone. At dinner.

**Why PocketPaw wins**: Guardian AI prevents the agent from running destructive commands. Cowork can't do this from a phone. Open Interpreter has no security layer.

### "Morning Briefing" (Knowledge worker retention hook)
Every morning at 7am, PocketPaw sends to Telegram: weather, calendar summary, 3 emails that need attention, yesterday's saved articles organized by topic, and that task you keep postponing.

**Why PocketPaw wins**: always-on daemon + scheduler + channel adapters. Neither Cowork nor Open Interpreter can push to you — you have to open them.

### "Inbox Zero Agent" (Cross-app orchestration)
"Go through my email. Archive newsletters. Flag anything from clients. Draft replies for the 3 most urgent. Put meeting requests on my calendar."

**Why PocketPaw wins**: integration mesh + memory (knows your clients, your preferences, your communication style from past interactions).

### "Receipt to Expense Report" (Document automation)
Drop photos of receipts into a folder. PocketPaw detects them (file watcher), extracts amounts/vendors/dates (OCR + AI), creates an Excel spreadsheet with categories and totals, saves it to your Dropbox.

**Why PocketPaw wins**: file watcher + document generation + background execution. No manual trigger needed.

### "Research Assistant" (Second brain)
Throughout the day, drop URLs, PDFs, screenshots into Telegram. PocketPaw ingests everything, embeds for semantic search. Friday: "Summarize everything I saved this week about competitor pricing." Gets a structured report.

**Why PocketPaw wins**: local knowledge base + memory that compounds. Cowork has no persistent memory across sessions.

### "Production Ops" (Server management from anywhere)
PocketPaw runs as a daemon on your server. Monitors logs, disk space, service health. Sends alerts to Telegram. You respond with commands: "restart nginx", "roll back to last deploy", "show me the last 50 error logs." Guardian AI prevents catastrophic commands.

**Why PocketPaw wins**: daemon mode + Telegram + Guardian AI. This is the use case where security isn't optional — it's the entire value proposition.

---

## Competitive Positioning

```
                    Always-On
                       ▲
                       │
                       │    ★ PocketPaw
                       │    (daemon + channels + security)
                       │
          ─────────────┼─────────────────►
          Desktop-Only │           Multi-Channel
                       │
          Cowork ●     │
          (folder VM)  │
                       │
          OI ●         │
          (doc editors)│
                       │
                    App-Like
```

**Open Interpreter** = desktop document editor with AI. Competes with Office. Forces you into their editor.
**Claude Cowork** = desktop file agent with sandbox. Competes with yourself manually organizing files. Forces you into their sandbox.
**PocketPaw** = always-on agent OS layer with security. Competes with *not having an AI running your machine.* Meets you wherever you already work.

The pitch: "Cowork is an app you open. Open Interpreter is an editor you learn. PocketPaw is a side panel that works with everything you already use."

---

## Architecture: Why PocketPaw Can Do This

The existing architecture already supports every client surface. Nothing needs to be rewritten — it needs to be extended.

```
Clients (pick your interface):
  TUI (Textual) ──────────┐
  Desktop (Tauri+Svelte) ──┤──→ REST API + WebSocket ──→ PocketPaw Core
  Web (SvelteKit) ─────────┤
  Telegram/Discord/Slack ──┘

PocketPaw Core (Python, unchanged):
  MessageBus → AgentLoop → 6 Backends → Tool Registry
  Memory System → Scheduler → Guardian AI → Audit Log
  Channel Adapters: Telegram, Discord, Slack, WhatsApp

Add for Agentic OS (later phases):
  Tauri Rust sidecar → System Tray + Hotkey + Native Notifs
  OS Hooks → File Watcher + Clipboard Monitor
  Integration Layer → Gmail, Calendar, GitHub, Notion
  Proactive Engine → Watches signals, surfaces relevant info
  Background Missions → Long-running persistent tasks
```

The MessageBus is the key. Everything already flows through events. Every client — TUI, desktop, web, Telegram — connects via the same WebSocket protocol and REST API. The desktop shell's Rust sidecar can additionally publish OS-level events (FileChanged, HotkeyPressed) onto the same bus. The AgentLoop consumes them the same way it consumes Telegram messages today.

No new architecture. Just new surfaces and event sources.

---

## What NOT to Do

**Don't build document editors.** Open Interpreter went this route — Word/Excel/PDF editors inside their app. PocketPaw's agent should *create* files, not host editors. Let the user open files in their preferred app. You don't compete with Office. You automate it.

**Don't require the desktop app.** The desktop shell is a premium experience layer, not a requirement. Everything should work via Telegram/Discord/Web too. The daemon mode + channel adapters are the foundation. The desktop app adds OS integration on top. Users who never install the desktop app should still get 80% of the value.

**Don't sandbox in a VM (yet).** Cowork's VZVirtualMachine approach is impressive but complex. PocketPaw's Guardian AI + tool policy + file_jail is a lighter-weight security model that's good enough for v1. VM sandboxing is a v2 consideration.

**Don't go closed source.** Cowork is closed. Open Interpreter pivoted commercial. PocketPaw's open source identity is a competitive advantage — the community trust, the self-hosting story, the "you own your data" pitch. Keep the core open. Monetize with team features, hosted version, premium integrations.

**Don't announce "Agentic OS" before it feels like one.** Ship daemon mode. Ship the scheduler. Ship the knowledge base. Ship the desktop tray agent. Let users say "this feels like an AI OS." Then own the positioning. Premature branding kills credibility (see: Rabbit R1, Humane AI Pin).

---

## The Tagline Evolution

| Version | Tagline | What's new |
|---|---|---|
| v0.4 (now) | "An AI agent that runs on your machine, not someone else's" | Multi-backend, dashboard, channels |
| v0.5 | "Your AI is always on" | Daemon mode, scheduler, knowledge base |
| v0.6 | "Your AI lives in your system tray" | Desktop app, native notifications, hotkey |
| v0.7 | "Your AI notices things before you do" | File watcher, clipboard, proactive alerts |
| v0.8 | "Your AI handles the paperwork" | Document generation, receipt processing |
| v1.0 | "The operating system for the agentic era" | Cross-app orchestration, background missions |

---

## Revenue Model

| Tier | Price | What you get |
|---|---|---|
| **Core** | Free forever | CLI + web dashboard + all channels + Ollama. Open source. Self-hosted. |
| **Desktop** | Free | Desktop app with tray agent, notifications, hotkey. Still self-hosted, still open source. |
| **Pro** | $10/mo | Priority model routing, advanced integrations (Gmail, Calendar), premium skills, cloud backup of memory |
| **Team** | $20/user/mo | Multi-user, shared knowledge base, admin dashboard, per-user permissions, audit analytics |
| **Enterprise** | Custom | Hosted infrastructure, SSO, compliance, dedicated support |

The core agent is always free and open source. That's the growth engine. You charge for convenience (Pro), collaboration (Team), and compliance (Enterprise).

---

## Summary

PocketPaw's path to "Agentic OS" is not about building a new operating system. It's about building the missing layer between human intent and system capabilities.

The advantage over Cowork: always-on, multi-channel, open source, self-hosted.
The advantage over Open Interpreter: security stack, persistent memory, background execution, channels.
The advantage over both: you don't have to be at your desk.

The agent isn't an app you open. It's infrastructure that's already running. That's the OS.

---

## Sources

- [Claude Cowork announcement](https://claude.com/blog/cowork-research-preview) — Anthropic, January 2026
- [Simon Willison's first impressions of Cowork](https://simonw.substack.com/p/first-impressions-of-claude-cowork) — architecture details, security concerns
- [Open Interpreter](https://www.openinterpreter.com/) — desktop agent, document editing, pricing
- [Cowork on Windows](https://venturebeat.com/technology/anthropics-claude-cowork-finally-lands-on-windows-and-it-wants-to-automate) — VentureBeat, February 2026
- [Cowork tutorial](https://www.datacamp.com/tutorial/claude-cowork-tutorial) — DataCamp guide
