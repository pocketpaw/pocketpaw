# Agent Security Layer: Practical Analysis for PocketPaw

> Evaluating 7 agent-economy security startup ideas against PocketPaw's existing
> architecture. What's practical, what's a stretch, and what to build first.

---

## Context

The agent economy has a trust problem. The most downloaded skill on OpenClaw
turned out to be malware. PocketPaw already runs agents with shell access,
browser control, filesystem operations, and persistent memory on the user's
local machine. That makes security not just relevant — it's existential.

PocketPaw's current security posture:

- **Guardian AI**: Secondary LLM that classifies shell commands as SAFE/DANGEROUS
- **Audit logging**: Append-only JSONL log of all tool executions (`~/.pocketclaw/audit.jsonl`)
- **File jail**: Restricts filesystem ops to a configurable sandbox path
- **Tool policy profiles**: minimal / coding / full — with allow/deny overrides
- **Hardcoded pattern blocking**: Fork bombs, `rm -rf /`, `curl | sh`, etc.
- **Single-user lock**: Only the paired Telegram user can issue commands

This is a solid foundation. Most of the 7 ideas below can plug into what
already exists without a rewrite.

---

## Idea-by-Idea Breakdown

### 1. Verified Publisher Registry (Agent App Store)

**What it is:** Cryptographic signing + reputation scores for skills/plugins.
Think iOS App Store review but for agent skills.

**Practicality for PocketPaw: HIGH**

PocketPaw already has a skills system (`~/.agents/skills/` and
`~/.pocketclaw/skills/`) that loads YAML files with embedded instructions.
Skills are installed via `npx skills add`. There is currently **zero
verification** of what gets installed.

**What to build:**

1. **Skill manifest signing** — Each skill YAML gets a detached signature
   (ed25519 or GPG). The `SkillLoader` (`src/pocketclaw/skills/loader.py`)
   checks the signature before loading. Unsigned skills get a warning; tampered
   skills get blocked.

2. **Publisher identity** — Map skill authors to verified identities (GitHub
   account, domain ownership, or a simple public key registry). Store trust
   anchors in `~/.pocketclaw/trusted_publishers.json`.

3. **Reputation metadata** — Track install count, community flags, and audit
   status per skill. This can start as a simple JSON feed fetched from a
   central registry and cached locally.

**Effort:** Medium. The signing infra is straightforward. The hard part is
building the registry service (out of scope for PocketPaw core, but the
verification client belongs here).

**Where it plugs in:**
- `src/pocketclaw/skills/loader.py` — Add signature verification before `_parse_skill()`
- `src/pocketclaw/config.py` — Add `skill_verification_mode: "strict" | "warn" | "off"`

---

### 2. Agent Identity Broker (Agent Property Manager)

**What it is:** Each agent gets scoped credentials — access only to what it
needs, with automatic expiry and revocation.

**Practicality for PocketPaw: HIGH**

PocketPaw's Mission Control already defines agents with roles and autonomy
levels (intern / specialist / lead). But today all agents share the same tool
set and the same API keys. A "research" agent has the same shell access as a
"coding" agent.

**What to build:**

1. **Per-agent tool scoping** — Extend `MissionControlAgent` model
   (`src/pocketclaw/mission_control/models.py`) with a `tool_policy` field.
   When the agent loop runs for a specific agent, apply that agent's policy
   instead of the global one.

2. **Scoped API key injection** — Instead of passing the global
   `ANTHROPIC_API_KEY` to every agent, generate per-agent credentials (or at
   minimum, per-agent usage tracking). For external services, support
   short-lived tokens.

3. **Session-scoped permissions** — When a skill invokes tools, those tools
   run with the skill's declared `allowed-tools` list only. This already
   partially exists but isn't enforced end-to-end.

4. **Automatic expiry** — Add a `ttl` field to agent sessions. After expiry,
   revoke the agent's message bus subscription and log it.

**Effort:** Medium. Per-agent tool policies are a natural extension of the
existing `ToolPolicy` system. Credential scoping requires more plumbing.

**Where it plugs in:**
- `src/pocketclaw/tools/policy.py` — Accept per-agent overrides
- `src/pocketclaw/mission_control/models.py` — Add `tool_policy` to Agent
- `src/pocketclaw/agents/loop.py` — Apply agent-specific policy before tool execution

---

### 3. Agent Command Center (God View for Enterprise)

