# 1. 🧭 Project Overview

**Purpose**: PocketPaw is a fully local, self-hosted, personal AI agent platform. It allows users to run complex, multi-agent workflows and integrations entirely on their own machine, avoiding cloud lock-in and protecting data privacy.

**Problem Solved**: Most advanced AI assistants are hosted in the cloud, raising data privacy, security, and subscription cost concerns. Users must give third parties access to personal files and integrations. PocketPaw solves this problem by localizing the agent orchestration and memory, interacting with whichever LLM backend the user chooses (including 100% offline via Ollama).

**Target Users**: Software engineers, power users, and privacy-conscious individuals who want a personalized AI that can safely run local shell commands, browse the web, and interface with private messaging tools.

**Core Features**:
- **Multi-channel**: Integrates directly into Web Dashboard, Telegram, Discord, Slack, WhatsApp, Signal.
- **Provider Agnostic (6+ Backends)**: Anthropic SDK, OpenAI, Google Gemini, local Ollama, GitHub Copilot.
- **Deep Work / Mission Control**: Orchestrates autonomous, multi-agent workspaces where a planner breaks down tasks, and specialized worker agents execute them in parallel based on a dependency graph.
- **Robust Security Stack**: 7-layer defense including a Guardian AI that intercepts and reviews critical tool executions.
- **Persistent Memory**: Session history, semantic vector searching (via Mem0), and automated PII redaction.
- **Native Desktop Client**: Tauri 2.0 app with a system tray and Svelte 5 UI.

---

# 2. 🏗️ High-Level Architecture

PocketPaw is inherently a **modular monolith**. It runs entirely locally but utilizes distinctly decoupled components via an Event-Driven Architecture.

### Key Components
1. **Message Bus (`queue.py`)**: A centralized publish-subscribe broker handling all input/output traffic via generic `InboundMessage` and `OutboundMessage` events.
2. **Channel Adapters**: Headless daemons integrating endpoints like Discord/WhatsApp with the Message Bus.
3. **Agent Loop (`loop.py`)**: The orchestration heart of the system. It runs continuously, reading inbound messages, gathering context, doing security analysis, and forwarding parameters to the respective AI backend.
4. **Agent Router & Backends**: The abstraction layer. Depending on the configuration, it maps LLM requests to specific APIs (Claude SDK, OpenAI Agents, Ollama, etc.).
5. **Mission Control / Deep Work**: An advanced asynchronous planning engine capable of spawning automated agent workspaces, tracking tasks, documents, and notifications.
6. **Security Stack**: Analyzes input for injections, and flags outbound data for PII and sensitive commands.

### Data Flow
```text
[User / Slack] --> InboundMessage --> (Message Bus) --> [AgentLoop]
                                                             | (Fetch Session History)
                                                             v 
                                                   [Security Scanner]
                                                             | (Build Context)
                                                             v
                   [Agent Backend (e.g. Claude)] <---- [Agent Router]
                               |
                               v
                     (Executes Tool e.g. Bash) ---> [Guardian AI Check]
                               |
                   [SystemEvent / Token Stream]
                               |
     (Memory Persist) <--- [AgentLoop]
                               |
                  [PII Redact / Sanitization]
                               |
                        OutboundMessage --> (Message Bus) --> [User / Slack]
```

---

# 3. 📁 Repository Structure Breakdown

The codebase is split into the Python backend (`src`) and the UI frontend (`client`).

### **`/src/pocketpaw/` (Core Backend)**
- `__main__.py` & `cli/`: Entry point, parses CLI arguments (server modes, `--doctor`, terminal dashboards).
- `api/`: Exposes FastAPI REST routes (`/api/v1/`) and WebSocket logic for the dashboard.
- `bus/`: Contains the pub/sub `MessageBus` (`queue.py`) and standard dataclasses (`events.py`). It also contains `adapters/` logic for translating third-party services into generic `InboundMessage` objects.
- `agents/`: The orchestration layer (`loop.py`, `router.py`, `registry.py`) and SDK implementations for various LLMs.
- `deep_work/` & `mission_control/`: Complex project scoping. `deep_work/` handles generating Product Requirements Documents (PRDs) and task dependency graphs. `mission_control/` executes these multi-agent environments.
- `memory/`: Storage providers. Implements File-based JSON memory and `mem0ai` based vector databases.
- `security/`: Vital boundaries. Implements `injection_scanner.py`, `pii.py` (redaction), `guardian.py` (LLM-in-the-loop reviewing critical commands), and `audit.py` for logging.
- `integrations/`: Third party OAuth tools (Gmail, G-Suite, Reddit, Spotify).
- `tools/`: Built-in capabilities the agent can call, like browser control, desktop screenshots, and bash executions. 
- `mcp/`: (Model Context Protocol). A standardized setup to allow the agent to talk to local tools consistently.

### **`/client/` (Frontend)**
- Pre-built Tauri desktop app written in TypeScript/SvelteKit. 
- The Svelte frontend uses REST & WebSockets to communicate with the Python backend.

---

