<p align="center">
  <img src="paw.png" alt="PocketPaw" width="100">
</p>

<h1 align="center">🐾 PocketPaw</h1>

<p align="center">
  <strong>An AI agent that runs on your machine, not someone else's.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/pocketpaw/"><img src="https://img.shields.io/pypi/v/pocketpaw.svg" alt="PyPI version"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
  <a href="https://pypi.org/project/pocketpaw/"><img src="https://img.shields.io/pypi/dm/pocketpaw.svg" alt="Downloads"></a>
  <a href="https://github.com/pocketpaw/pocketpaw/stargazers"><img src="https://img.shields.io/github/stars/pocketpaw/pocketpaw?style=social" alt="GitHub Stars"></a>
</p>

<p align="center">
  <a href="https://github.com/pocketpaw/pocketpaw/releases/latest/download/PocketPaw-Setup.exe"><img src="https://img.shields.io/badge/Windows-Download_.exe-0078D4?style=for-the-badge&logo=windows&logoColor=white" alt="Download for Windows"></a>
</p>

<p align="center">
  Self-hosted AI agent with a web dashboard. Talks to you over <strong>Discord</strong>, <strong>Slack</strong>, <strong>WhatsApp</strong>, <strong>Telegram</strong>, or the browser.<br>
  No subscription. No cloud lock-in. Your data stays on your machine.
</p>

> ⚠️ **Beta:** This project is under active development. Expect breaking changes between versions.

<p align="center">
  <video src="https://github.com/user-attachments/assets/a15bb8c7-6897-40d2-8111-aa905fe3fdfe" width="700" controls></video>
</p>

---

## Quick Start

### Via Desktop Installer

Sets up Python and PocketPaw in one click, then opens the dashboard.