**What it is:** A single dashboard showing every agent's activity — what skills
they installed, what data they touched, what commands they ran.

**Practicality for PocketPaw: MEDIUM-HIGH**

PocketPaw already has the raw data:
- Audit log captures every tool execution with metadata
- Mission Control tracks agent status, tasks, and activities
- The web dashboard (`src/pocketclaw/dashboard.py`) has WebSocket real-time push

What's missing is a unified view that aggregates this into a "command center."

**What to build:**

1. **Audit log viewer** — Parse `audit.jsonl` and expose it via a REST
   endpoint. Filter by agent, tool, severity, time range. The dashboard
   already has FastAPI — add `/api/audit` routes.

2. **Real-time activity stream** — The message bus already broadcasts
   `SystemEvent` messages. Pipe these to a dedicated WebSocket channel for
   the command center UI.

3. **Agent inventory panel** — List all registered agents, their current
   status (via heartbeat), tool policies, installed skills, and recent
   actions. Most of this data exists in Mission Control already.

4. **Anomaly highlights** — Flag unusual patterns: agent executing commands
   outside its tool policy, high-frequency tool calls, access to sensitive
   paths. Simple heuristic rules, not ML.

**Effort:** Medium. Backend data exists. This is primarily a frontend/API
aggregation task.

**Where it plugs in:**
- `src/pocketclaw/dashboard.py` — New routes and WebSocket channels
- `src/pocketclaw/security/audit.py` — Add query/filter methods to `AuditLogger`
- `src/pocketclaw/bus/events.py` — Add security-specific event types

---

### 4. AI Security Advisor (Audit-as-a-Service)

**What it is:** Automated security audit of your agent setup. Scan config,
identify weaknesses, recommend fixes.

**Practicality for PocketPaw: HIGH**

This is essentially a skill + built-in health check. PocketPaw can audit itself.

**What to build:**

1. **Security health check command** — A built-in `/security-audit` command
   that scans:
   - Is Guardian AI enabled?
   - Is `bypass_permissions` turned off?
   - Is file jail configured and restrictive enough?
   - Are any skills unsigned or from unknown publishers?
   - Are API keys stored securely (not in plaintext config)?
   - Is the audit log enabled and not tampered with?
   - Are tool policies appropriate (not running "full" without reason)?
   - Is the web dashboard exposed on 0.0.0.0?

2. **Risk score** — Generate a simple 0-100 score based on findings. Show
   it on the dashboard and in Telegram status.

3. **Fix suggestions** — For each finding, provide the exact config change
   or command to remediate.

4. **Periodic re-scan** — Use the existing `APScheduler` integration to run
   the audit on a schedule and alert via Telegram if the score drops.

**Effort:** Low-Medium. Most checks are simple config/state inspections.
This could ship as a built-in skill with a small supporting module.

**Where it plugs in:**
- New module: `src/pocketclaw/security/advisor.py`
- New skill: `~/.pocketclaw/skills/security-audit.yaml`
- `src/pocketclaw/dashboard.py` — Security score widget
- `src/pocketclaw/scheduler.py` — Periodic audit schedule

---

### 5. Agent Cleanse (TSA for Agents / VirusTotal for Skills)

**What it is:** Automated scanning of skills before installation for
obfuscated payloads, suspicious URLs, staged delivery, and data exfiltration
patterns.

**Practicality for PocketPaw: HIGH**

Skills are YAML files containing natural-language instructions that get
injected into agent prompts. The attack surface is **prompt injection** more
than binary exploits. This changes the scanning approach.

**What to build:**

1. **Static skill scanner** — Before loading a skill, scan its instruction
   block for:
   - URLs (especially to paste sites, raw GitHub, IP addresses)
   - Shell command patterns (`curl | sh`, `wget`, `eval`, base64 encoding)
   - Data exfiltration indicators (references to uploading, sending, or
     posting user data to external endpoints)
   - Privilege escalation language ("run as root", "disable security",
     "bypass permissions")
   - Obfuscation (base64 encoded strings, hex-encoded commands, unicode
     tricks)
   - Staged delivery ("download the next step from...")

2. **Quarantine** — Suspicious skills go to `~/.pocketclaw/quarantine/`
   instead of the active skills directory. User gets a Telegram notification
   with the findings.

3. **Pre-install hook** — Intercept `npx skills add` (or whatever the
   install mechanism is) and run the scan before the skill lands in the
   active directory.

