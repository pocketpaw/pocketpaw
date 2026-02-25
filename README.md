<p align="center">

&nbsp; <img src="paw.png" alt="PocketPaw" width="100">

</p>



<h1 align="center">🐾 PocketPaw</h1>



<p align="center">

&nbsp; <strong>An AI agent that runs on your machine, not someone else's.</strong>

</p>



<p align="center">

&nbsp; <a href="https://pypi.org/project/pocketpaw/"><img src="https://img.shields.io/pypi/v/pocketpaw.svg" alt="PyPI version"></a>

&nbsp; <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>

&nbsp; <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>

&nbsp; <a href="https://pypi.org/project/pocketpaw/"><img src="https://img.shields.io/pypi/dm/pocketpaw.svg" alt="Downloads"></a>

&nbsp; <a href="https://github.com/pocketpaw/pocketpaw/stargazers"><img src="https://img.shields.io/github/stars/pocketpaw/pocketpaw?style=social" alt="GitHub Stars"></a>

</p>



<p align="center">

&nbsp; <a href="https://github.com/pocketpaw/pocketpaw/releases/latest/download/PocketPaw-Setup.exe"><img src="https://img.shields.io/badge/Windows-Download\_.exe-0078D4?style=for-the-badge\&logo=windows\&logoColor=white" alt="Download for Windows"></a>

</p>



<p align="center">

&nbsp; Self-hosted AI agent with a web dashboard. Talks to you over <strong>Discord</strong>, <strong>Slack</strong>, <strong>WhatsApp</strong>, <strong>Telegram</strong>, or the browser.<br>

&nbsp; No subscription. No cloud lock-in. Your data stays on your machine.

</p>



> ⚠️ \*\*Beta:\*\* This project is under active development. Expect breaking changes between versions.



<p align="center">

&nbsp; <video src="https://github.com/user-attachments/assets/a15bb8c7-6897-40d2-8111-aa905fe3fdfe" width="700" controls></video>

</p>



---



\## Quick Start



\### Via Desktop Installer



Sets up Python and PocketPaw in one click, then opens the dashboard.



| Platform | Download |

| --- | --- |

