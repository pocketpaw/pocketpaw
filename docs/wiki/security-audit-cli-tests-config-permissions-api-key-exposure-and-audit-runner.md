---
{
  "title": "Security Audit CLI Tests — Config Permissions, API Key Exposure, and Audit Runner",
  "summary": "This test file validates PocketPaw's security audit command-line tool, which checks for world-readable config files, plaintext API keys, missing audit logs, unreachable guardian agents, invalid file jails, risky tool profiles, and dangerous permission bypasses. Tests cover both detection and auto-fix capabilities.",
  "concepts": [
    "security audit",
    "config permissions",
    "API key exposure",
    "audit log",
    "guardian reachability",
    "file jail",
    "tool profile",
    "bypass permissions",
    "run_security_audit",
    "fix mode",
    "Unix permissions",
    "world-readable"
  ],
  "categories": [
    "testing",
    "security",
    "CLI",
    "system hardening",
    "test"
  ],
  "source_docs": [
    "9d335dde9dfed20b"
  ],
  "backlinks": null,
  "word_count": 493,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_security_audit_cli.py` (created 2026-02-06) tests the security audit module at `pocketpaw.security.audit_cli`. This CLI tool implements a checklist of security invariants that operators can run to verify their PocketPaw installation is correctly hardened. Each check returns a `(ok, message, fixable)` tuple; a separate `run_security_audit` function collects all results and optionally applies fixes.

## Config File Permission Checks

`TestConfigPermissions` verifies `_check_config_permissions()`:

- **No config file** — reports OK (absence is not a problem; it means no config is set).
- **Secure permissions (`0o600`)** — OK.
- **World-readable (`0o644`)** — reports `ok=False`, `fixable=True`. This is a real risk: the config file stores API keys, and world-readable permissions on a shared system expose them to other users.
- **`_fix_config_permissions()`** — removes group and other read/write bits and asserts neither `S_IROTH` nor `S_IRGRP` remain.
- **Windows** — permission checks are skipped on Windows (NTFS semantics differ), and the message includes `"Windows"` to explain the skip.

The skip markers (`@pytest.mark.skipif(sys.platform == "win32", ...)`) are critical: `os.chmod` on Windows silently does nothing for group/other bits, so asserting their absence would be a false test.

## Plaintext API Key Detection

`TestPlaintextApiKeys` covers `_check_plaintext_api_keys()`. The checks detect when API keys are stored in plaintext in the config file rather than via environment variables or a secrets manager. Tests cover: no config file (OK), no keys in config (OK), keys present in config (not OK).

## Audit Log Checks

`TestAuditLog` covers `_check_audit_log()` and `_fix_audit_log()`:

- **Audit log missing** — not OK; the audit log is required for incident investigation.
- **Audit log exists** — OK.
- **`_fix_audit_log()`** — creates the log file and asserts it exists afterward.

## Guardian Reachability

`TestGuardianReachable` checks whether the guardian agent (which approves/blocks tool calls) is accessible:

- **No API key set** — not OK; the guardian cannot be contacted without credentials.
- **API key set** — OK (the test does not make a real network call).

## File Jail Validation

`TestFileJail` covers `_check_file_jail()`:

- **Valid jail path** — OK.
- **Nonexistent path** — not OK. A missing jail means the `RunPythonTool` and similar tools have no sandboxed directory to work in, which could cause runtime errors or fall back to insecure defaults.

## Tool Profile and Bypass Checks

- **`TestToolProfile`** — `"full"` profile warns (it allows all tools including dangerous ones); `"coding"` profile is fine.
- **`TestBypassPermissions`** — `bypass_permissions=True` warns; `False` is OK.

## Full Audit Runner

`TestRunSecurityAudit` tests `run_security_audit()`:

- **All pass** — all checks return OK; the runner returns a clean summary.
- **Issues found** — one check returns not-OK; the result lists the issue.
- **Fix mode** — `run_security_audit(fix=True)` calls fixable check fix functions; `mock_fix` verifies the fix was invoked.

```python
async def test_fix_mode():
    # Asserts that fixable=True checks have their fix functions called
```

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests mock `get_config_path` and `get_settings` rather than using real config, so they do not cover interactions between multiple simultaneous config sources (env vars + file + defaults).
