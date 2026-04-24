---
{
  "title": "Security Rails: Single Source of Truth for Dangerous Command Patterns",
  "summary": "This module defines the canonical set of dangerous command patterns used across every security layer in PocketPaw — Guardian, ShellTool, native agent, and Claude SDK hooks all import from here. Centralizing patterns in one place eliminates the drift and inconsistency that would arise from each subsystem maintaining its own ad-hoc block list.",
  "concepts": [
    "security rails",
    "dangerous command patterns",
    "COMPILED_DANGEROUS_PATTERNS",
    "DANGEROUS_SUBSTRINGS",
    "is_substring_blocked",
    "single source of truth",
    "regex patterns",
    "PreToolUse hook",
    "Guardian Agent",
    "ShellTool",
    "pattern centralization"
  ],
  "categories": [
    "security",
    "agent runtime",
    "architecture"
  ],
  "source_docs": [
    "dcd3c94eb042dc2c"
  ],
  "backlinks": null,
  "word_count": 498,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Problem: Distributed Pattern Drift

Without a single source of truth, each security layer accumulates its own list of blocked commands. Teams add patterns to one layer but forget others. A regex that blocks `rm -rf /` in the Shell tool might be absent from the Claude SDK's `PreToolUse` hook. The result is inconsistent enforcement — some paths through the codebase block a dangerous command, others let it through.

`rails.py` solves this by being the only place where dangerous patterns are defined. The module documentation is explicit: **Do not define ad-hoc pattern lists elsewhere.**

## Three Export Forms

The module exports patterns in three forms because different consumers need different representations:

**`DANGEROUS_PATTERNS`** — Raw regex strings. Used when consumers need to display patterns to users, serialize them to configuration, or build derived patterns on top.

**`COMPILED_DANGEROUS_PATTERNS`** — Pre-compiled `re.Pattern` objects with `IGNORECASE` flag. Used for performance-critical hot paths where pattern compilation overhead must be avoided. The Guardian Agent imports this list directly.

**`DANGEROUS_SUBSTRINGS`** — Plain lowercase strings for substring matching. The Claude SDK's `PreToolUse` hook uses this because it operates in a context where full regex evaluation may be too expensive or where the hook framework expects simple string matching.

## `is_substring_blocked()`: The Canonical Helper

The module exports a helper function rather than exposing `DANGEROUS_SUBSTRINGS` as a bare list. This is a deliberate API design: callers who iterate the list directly and implement their own `in` check will get case-sensitive matching (because the substrings are stored lowercase). `is_substring_blocked()` normalizes the input to lowercase before comparison, guaranteeing case-insensitive matching regardless of how the caller formats their input.

This subtlety is documented explicitly in the module exports section — a signal that it has caused bugs in the past or was anticipated as a likely source of bugs.

## Pattern Coverage

The patterns cover categories including:
- Filesystem destruction (`rm -rf`, `find ... -delete`, `shred`)
- System modification (`chmod 777`, `chown root`, `mkfs`)
- Network exfiltration (`curl ... | bash`, `wget -O- | sh`)
- Privilege escalation (`sudo su`, `passwd root`)
- Process injection and memory manipulation
- Package manager abuse (`pip install` with suspicious flags)

## Pre-Compilation for Performance

Patterns are compiled at module import time using `re.compile()` with `re.IGNORECASE`. Module-level compilation means the cost is paid once per process, not once per evaluated command — important given that every tool invocation passes through pattern matching.

## Known Gaps

- **Static pattern list**: New attack techniques require manual additions to this file. There is no automated mechanism to ingest threat intelligence feeds or update patterns based on observed incidents.
- **No wildcard/glob pattern support**: The patterns are pure regex. Complex shell globbing or environment variable expansion tricks (e.g., `$'\x72\x6d'` expanding to `rm`) may not be caught without additional pre-processing.
- **No pattern versioning**: There is no mechanism to track which version of the rails a given audit log entry was evaluated against, making it hard to determine retroactively if an earlier pattern set would have blocked a command.