# 4. ⚙️ Setup & Execution Guide

### Prerequisites
- **Language**: Python 3.11+, Rust & Bun (if building the desktop client instead of downloading).
- **Package Manager**: It strictly requires `uv` for python dependencies.

### Installation Steps (Backend focus)
```bash
git clone https://github.com/pocketpaw/pocketpaw.git
cd pocketpaw
uv sync --dev
```

### Environment Variables
Located in `.env` or `~/.pocketpaw/config.json`.
- `POCKETPAW_AGENT_BACKEND`: Enum for which LLM driver to use (`claude_agent_sdk`, `openai_agents`, `google_adk`, etc).
- `POCKETPAW_ANTHROPIC_API_KEY`: API credential (or equivalent for other providers).
- `POCKETPAW_OLLAMA_HOST`: Configures local runtime for privacy (default `http://localhost:11434`).
- `POCKETPAW_MEMORY_BACKEND`: Sets `file` or `mem0` for vector DB support.

### Running Locally
To launch the core engine:
```bash
uv run pocketpaw --dev
```
To run tests without E2E:
```bash
uv run pytest --ignore=tests/e2e
```

---

# 5. 🔄 Core Workflows

### 1. Inbound Messaging Workflow
When a user sends a message on Slack/Discord:
1. **Adapter**: Translates the proprietary Slack JSON into an generic `InboundMessage`.
2. **Bus**: Adapter publishes this object to `queue.py`.
3. **Consumption**: The `AgentLoop`'s infinite watcher pulls the queued event message.

### 2. Context Building & Generation
1. **Concurrency Handled**: A per-session async lock ensures multiple rapid messages from the same user are evaluated correctly in order.
2. **Security & PII**: Scans inbound prompt for Prompt Injections.
3. **Recall Context**: Agent pulls recent `compacted_history` and `file_context`. Every 20 turns, the `AgentLoop` dynamically enforces a core `<identity>` block to prevent the agent from veering off-character.
4. **Router Execution**: Request is funneled through `AgentRouter` into the configured LLM API. 
5. **Streaming**: LLM yields raw chunks. The system immediately redacts PII before framing it into an `OutboundMessage` sent to the Message Bus. 

### 3. Deep Work Task Planning
1. **Analysis**: User issues a complex prompt. `GoalParser` splits the goal by complexity and domain.
2. **PRD Generation**: The Planner Agent dynamically charts a Dependency Graph of tasks and sub-tasks required.
3. **Approval Flow**: `Mission Control` surfaces these tasks to the UI. The user clicks "Approve". 
4. **Parallel Execution**: Free agents begin independently picking up tasks in topological order. 

---

# 6. 🧠 Business Logic Deep Dive

### The `AgentLoop` Lock System (`src/pocketpaw/agents/loop.py`)
**WHY it exists:** When a user spams 5 messages instantly, the LLM will generate 5 completely separate branches of the chat if not serialized. The `AgentLoop` holds an `asyncio.Lock` mapped tightly to a dynamically generated `session_key`. 
**Algorithm:** A Garbage Collection background thread specifically wakes up every 5 minutes to dump idle session locks out of memory, preventing catastrophic memory leaks in massive deployed environments. 

### Guardian AI Checks (`src/pocketpaw/security/guardian.py`)
**WHY it exists:** Agents running raw bash code on your local system is incredibly dangerous. 
**Logic:** A completely secondary "Guardian LLM" context is established transparently. If the Primary AI invokes a `trust_level="critical"` tool (like `shell_execute`), the invocation halts. The Guardian analyzes the impact radius and automatically rejects dangerous rm, curl payload hooks, or privileged escalation commands without user intervention.

### Automatic Auto-Learn (Memory)
Agents dynamically summarize successful interactions on tool completions and push them to memory, meaning they will learn your coding style or environment defaults passively.

---

# 7. 🗄️ Data Layer

**Storage Design:** True to its philosophy of zero-cloud, there are no mandatory external PostgreSQL clusters configured.

**Folder Structure**:
All settings and data operate via dot-folder structure in the user's home path `~/.pocketpaw/`.
- `~/.pocketpaw/config.json`: The core settings definitions.
- `~/.pocketpaw/memory/`: Uses `FileMemoryStore` saving threads directly as `jsonl` files per user/session. Very simple, easy for users to migrate or delete.
- `~/.pocketpaw/workspace/`: Stored artifacts from Mission control and executed tasks.

For sophisticated data, it enables locally hosted **mem0ai** modules, utilizing local Qdrant vectors to perform semantic memory fetching (e.g. "I remember you said you prefer python over java"). Data lifecycle is maintained by pruning and automated compaction when memory approaches token limits.

---

# 8. 🔌 APIs & Integrations

### Internal APIs
The `serve.py` uses `FastAPI` logic offering traditional endpoints.
- `/api/mission-control/*`: Manages autonomous agent instances.
- `/api/deep-work/*`: Project submission and tracking.
- `/ws`: Primary multiplexing channel routing to the dashboard. 

