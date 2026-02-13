# Security

PocketPaw includes multiple layers of security: prompt injection scanning, a CLI security audit, Guardian AI command vetting, append-only audit logging, and an automated self-audit daemon.

## Injection Scanner

Two-tier detection system that scans inbound messages for prompt injection attacks.

### Tier 1: Heuristic Scan (Fast)

~20 compiled regex patterns detect common injection techniques:

| Category | Examples | Threat Level |
|----------|----------|-------------|
| Instruction overrides | "ignore all previous instructions" | HIGH |
| Persona hijacks | "you are now a...", "act as if" | MEDIUM–HIGH |
| Delimiter attacks | `` ```system ``, `<\|im_start\|>`, `[INST]` | HIGH |
| Data exfiltration | "send token to webhook" | HIGH |
| Jailbreak patterns | "DAN mode", "do anything now" | HIGH |
| Tool abuse | "run rm -rf", "write reverse shell" | HIGH |

### Tier 2: LLM Deep Scan (Optional)

When heuristic scan flags content as suspicious, an optional LLM classifier (Haiku by default) performs a second check. The LLM returns one of:

- **SAFE** — Overrides heuristic (false positive)
- **SUSPICIOUS** — Keeps the heuristic threat level
- **MALICIOUS** — Escalates to HIGH

### Threat Levels

```
NONE → LOW → MEDIUM → HIGH
```

When a threat is detected, content is wrapped with sanitization markers:

```
[EXTERNAL CONTENT - may contain manipulation (high risk): ] ... [END EXTERNAL CONTENT]
```

### Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `injection_scan_enabled` | bool | `true` | Enable injection scanning on inbound messages |
| `injection_scan_llm` | bool | `false` | Enable Tier 2 LLM deep scan |
| `injection_scan_llm_model` | string | `"claude-haiku-4-5-20251001"` | Model for LLM deep scan |

### Integration

The scanner is wired into:
- **AgentLoop** — scans all inbound messages before processing
- **ToolRegistry** — scans tool results from external sources

## Security Audit CLI

Run a security check from the command line:

```bash
# Check security posture
pocketpaw --security-audit

# Auto-fix fixable issues
pocketpaw --security-audit --fix
```

### Checks (7 total)

| Check | Fixable | What it does |
|-------|---------|-------------|
| Config permissions | Yes | Ensures `~/.pocketclaw/config.json` is owner-read-only (chmod 600) |
| Plaintext API keys | No | Warns if API keys are stored in config without env vars |
| Audit log exists | Yes | Creates `~/.pocketclaw/audit.jsonl` with secure permissions |
| Guardian AI enabled | No | Checks that Guardian AI safety layer is active |
| File jail configured | No | Verifies file jail path is set (not `/`) |
| Tool profile | No | Warns if tool profile is `full` (no restrictions) |
| Bypass permissions | No | Warns if `bypass_permissions` is enabled |

### Exit Codes

- `0` — All checks passed
- `1` — One or more issues found

## Audit Logging

Append-only security log at `~/.pocketclaw/audit.jsonl`. Records:
- Agent actions (tool calls, file operations)
- Security events (injection detections, Guardian AI blocks)
- Authentication events

Each entry is a JSON line with timestamp, event type, and details.

## Guardian AI

Secondary LLM safety check that reviews commands before execution. Uses `AsyncAnthropic` directly to evaluate whether a command is safe to run. Blocks dangerous operations like:
- Recursive deletions (`rm -rf /`)
- Permission escalation (`sudo`)
- Network exfiltration attempts

## Implementation

| File | Description |
|------|-------------|
| `security/injection_scanner.py` | Two-tier injection scanner with `InjectionScanner` class |
| `security/audit_cli.py` | 7-check security audit with `run_security_audit(fix=False)` |
| `security/audit.py` | Append-only audit logging |
| `security/guardian.py` | Guardian AI command vetting |

## Tests

- `tests/test_injection_scanner.py` — Injection scanner tests
- `tests/test_security_audit_cli.py` — Audit CLI tests
