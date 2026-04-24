---
{
  "title": "Security Rails Tests: Dangerous Command Blocking, Bypass Detection, and Safe Allowlist",
  "summary": "Layer 5 of PocketPaw's security stack (`pocketpaw.security.rails`) blocks dangerous shell commands before they reach the execution layer. These tests validate the blocking of destructive file operations, remote code execution patterns, obfuscation bypasses, privilege escalation, data exfiltration, and system damage commands, while confirming that safe commands are not falsely blocked.",
  "concepts": [
    "security rails",
    "dangerous command blocking",
    "rm -rf",
    "curl pipe bash",
    "base64 bypass",
    "obfuscation detection",
    "privilege escalation",
    "data exfiltration",
    "safe allowlist",
    "substring matching",
    "regex blocking",
    "Claude SDK integration"
  ],
  "categories": [
    "testing",
    "security",
    "command execution",
    "test"
  ],
  "source_docs": [
    "6bfb83395e4317dd"
  ],
  "backlinks": null,
  "word_count": 514,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw agents can execute shell commands. Without a rails layer, a compromised or manipulated agent could run `rm -rf /`, pipe remote scripts into bash, or exfiltrate `/etc/passwd`. The rails system uses both regex patterns and substring matching to block known-dangerous commands before execution.

## Pattern Integrity

`TestPatternListIntegrity` validates the rails ruleset itself:

- All patterns are valid compiled regular expressions (no syntax errors).
- The compiled pattern count matches the raw pattern count (no patterns dropped during compilation).
- Substrings are lowercase-compatible (the substring match is case-folded before comparison).
- No duplicate patterns or substrings exist (duplicates indicate a maintenance error and waste CPU).

This meta-test catches regressions in the ruleset file itself, not just in the command being tested.

## Destructive File Operations

`TestDestructiveFileOps` blocks:
- `rm -rf` variants targeting system paths.
- Writes to `/dev/` and critical config files.
- Filesystem format commands (`mkfs`, `format`).
- `dd if=/dev/zero` and similar disk-wiping operations.
- Fork bombs (`:(){:|:&};:`).
- `chmod 777 /` and recursive permission changes on root.
- `find . -delete` patterns.
- `mv` operations targeting critical system files.

## Remote Code Execution

`TestRemoteCodeExecution` blocks the classic `curl | bash` and `wget | sh` patterns — downloading and immediately executing remote scripts — as well as downloads targeting root-owned directories.

## Obfuscation Bypass Detection

`TestObfuscationBypass` is the most adversarial test class, covering bypass techniques that attackers use to evade naive blocklists:

```python
def test_base64_decode_pipe_to_shell_blocked(cmd):
    # e.g., "echo cm0gLXJmIC8= | base64 -d | bash"
    assert _is_blocked(cmd)
```

- **Base64 decode pipe**: encoding a dangerous command in base64 and piping it to bash.
- **Hex decode pipe**: similar technique using hex encoding.
- **`eval` and `exec`**: executing dynamically constructed strings.
- **`$IFS` injection**: using the internal field separator variable to bypass space-based detection.
- **`echo | base64 -d`**: combined encoding and execution in a single line.

## Privilege Escalation

`TestPrivilegeEscalation` blocks:
- Interactive root shells (`sudo -s`, `sudo su`).
- Modifications to `/etc/sudoers`.
- Adding users to the `sudo` or `wheel` group.

## Data Exfiltration

`TestDataExfiltration` blocks `curl --data @/etc/passwd` style POSTs and netcat exfiltration pipes.

## System Damage

`TestSystemDamage` blocks shutdown/reboot commands, iptables flush, SSH service stop, and disk partitioning commands.

## Safe Command Allowlist

`TestSafeCommands` is equally important — it verifies that everyday commands (`ls`, `git status`, `python -m pytest`, `cat README.md`) are NOT blocked. A rails system that blocks legitimate development commands is unusable.

## Claude SDK Integration

`TestClaudeSDKIntegration` verifies that `_is_dangerous_command` (the function called by the Claude SDK tool-use hook) uses both regex and substring matching, and that it is case-insensitive. This matters because LLMs sometimes capitalize command names.

## Substring Blocking

`TestIsSubstringBlocked` validates the substring matching path independently:
- Uppercase and mixed-case variants of blocked patterns are caught.
- Safe commands are not blocked.
- The function returns the matched substring (not just True/False) for audit logging.

## Known Gaps

- No test for multi-line commands split across `\n` or `;` chaining.
- No test for commands using shell aliases that expand to dangerous operations.
- PowerShell tests cover Windows patterns but PocketPaw's primary target is Linux/macOS.