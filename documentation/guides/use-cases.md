# Use Cases

Real-world examples of what PocketPaw can do. Each section shows what you'd say and what happens.

---

## Personal Automation

### File Organization

```
You:  "Organize my Downloads folder. PDFs by date, images by type, delete duplicates."
Paw:  *scans 200+ files, categorizes, moves 47, removes 12 duplicates*
Paw:  "Done. Moved 47 files into folders, removed 12 duplicates (saved 340MB).
       Here's what changed: [summary]"
```

Works for any directory. Ask it to clean your Desktop, sort project files, or archive old documents.

### Recurring Reminders

```
You:  "Every Sunday at 6pm, check the recycling calendar and tell me which bins to put out."
Paw:  *creates a cron schedule, persists across restarts*
Paw:  "Set. I'll message you every Sunday at 6pm."
```

```
You:  "Remind me to check email every morning at 9am."
Paw:  "Done. Daily reminder at 9:00 AM."
```

Reminders are stored in `~/.pocketclaw/reminders.json` and survive restarts. Natural language time parsing handles "in 5 minutes", "tomorrow at 3pm", "every weekday at noon".

### Email Management

```
You:  "Check my inbox for anything urgent from this week."
Paw:  *searches Gmail via OAuth, reads subject lines and previews*
Paw:  "3 emails need attention:
       1. Invoice from AWS (due Friday)
       2. Meeting reschedule from Sarah
       3. Deploy approval request from CI"
```

```
You:  "Draft a reply to Sarah saying Tuesday works better."
Paw:  *composes and sends via Gmail API*
```

Requires Google OAuth setup. No app passwords needed.

### Calendar

```
You:  "What's on my calendar tomorrow?"
Paw:  "You have 3 meetings:
       9:00  Stand-up (30 min)
       11:00 Design review with Alex (1 hr)
       2:00  1:1 with manager (30 min)"
```

```
You:  "Schedule a 30-minute meeting with the team next Wednesday at 2pm."
Paw:  *creates Google Calendar event, sends invites*
```

---

## Research & Browsing

### Web Research

```
You:  "Research the best self-hosted analytics tools. Compare Plausible, Umami, and Matomo."
Paw:  *searches the web, reads documentation pages, cross-references features*
Paw:  "Here's a comparison:
       - Plausible: Lightweight, privacy-focused, no cookies. $9/mo cloud or self-host.
       - Umami: Free, open source, simple UI. Needs Node + Postgres.
       - Matomo: Full GA replacement, most features, heaviest setup.
       Full write-up saved to ~/research/analytics-comparison.md"
```

Uses Tavily or Brave for search, Playwright for reading pages, LLM for synthesis.

### Link Summarization

```
You:  *pastes a URL*
Paw:  *opens browser, reads the page, identifies key points*
Paw:  "3 takeaways from that article:
       1. ...
       2. ...
       3. ...
       Want me to save this to memory?"
```

### Monitoring & Alerts

```
You:  "Every 6 hours, check Hacker News for posts mentioning 'self-hosted AI agent'
       and send me a Telegram message if anything new shows up."
Paw:  *creates a cron job, searches on schedule, deduplicates against previous results*
```

Works with any data source accessible via browser or API. Connect your own sources via MCP servers for custom monitoring.

---

## Coding & DevOps

### Bug Hunting

```
You:  "The app crashes after about 2 hours. Find the memory leak."
Paw:  *reads logs, profiles code, traces the issue*
Paw:  "Found it. The WebSocket handler in dashboard.py never closes connections
       on client disconnect. Connections accumulate until OOM.
       Here's the fix: [diff]"
```

### Code Changes

```
You:  "Add rate limiting to the /api/search endpoint. 10 requests per minute per IP."
Paw:  *reads the existing endpoint, checks what middleware is available*
Paw:  *writes the code, adds tests*
Paw:  "Done. Added rate limiting with a 10/min/IP limit.
       Tests pass. Want me to commit?"
```

### Project Scaffolding

