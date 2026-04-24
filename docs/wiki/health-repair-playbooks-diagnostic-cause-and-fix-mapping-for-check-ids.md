---
{
  "title": "Health Repair Playbooks — Diagnostic Cause-and-Fix Mapping for Check IDs",
  "summary": "`playbooks.py` maps each health check ID to a structured diagnostic record describing the symptom, likely causes, and step-by-step fix instructions, providing the data layer for the dashboard's repair guidance UI and the `config_doctor` tool. It also includes a `build_health_summary()` helper that formats check results and playbook context into a human-readable or prompt-injectable text block.",
  "concepts": [
    "playbooks",
    "repair guidance",
    "health check IDs",
    "diagnostic mapping",
    "auto_fixable",
    "build_health_summary",
    "frozenset",
    "config_doctor",
    "prompt injection",
    "cause and fix"
  ],
  "categories": [
    "health monitoring",
    "diagnostics"
  ],
  "source_docs": [
    "4fea0f9bcf95455c"
  ],
  "backlinks": null,
  "word_count": 535,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a health check returns `warning` or `critical`, knowing the status is not enough — the operator needs to know *why it happened* and *how to fix it*. `playbooks.py` provides that second layer of diagnostics as a pure data mapping from `check_id` strings to structured repair records.

## PLAYBOOKS Dictionary

The `PLAYBOOKS` dict maps `check_id` → `dict` with four fields:

- **symptom** — The user-visible failure mode (e.g., "Agent fails to respond or returns authentication errors")
- **causes** — Ordered list of likely root causes
- **fix_steps** — Numbered instructions the user should follow
- **auto_fixable** — Boolean indicating whether PocketPaw can self-repair this issue

All current playbooks have `auto_fixable: False`. This flag is reserved for future automation where the dashboard could apply fixes with a single button click (e.g., auto-generating a new config file).

```python
"llm_reachable": {
    "symptom": "Agent times out or returns network errors",
    "causes": [
        "Internet connection is down",
        "Anthropic API is experiencing an outage",
        "Firewall or proxy blocking API requests",
        "Ollama is not running (if using ollama backend)",
    ],
    "fix_steps": [
        "Check your internet connection",
        "Visit https://status.anthropic.com for API status",
        "If using Ollama: run 'ollama serve' in a terminal",
        "Check if a firewall/VPN is blocking api.anthropic.com",
    ],
    "auto_fixable": False,
},
```

The playbooks cover: `api_key_primary`, `llm_reachable`, `config_valid_json`, `backend_deps`, `disk_space`, `audit_log_writable`, `memory_dir_accessible`, `version_update`, `gws_binary`, and `secrets_encrypted`.

## Section Filtering with Frozensets

```python
_API_KEY_CHECK_IDS: frozenset[str] = frozenset({"api_key_primary", "api_key_format", "secrets_encrypted"})
```

This frozenset enables O(1) membership tests when the `build_health_summary()` function groups results into sections (e.g., grouping all API key related checks under a single heading). The comment explicitly notes "Frozensets for O(1) membership tests inside the section-filter loop", indicating this is a deliberate performance choice over a plain list.

## build_health_summary()

This helper function takes a list of `HealthCheckResult` objects and formats them into a multi-line text summary. For each non-ok result, it includes the check message, fix hint, and — if a playbook exists — the symptom and causes list. The output is consumed in two contexts:

1. **Dashboard UI** — displayed in the health modal as human-readable repair guidance
2. **Prompt injection** — injected into the agent's system prompt so the LLM itself can explain failures to users

The dual use of the same text format is by design: keeping the human-facing and LLM-facing representations identical means no translation layer is needed between them.

## Separation of Data and Logic

`playbooks.py` is pure data and a single formatting function — no network calls, no file I/O, no async code. This makes it trivially testable and importable from any context without side effects. The health engine and dashboard import it independently.

## Known Gaps

- Playbooks are manually maintained and must be updated whenever a new health check is added. There is no enforcement that every `check_id` in the check modules has a corresponding playbook entry — missing entries cause the summary to silently omit repair guidance for that check.
- The `auto_fixable` field is defined but has no implementation — there is no mechanism in the codebase that reads this flag and acts on it.
- `build_health_summary()` always includes causes when a playbook exists, which could produce very verbose output for prompt injection if many checks are failing simultaneously.
