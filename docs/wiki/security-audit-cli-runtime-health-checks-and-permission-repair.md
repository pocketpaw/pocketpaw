---
{
  "title": "Security Audit CLI: Runtime Health Checks and Permission Repair",
  "summary": "This module provides an async CLI-callable security auditor that checks file permissions, validates the audit log setup, and optionally scans memory files for PII. It was built as a \"Phase 1 Quick Wins\" hardening measure to catch misconfigurations before they become vulnerabilities.",
  "concepts": [
    "security audit",
    "file permissions",
    "config file hardening",
    "PII scanning",
    "audit log setup",
    "stat module",
    "Unix mode bits",
    "CLI tool",
    "exit codes",
    "memory scanning",
    "operator tooling"
  ],
  "categories": [
    "security",
    "devops",
    "cli"
  ],
  "source_docs": [
    "55e51f5e7fd1cbb9"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why This Exists

Security misconfigurations are notoriously silent — a world-readable config file leaks credentials every time someone runs `ls -la`, but no alarm fires. `audit_cli.py` exists to make these invisible problems visible, and where possible, automatically fixable.

The module was introduced as part of Phase 1 Quick Wins (security hardening sprint) and later extended with PII memory scanning. Its two public entry points — `run_security_audit()` and `scan_memory_for_pii()` — are wired into the CLI so operators can run `pocketpaw security audit` at any time.

## Config File Permission Check

`_check_config_permissions()` inspects the PocketPaw config file using Python's `stat` module to read Unix mode bits. The failure it prevents: if the config file is world-readable (mode `0o644` or wider), any local user on a shared machine can `cat ~/.pocketpaw/config.yaml` and harvest API keys, OAuth tokens, and provider URLs.

The check returns a three-tuple `(ok, message, fixable)` — the `fixable` flag tells the runner whether `_fix_config_permissions()` can correct the issue automatically or whether human intervention is required.

**Windows caveat**: Python's `stat()` on Windows simulates Unix mode bits from the read-only attribute, which means the check produces a warning rather than a hard failure on that platform.

## Audit Log Setup Check

`_fix_audit_log()` ensures the audit log file exists and has restrictive permissions (`0o600` — owner read/write only). The failure scenario it prevents: without a pre-created log file, the `AuditLogger` creates it with default umask permissions, which on many systems is `0o644`. That exposes the entire audit trail (including blocked command attempts and actor identities) to other local users.

## PII Memory Scanner

`scan_memory_for_pii()` walks memory files stored under the PocketPaw data directory and runs each through the `PIIScanner`. This addresses a specific risk: users often paste sensitive data into chat (emails, phone numbers, addresses), which gets persisted to memory. The scanner produces a report of findings without automatically redacting, preserving the operator's ability to review and make context-sensitive decisions.

## run_security_audit: Composite Check Runner

`run_security_audit(fix=False)` runs all checks in sequence, accumulates results, prints a formatted report to stdout, and returns an exit code (`0` for pass, `1` for issues found). The `fix` flag enables automatic repair for fixable issues — without it, the audit is purely read-only.

This exit-code contract matters: it allows the audit to be integrated into CI pipelines, deployment scripts, or Docker healthchecks.

## Known Gaps

- **No scheduled auditing**: The audit runs on demand only. There is no built-in cron or watchdog to catch permission regressions that occur after initial setup.
- **PII scanner is report-only**: `scan_memory_for_pii()` reports findings but does not offer a `--fix` mode to redact in place. Users must manually clean flagged memory entries.
- **Windows permission model**: The Unix mode bit approach does not translate reliably to Windows ACLs. The check degrades to a best-effort warning on that platform.