| Platform | Download |
| --- | --- |
| **Windows** | [PocketPaw-Setup.exe](https://github.com/pocketpaw/pocketpaw/releases/latest/download/PocketPaw-Setup.exe) |

### Install via Terminal

<details open>
<summary>macOS / Linux</summary>

```bash
pip install pocketpaw && pocketpaw
```

Or use the install script:

```bash
curl -fsSL https://pocketpaw.xyz/install.sh | sh
```

</details>

<details>
<summary>Windows (PowerShell)</summary>

```powershell
powershell -NoExit -Command "iwr -useb https://pocketpaw.xyz/install.ps1 | iex"
```

Or install manually with pip:

```powershell
pip install pocketpaw
pocketpaw
```

> **Note:** Some features (browser automation, shell tools) work best under WSL2. Native Windows support covers the web dashboard and all LLM chat features.

</details>

<details>
<summary>Other methods</summary>

```bash
pipx install pocketpaw && pocketpaw    # Isolated install
uvx pocketpaw                           # Run without installing

# From source
git clone https://github.com/pocketpaw/pocketpaw.git
cd pocketpaw && uv run pocketpaw
```

</details>

<details>
<summary>Docker</summary>

```bash
git clone https://github.com/pocketpaw/pocketpaw.git && cd pocketpaw
cp .env.example .env
docker compose up -d
```

Dashboard at `http://localhost:8888`. Get the access token:

```bash
docker exec pocketpaw cat /home/pocketpaw/.pocketpaw/access_token
```

Agent-created files appear in `./workspace/` on the host. Optional profiles: `--profile ollama` (local LLMs), `--profile qdrant` (vector memory). Using Ollama on the host? Set `POCKETPAW_OLLAMA_HOST=http://host.docker.internal:11434` in `.env`.

</details>

The web dashboard opens at `http://localhost:8888`. From there you can connect Discord, Slack, WhatsApp, or Telegram.

---

## Features

| | |
| --- | --- |
| 📡 **9+ Channels** | Web Dashboard, Discord, Slack, WhatsApp, Telegram, Signal, Matrix, Teams, Google Chat |
| 🧠 **6 Agent Backends** | Claude Agent SDK, OpenAI Agents, Google ADK, Codex CLI, OpenCode, Copilot SDK |
| 🛠️ **50+ Tools** | Browser, web search, image gen, voice/TTS/STT, OCR, research, delegation, skills |
| 🔌 **Integrations** | Gmail, Calendar, Google Drive & Docs, Spotify, Reddit, MCP servers |
| 💾 **Memory** | Long-term facts, session history, smart compaction, Mem0 semantic search |
| 🔒 **Security** | Guardian AI, injection scanner, tool policy, plan mode, audit log, self-audit daemon |
| 🏠 **Local-First** | Runs on your machine. Ollama for fully offline operation. macOS / Windows / Linux. |

### Examples

```
You:  "Every Sunday evening, remind me which recycling bins to put out"
Paw:  Done. I'll check the recycling calendar and message you every Sunday at 6pm.

You:  "Find that memory leak, the app crashes after 2 hours"
Paw:  Found it. The WebSocket handler never closes connections. Here's the fix.

You:  "I need a competitor analysis report for our product launch"
Paw:  3 agents working on it. I'll ping you when it's ready.
```

---

## Architecture

<p align="center">
  <img src="docs/public/pocketpaw-system-architecture.webp" alt="PocketPaw System Architecture" width="800">
</p>

Everything goes through an event-driven message bus. Channels publish messages, the `AgentLoop` picks them up and routes to whichever backend you've configured. All six backends implement the same `AgentBackend` protocol, so swapping one for another doesn't touch the rest of the system.

### Agent Backends

| Backend | Key | Providers | MCP |
| --- | --- | --- | :---: |
| **Claude Agent SDK** (Default) | `claude_agent_sdk` | Anthropic, Ollama | Yes |
| **OpenAI Agents SDK** | `openai_agents` | OpenAI, Ollama | No |
| **Google ADK** | `google_adk` | Google (Gemini) | Yes |
| **Codex CLI** | `codex_cli` | OpenAI | Yes |
| **OpenCode** | `opencode` | External server | No |
| **Copilot SDK** | `copilot_sdk` | Copilot, OpenAI, Azure, Anthropic | No |

### Security

<p align="center">
  <img src="docs/public/pocketpaw-security-stack.webp" alt="PocketPaw 7-Layer Security Stack" width="500">
</p>

A secondary LLM (Guardian AI) reviews every tool call before it runs. On top of that: injection scanning, configurable tool policies, plan mode for human approval, `--security-audit` CLI, a self-audit daemon, and an append-only audit log. [Details in the docs](https://docs.pocketpaw.xyz/security).

<details>
<summary>Detailed security architecture</summary>
<br>
<p align="center">
  <img src="docs/public/pocketpaw-security-architecture.webp" alt="PocketPaw Security Architecture (Defense-in-Depth)" width="800">
</p>
</details>

---

## Configuration

Settings live in `~/.pocketpaw/config.json`. You can also use `POCKETPAW_`-prefixed env vars or the dashboard Settings panel. API keys are encrypted at rest.

```bash
export POCKETPAW_ANTHROPIC_API_KEY="sk-ant-..."   # Required for Claude SDK backend
export POCKETPAW_AGENT_BACKEND="claude_agent_sdk"  # or openai_agents, google_adk, etc.
```

> **Note:** An Anthropic API key from [console.anthropic.com](https://console.anthropic.com/api-keys) is required for the Claude SDK backend. OAuth tokens from Claude Free/Pro/Max plans are [not permitted](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use) for third-party use. For free local inference, use Ollama instead.

See the [full configuration reference](https://docs.pocketpaw.xyz/getting-started/configuration) for all settings.

---

## Development

```bash
git clone https://github.com/pocketpaw/pocketpaw.git && cd pocketpaw
uv sync --dev               # Install with dev deps
uv run pocketpaw --dev      # Dashboard with auto-reload
uv run pytest               # Run tests (2000+)
uv run ruff check . && uv run ruff format .  # Lint & format
```

<details>
<summary>Optional extras</summary>

```bash
pip install pocketpaw[openai-agents]       # OpenAI Agents backend
pip install pocketpaw[google-adk]          # Google ADK backend
pip install pocketpaw[discord]             # Discord
pip install pocketpaw[slack]               # Slack
pip install pocketpaw[memory]              # Mem0 semantic memory
pip install pocketpaw[all]                 # Everything
```

</details>

---

## Documentation

**[docs.pocketpaw.xyz](https://docs.pocketpaw.xyz)** covers getting started, backends, channels, tools, integrations, security, memory, and the full API reference.

---

## Star History

<a href="https://star-history.com/#pocketpaw/pocketpaw&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=pocketpaw/pocketpaw&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=pocketpaw/pocketpaw&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=pocketpaw/pocketpaw&type=Date" />
 </picture>
</a>

## Contributors

<a href="https://github.com/pocketpaw/pocketpaw/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=pocketpaw/pocketpaw" alt="Contributors" />
</a>

---

## Join the Pack

- Twitter: [@PocketPawAI](https://twitter.com/PocketPaw89242)
- Discord: Coming Soon
- Email: pocketpawai@gmail.com

PRs welcome. Come build with us.

## License

MIT &copy; PocketPaw Team

<p align="center">
  <img src="paw.png" alt="PocketPaw" width="40">
  <br>
  <strong>Built for people who'd rather own their AI than rent it</strong>
</p>