### Integrations (`src/pocketpaw/integrations`)
Managed securely via local OAuth callback redirect logic:
- **Google Services**: Gmail (Sending, Read labels), Google Drive / Docs (Download and Create sheets).
- **Reddit & Spotify**: Read community contexts or manipulate local environments.

### Model Context Protocol (MCP)
The application acts as an MCP server. It explicitly normalizes third-party data connections so external MCP clients can interact safely with the workspace environment.

---

# 9. 🧪 Testing & Quality

**State:** Over 2900 tests run via Pytest. 
**Strategy:**
- Test suite primarily isolates unit environments. Heavily tests context starvation, memory serialization, auth middlewares, and mock API returns.
- Adapters (Discord, Slack) are completely stubbed out contextually.
- Integration coverage on complex deep work `DependencyScheduler` logic.

**Missing Test Coverage**:
- UI end-to-end (E2E) testing appears to be deliberately omitted in local dev loops (as shown by `--ignore=tests/e2e`).
- Multi-threaded lock contention race conditions.

---

# 10. ⚠️ Known Issues & Weak Points

1. **FileStore Bottlenecks**: The `FileMemoryStore` relying on `jsonl` lists works extremely well for single users. If instances scale to thousands of connected users over Discord/Slack, I/O filesystem deadlocks are inevitable.
2. **Context Drifting & Startvation**: Handled partially by `_IDENTITY_REINFORCE_THRESHOLD`, but overly compacted memories could wipe out highly relevant specialized skill details.
3. **Event Loop Latency**: Long-running synchronous system routines might block the main `asyncio` event loop dynamically since Python isn't inherently multithreaded at the CPU level.

---

# 11. 🚀 Improvement Opportunities

### Easy Wins (Good for first PRs)
- **Log cleanup & UI Polish**: Exposing more status commands under the `pocketpaw status` CLI interface.
- **Support more Media formats**: Enhancing audio/video handling directly in the adapters.

### Medium Complexity
- **Database Refactor**: Offload the `FileMemoryStore` capability explicitly into `SQLite` as the default local backend for a massive speed multiplier in message recall.
- **Docker Compose Updates**: Pre-baking Qdrant into the docker-compose YAML aggressively.

### Advanced Improvements
- **Microservice Separation**: Separate the Deep Work / Mission Control processing away from standard chatbot responding via Redis queues, instead of running everything on standard `asyncio.create_task()`.
- **RBAC for Dashboards**: Currently single-tenant local system. Introduce tokenized Role Based Access Controls for instances deployed generically into an office environment.

---

# 12. 🧑‍💻 Contribution Guide

**Getting Started**:
1. Check out the current open issues mapped to "Good First Issue".
2. Spin up the backend with `uv sync --dev` and play with the websocket interface.
3. **Coding Standards**: `uv run ruff check .` and `uv run ruff format .` before pushing. 
4. **Architecture Rule**: All LLM processing MUST implement the `AgentEvent` yields from `AgentBackend` protocol in `/src/pocketpaw/agents/protocol.py`. Never call an API request directly on an endpoint.
5. All file execution runs must use `async def`. Blocking filesystem I/O should be wrapped implicitly or explicitly in `asyncio.to_thread()`.

---

# 13. 🔍 Execution Trace Example

**Scenario: User requests "Find the memory leak in loop.py"**

1. The SvelteKit Desktop App emits a websocket packet `{"text": "Find the memory leak in loop.py"}`.
2. `pocketpaw.dashboard_ws.py` reads this payload, packages into `InboundMessage` class, pushes to `queue.py`.
3. The `AgentLoop`'s infinite listener consumes the message. It immediately generates an async session lock for the user's ID. 
4. The backend calls `context_builder` fetching memory, injecting system goals, checking threat levels using `injection_scanner.py`. 
5. Passed to `AgentRouter`, which forwards to `Claude SDK` component. 
6. Claude determines it needs to read the file. Yields a Event -> `tool_use (read_file)`. 
7. System intercepts tool, processes read, checks `rails.py` security. Success. 
8. Agent retrieves file context, discovers that `_session_locks` wasn't dropping idle elements, and streams response tokens back over the message bus to the frontend websocket stream chunk-by-chunk.

---

# 14. 📌 Summary for Quick Understanding

* **Goal**: Build an elite, cloud-free AI orchestration tool.
* **Storage**: Purely local. File paths via `~/.pocketpaw`.
* **Execution Engine**: `AgentLoop` drives the backend.
* **Pluggability**: 6+ Supported APIs abstracted via universal message structure. 
* **Scalable AI Output**: `Deep Work` handles planning graphs; `Mission Control` handles multi-agent execution. 
* **Security Priority**: Multi-layered defense including a Guardian AI.
* **Integrations**: Standard Oauth patterns for connecting to third parties.
* **Multi-Platform**: Cross-channel capability out of the box (Discord/Telegram).
* **Robust Codebase**: 2900+ Pytests and strict type hints. 
* **Ecosystem**: Built utilizing uv, Tauri, and Svelte.