4. **Guardian AI review** — For borderline cases, pass the skill's
   instruction text to the Guardian AI for a second opinion on whether it's
   malicious.

**Effort:** Medium. The static scanner is regex + heuristics. The Guardian
AI integration is already built. The quarantine flow is new but simple.

**Where it plugs in:**
- `src/pocketclaw/skills/loader.py` — Pre-load scanning hook
- New module: `src/pocketclaw/security/skill_scanner.py`
- `src/pocketclaw/security/guardian.py` — Extend to analyze skill content (not just commands)

---

### 6. Burner Credentials for Agents

**What it is:** Time-bound, auto-expiring credentials for agent operations.
Like burner phones but for API keys and database access.

**Practicality for PocketPaw: MEDIUM**

PocketPaw doesn't currently manage credentials beyond the global API keys in
config. Adding a credential vault with TTL support is useful but requires
integration with external services.

**What to build:**

1. **Credential vault** — A local encrypted store
   (`~/.pocketclaw/vault.json.enc`) for API keys, tokens, and secrets. Each
   credential has a TTL and scope (which agent/skill can use it).

2. **Auto-rotation** — For services that support it (OAuth2), automatically
   refresh tokens before expiry. For others, notify the user when a
   credential is about to expire.

3. **Per-task credentials** — When a Mission Control task starts, mint a
   short-lived credential set for that task's agent. When the task
   completes, credentials are revoked automatically.

4. **Blast radius containment** — If a skill goes rogue with a burner
   credential, the damage window is limited to the TTL. Combined with audit
   logging, you have a full trail of what was accessed.

**Effort:** High. Credential management, encryption, rotation, and service
integration are complex. This is the most infrastructure-heavy suggestion.

**Where it plugs in:**
- New module: `src/pocketclaw/security/vault.py`
- `src/pocketclaw/config.py` — Vault integration for API key retrieval
- `src/pocketclaw/agents/loop.py` — Inject scoped credentials per agent run
- `src/pocketclaw/mission_control/executor.py` — Mint/revoke per-task credentials

---

### 7. Agent Bouncer (Data Access Blocker)

**What it is:** A runtime filter that blocks unauthorized access to sensitive
user data (passwords, cookies, autofill, saved credentials).

**Practicality for PocketPaw: HIGH**

PocketPaw agents already have shell and filesystem access. The file jail
provides a coarse boundary, but nothing prevents an agent from reading
`~/.ssh/id_rsa` or `~/.aws/credentials` if those are within the jail.

**What to build:**

1. **Sensitive path registry** — A configurable list of paths that are
   always blocked, regardless of file jail settings:
   ```
   ~/.ssh/*
   ~/.aws/*
   ~/.gnupg/*
   ~/.config/gcloud/*
   **/passwords*
   **/.env
   **/credentials*
   **/*secret*
   ~/.local/share/keyrings/*
   browser profile paths (Chrome, Firefox cookie/login databases)
   ```

2. **Runtime interception** — Hook into the filesystem tool
   (`src/pocketclaw/tools/builtin/filesystem.py`) and shell tool to block
   reads of sensitive paths. Log blocked attempts as ALERT-level audit events.

3. **Browser data protection** — When the browser tool is active, prevent
   the agent from navigating to `chrome://settings/passwords` or reading
   browser profile directories containing login databases.

4. **Notification on block** — When an access attempt is blocked, send a
   Telegram alert: "Agent tried to read ~/.ssh/id_rsa — BLOCKED."

5. **User override** — Allow explicit per-session grants: "Yes, the deploy
   agent can read ~/.ssh/deploy_key for this task." Logged and time-limited.

**Effort:** Low-Medium. Path matching is simple. The filesystem and shell
tools are already centralized — adding blockers is straightforward.

**Where it plugs in:**
- `src/pocketclaw/tools/builtin/filesystem.py` — Path validation before read/write
- `src/pocketclaw/tools/builtin/shell.py` — Command argument scanning
- `src/pocketclaw/security/guardian.py` — Extend blocked patterns
- New config: `src/pocketclaw/security/sensitive_paths.py`

---

## Priority Matrix