| \*\*Windows\*\* | \[PocketPaw-Setup.exe](https://github.com/pocketpaw/pocketpaw/releases/latest/download/PocketPaw-Setup.exe) |



\### Install via Terminal



<details open>

<summary>macOS / Linux</summary>



\*\*Prerequisites:\*\*

\- Python 3.11 or higher (\[download here](https://www.python.org/downloads/))

\- pip package manager (included with Python)



\*\*Quick install:\*\*



```bash

pip install pocketpaw \&\& pocketpaw

Recommended install (with virtual environment):Bash# 1. Verify Python version (must be 3.11+)

python3 --version



\# 2. Upgrade pip to latest version

python3 -m pip install --upgrade pip



\# 3. Create and activate virtual environment (optional but recommended)

python3 -m venv .venv

source .venv/bin/activate



\# 4. Install PocketPaw

pip install pocketpaw



\# 5. Run PocketPaw

pocketpaw

Or use the automated install script:Bashcurl -fsSL \[https://pocketpaw.xyz/install.sh](https://pocketpaw.xyz/install.sh) | sh

</details><details>

<summary>Windows (PowerShell)</summary>Prerequisites:Python 3.11 or higher (download here)pip package manager (included with Python)Ensure Python is added to PATH during installationAutomated installer:PowerShellpowershell -NoExit -Command "iwr -useb \[https://pocketpaw.xyz/install.ps1](https://pocketpaw.xyz/install.ps1) | iex"

Manual install with pip:PowerShell# 1. Verify Python version (must be 3.11+)

python --version



\# 2. Upgrade pip to latest version

python -m pip install --upgrade pip



\# 3. Create and activate virtual environment (optional but recommended)

python -m venv .venv

.\\.venv\\Scripts\\Activate.ps1



\# 4. Install PocketPaw

pip install pocketpaw



\# 5. Run PocketPaw

pocketpaw

Note: Some features (browser automation, shell tools) work best under WSL2. Native Windows support covers the web dashboard and all LLM chat features.</details>Windows CLI TroubleshootingIf you installed PocketPaw using:PowerShellpip install pocketpaw

and the pocketpaw command is not recognized:Complaintext'pocketpaw' is not recognized as an internal or external command

This usually means your Python Scripts directory is not added to PATH.By default, it is located at:ComplaintextC:\\Users\\<your-username>\\AppData\\Local\\Python\\Python3.XX\\Scripts

You can find your exact Scripts path by running:PowerShellpython -c "import sysconfig; print(sysconfig.get\_path('scripts'))"

How to FixOpen Start → Search "Environment Variables"Click "Edit the system environment variables"Click "Environment Variables"Under User variables → Select Path → Click EditAdd the Scripts directory pathRestart your terminalAlternatively, you can run PocketPaw using:PowerShellpython -m pocketpaw

<details>

<summary>Other method</summary>Bashpipx install pocketpaw \&\& pocketpaw    # Isolated install

uvx pocketpaw                           # Run without installing



\# From source

git clone \[https://github.com/pocketpaw/pocketpaw.git](https://github.com/pocketpaw/pocketpaw.git)

cd pocketpaw \&\& uv run pocketpaw

</details><details>

<summary>Docker</summary>Bashgit clone \[https://github.com/pocketpaw/pocketpaw.git](https://github.com/pocketpaw/pocketpaw.git) \&\& cd pocketpaw

cp .env.example .env

docker compose up -d

Dashboard at http://localhost:8888. Get the access token:Bashdocker exec pocketpaw cat /home/pocketpaw/.pocketpaw/access\_token

Agent-created files appear in ./workspace/ on the host. Optional profiles: --profile ollama (local LLMs), --profile qdrant (vector memory). Using Ollama on the host? Set POCKETPAW\_OLLAMA\_HOST=http://host.docker.internal:11434 in .env.</details>The web dashboard opens at http://localhost:8888. From there you can connect Discord, Slack, WhatsApp, or Telegram.Features📡 9 + ChannelsWeb Dashboard, Discord, Slack, WhatsApp, Telegram, Signal, Matrix, Teams, Google Chat🧠 6 Agent BackendsClaude Agent SDK, OpenAI Agents, Google ADK, Codex CLI, OpenCode, Copilot SDK🛠️ 50+ ToolsBrowser, web search, image gen, voice/TTS/STT, OCR, research, delegation, skills🔌 IntegrationsGmail, Calendar, Google Drive \& Docs, Spotify, Reddit, MCP servers💾 MemoryLong-term facts, session history, smart compaction, Mem0 semantic search🔒 SecurityGuardian AI, injection scanner, tool policy, plan mode, audit log, self-audit daemon🏠 Local-FirstRuns on your machine. Ollama for fully offline operation. macOS / Windows / Linux.ExamplesYou:  "Every Sunday evening, remind me which recycling bins to put out"

Paw:  Done. I'll check the recycling calendar and message you every Sunday at 6pm.



You:  "Find that memory leak, the app crashes after 2 hours"

Paw:  Found it. The WebSocket handler never closes connections. Here's the fix.



You:  "I need a competitor analysis report for our product launch"

Paw:  3 agents working on it. I'll ping you when it's ready.

Architecture<p align="center">

<img src="docs/public/pocketpaw-system-architecture.webp" alt="PocketPaw System Architecture" width="800">

</p>Everything goes through an event-driven message bus. Channels publish messages, the AgentLoop picks them up and routes to whichever backend you've configured. All six backends implement the same AgentBackend protocol, so swapping one for another doesn't touch the rest of the system.Agent BackendsbackendKeyProvidersMCPClaude Agent SDK (Default)claude\_agent\_sdkAnthropic, OllamaYesOpenAI Agents SDKopenai\_agentsOpenAI, OllamaNoGoogle ADKgoogle\_adkGoogle (Gemini)YesCode 151codex\_cliOpenAIYesOpenCodeopencodeExternal serverNoCopilot SDKcopilot\_sdkCopilot, OpenAI, Azure, AnthropicNoSecurity<p align="center">

<img src="docs/public/pocketpaw-security-stack.webp" alt="PocketPaw 7-Layer Security Stack" width="500">

</p>A secondary LLM (Guardian AI) reviews every tool call before it runs. On top of that: injection scanning, configurable tool policies, plan mode for human approval, --security-audit CLI, a self-audit daemon, and an append-only audit log. Details in the docs.<details>

<summary>Detailed security architecture</summary>



<p align="center">

<img src="docs/public/pocketpaw-security-architecture.webp" alt="PocketPaw Security Architecture (Defense-in-Depth)" width="800">

</p>

</details>ConfigurationSettings live in ~/.pocketpaw/config.json. You can also use POCKETPAW\_-prefixed env vars or the dashboard Settings panel. API keys are encrypted at rest.Bashexport POCKETPAW\_ANTHROPIC\_API\_KEY="sk-ant-..."   # Required for Claude SDK backend

export POCKETPAW\_AGENT\_BACKEND="claude\_agent\_sdk"  # or openai\_agents, google\_adk, etc.

Note: An Anthropic API key from console.anthropic.comis required for the Claude SDK backend. OAuth tokens from Claude Free/Pro/Max plans are not permittedfor third-party use. For free local inference, use Ollama instead.See the full configuration referencefor all settings.DevelopmentPrerequisites:Python 3.11 or higher (download here)uvpackage managerInstall uv:Bash# macOS/Linux

curl -LsSf \[https://astral.sh/uv/install.sh](https://astral.sh/uv/install.sh) | sh



\# Windows (PowerShell)

powershell -ExecutionPolicy ByPass -c "irm \[https://astral.sh/uv/install.ps1](https://astral.sh/uv/install.ps1) | iex"



\# Or via pip

pip install uv

Setup and run:Bash# 1. Verify Python version

python3 --version



\# 2. Clone and enter the repository

git clone \[https://github.com/pocketpaw/pocketpaw.git](https://github.com/pocketpaw/pocketpaw.git) \&\& cd pocketpaw



\# 3. Install with dev dependencies

uv sync --dev



\# 4. Run PocketPaw in development mode (auto-reload)

uv run pocketpaw --dev



\# 5. Run tests

uv run pytest               # Run tests (2000+)



\# 6. Lint \& format

uv run ruff check . \&\& uv run ruff format .

<details>

<summary>Optional extras</summary>Bashpip install pocketpaw\[openai-agents]       # OpenAI Agents backend

pip install pocketpaw\[google-adk]          # Google ADK backend

pip install pocketpaw\[discord]             # Discord

pip install pocketpaw\[slack]               # Slack

pip install pocketpaw\[memory]              # Mem0 semantic memory

pip install pocketpaw\[all]                 # Everything

</details>Documentationpocketpaw.xyzcovers getting started, backends, channels, tools, integrations, security, memory, and the full API reference.Star History<a href="https://star-history.com/#pocketpaw/pocketpaw\&Date">

<picture>

<source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=pocketpaw/pocketpaw\&type=Date\&theme=dark" />

<source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=pocketpaw/pocketpaw\&type=Date" />

<img alt="Star History Chart" src="https://api.star-history.com/svg?repos=pocketpaw/pocketpaw\&type=Date" />

</picture>

</a>Contributors<a href="https://github.com/pocketpaw/pocketpaw/graphs/contributors">

<img src="https://contrib.rocks/image?repo=pocketpaw/pocketpaw" alt="Contributors" />

</a>Join the PackTwitter: @prakashd88Discord: Coming SoonEmail: pocketpawai@gmail.comPRs welcome. Come build with us.licenseMIT © PocketPaw Team<p align="center">

<img src="paw.png" alt="PocketPaw" width="40">



<strong>Built for people who'd rather own their AI than rent it</strong>

</p>