```
You:  "Set up a new FastAPI project with auth, a Postgres database, and Docker Compose."
Paw:  *creates directory structure, writes boilerplate, configures Docker*
Paw:  "Project scaffolded at ~/projects/new-api/.
       Run 'docker compose up' to start. Includes:
       - FastAPI with JWT auth
       - Postgres + Alembic migrations
       - Docker Compose with hot reload"
```

---

## Multi-Agent Workflows (Mission Control)

Mission Control is for tasks too complex for a single prompt. You describe the goal and PocketPaw breaks it into tasks, assigns agents, and runs them.

### Competitor Analysis

```
You:  "I need a competitor analysis report for our product launch."
Paw:  *creates a project, spins up 3 agents*
Paw:  "3 agents working on it:
       - Agent A: Researching competitor websites and pricing
       - Agent B: Analyzing feature comparisons
       - Agent C: Writing the final report (waiting on A and B)
       I'll ping you when it's ready."
```

30 minutes later:

```
Paw:  "Report done. Saved to the project documents.
       Key finding: two competitors just raised prices 20%.
       Want me to summarize the main gaps?"
```

### Content Pipeline

```
You:  "Write a blog post about our new security features.
       Research what competitors are doing, find an angle, write the draft."
Paw:  *research agent scans competitor blogs and changelogs*
Paw:  *writer agent drafts the post using research findings*
Paw:  "Draft ready. 1,200 words. Angle: most tools bolt on security after launch,
       we built it in from day one. Want to review?"
```

### Codebase Refactoring

```
You:  "Refactor the authentication module. Move from session-based to JWT.
       Don't break existing tests."
Paw:  *creates tasks: audit current auth, design JWT flow, implement, update tests*
Paw:  *runs tasks sequentially, verifying tests pass after each step*
Paw:  "Refactoring complete. 14 files changed, all 47 tests pass.
       Here's what changed: [summary]"
```

---

## Data & Integration (via MCP)

PocketPaw connects to Model Context Protocol servers, so you can plug in your own data sources.

### Custom Data Pipeline

```
You:  "Connect to my sales database MCP. Every morning, check yesterday's numbers
       and flag anything unusual."
Paw:  *queries MCP server on schedule, compares against historical averages*
Paw:  "Monday morning report:
       - Revenue: $12,400 (normal)
       - Signups: 3 (down 70% from weekly average)
       - Churn: 2 accounts canceled (both enterprise)
       The signup drop looks like something to investigate."
```

### Document Management

```
You:  "Pull the Q4 report from Google Drive, summarize it,
       and create a 5-slide outline for the board meeting."
Paw:  *downloads from Drive, reads content, generates outline*
Paw:  "Outline ready:
       1. Q4 Revenue Summary
       2. Growth Metrics vs Plan
       3. Key Wins
       4. Challenges & Mitigation
       5. Q1 Priorities
       Want me to create the slides in Docs?"
```

---

## Fully Local (Ollama)

Everything above works with Ollama instead of Claude/GPT. No API keys, no data leaving your machine.

```
You:  *sets llm_provider to "ollama" in settings*
Paw:  *auto-detects local Ollama instance*
Paw:  "Connected to Ollama (llama3.2). Everything stays local."
```

Best for: privacy-sensitive tasks, offline use, or when you don't want to pay for API credits on simple tasks.

Tool calling works with local models for simpler tasks (file operations, scheduling, web search). Complex multi-step workflows still work better with larger models.

---

## Talk to PocketPaw From Anywhere

All of the above works from any connected channel:

| Channel | How |
| --- | --- |
| **Web Dashboard** | Default. Opens at `localhost:8888`. |
| **Telegram** | Scan QR code, chat from your phone. |
| **Discord** | `/paw` slash command or DM the bot. |
| **Slack** | Mention `@PocketPaw` or DM in Socket Mode. |
| **WhatsApp** | Business API webhook. |

Set up channels from the sidebar in the web dashboard or run headless:

```bash
pocketpaw --discord --slack    # Multiple channels, no dashboard
```