| # | Idea | Fit | Effort | Impact | Priority |
|---|------|-----|--------|--------|----------|
| 7 | Agent Bouncer | HIGH | Low | Critical — prevents credential theft | **P0** |
| 5 | Agent Cleanse | HIGH | Medium | Critical — blocks malware at install | **P0** |
| 4 | AI Security Advisor | HIGH | Low | High — builds user trust | **P1** |
| 1 | Verified Publisher | HIGH | Medium | High — ecosystem trust | **P1** |
| 2 | Identity Broker | HIGH | Medium | High — least-privilege agents | **P1** |
| 3 | Command Center | MED-HIGH | Medium | Medium — enterprise visibility | **P2** |
| 6 | Burner Credentials | MEDIUM | High | Medium — advanced blast containment | **P2** |

---

## Recommended Build Order

### Phase 1: Lock the Doors (P0)

**Agent Bouncer + Agent Cleanse**

These two address the most immediate threat: a malicious skill reading your
SSH keys or exfiltrating credentials. PocketPaw already has the hook points
(filesystem tool, shell tool, skill loader). This is about adding deny-lists
and static analysis to existing code paths.

Deliverables:
- Sensitive path registry with default deny-list
- Runtime path blocking in filesystem and shell tools
- Static skill scanner with quarantine flow
- Telegram alerts on blocked access attempts

### Phase 2: Build Trust (P1)

**Security Advisor + Verified Publisher + Identity Broker**

Once the doors are locked, give users visibility and control. The security
advisor shows them their risk posture. Publisher verification ensures new
skills are trustworthy. Identity broker scopes agent permissions.

Deliverables:
- `/security-audit` command with risk score
- Skill signature verification in loader
- Per-agent tool policies in Mission Control
- Dashboard security score widget

### Phase 3: Enterprise Ready (P2)

**Command Center + Burner Credentials**

These are the features that make PocketPaw viable for teams and companies.
Real-time visibility across all agents, credential lifecycle management,
compliance-ready audit trails.

Deliverables:
- Unified audit log viewer with filtering
- Real-time agent activity stream
- Credential vault with TTL and auto-revocation
- Anomaly detection heuristics

---

## What Doesn't Fit

Some aspects of these ideas are **platform-level concerns** that don't
belong in PocketPaw core:

- **Registry service infrastructure** (idea 1) — The client-side
  verification belongs in PocketPaw. The registry itself is a separate
  service/product.
- **Web3/blockchain verification** (idea 1) — Cryptographic signing is
  practical. On-chain verification is an entire separate project.
- **Browser extension** (idea 7) — PocketPaw's bouncer works at the
  tool/API level, not as a browser extension. A browser extension is a
  separate product that could integrate with PocketPaw's audit system.
- **H&R Block business model** (idea 4) — The security advisor feature
  is practical. The managed service business model is a business decision,
  not a code feature.

---

## Technical Notes

### Existing Code Touch Points

All seven ideas interact with a small set of existing modules:

```
src/pocketclaw/
├── security/
│   ├── guardian.py          # Extend: skill analysis, path blocking
│   ├── audit.py             # Extend: query API, anomaly detection
│   ├── advisor.py           # NEW: security health checks
│   ├── vault.py             # NEW: credential management
│   ├── skill_scanner.py     # NEW: static skill analysis
│   └── sensitive_paths.py   # NEW: protected path registry
├── skills/
│   └── loader.py            # Extend: signature verification, pre-load scan
├── tools/
│   ├── policy.py            # Extend: per-agent policies
│   └── builtin/
│       ├── filesystem.py    # Extend: sensitive path blocking
│       └── shell.py         # Extend: argument scanning
├── mission_control/
│   └── models.py            # Extend: per-agent tool policy
├── agents/
│   └── loop.py              # Extend: scoped credentials, agent policy
├── dashboard.py             # Extend: security UI, audit viewer
└── config.py                # Extend: security settings
```

### Risk: Over-Engineering

PocketPaw's strength is simplicity ("one command install, 30 seconds to
configure"). Every security feature adds friction. The design should follow
these principles:

1. **Secure by default** — Sensitive paths blocked out of the box, no config needed
2. **Warn, don't block** — For non-critical issues, warn and let the user decide
3. **Progressive disclosure** — Basic users see a risk score; advanced users configure policies
4. **Zero mandatory setup** — Everything works without touching security config; tightening is optional

---

*This document is a starting point for discussion. Each section maps directly
to existing PocketPaw code and architecture. Implementation PRs should
reference this document for context.*
