"""
Shared dangerous-command patterns — single source of truth.

Every security rail in the codebase (Guardian, ShellTool, pocketpaw_native,
claude_sdk) imports from here.  **Do not define ad-hoc pattern lists elsewhere.**

Two exports:
  DANGEROUS_PATTERNS           – raw regex strings (case-insensitive intent)
  COMPILED_DANGEROUS_PATTERNS  – pre-compiled ``re.Pattern`` objects (IGNORECASE)
  DANGEROUS_SUBSTRINGS         – plain lowercase strings for substring matching
                                 (used by claude_sdk's PreToolUse hook)
"""

import re

# ---------------------------------------------------------------------------
# Regex patterns — union of every pattern previously in:
#   shell.py, pocketpaw_native.py, guardian.py, and claude_sdk.py
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS: list[str] = [
    # -- Destructive file operations --
    r"rm\s+(-[rf]+\s+)*[/~]",       # rm -rf /, rm -r -f ~, etc.
    r"rm\s+(-[rf]+\s+)*\*",         # rm -rf *
    r"sudo\s+rm\b",                 # Any sudo rm
    r">\s*/dev/",                    # Write to devices
    r">\s*/etc/",                    # Overwrite system config
    r"mkfs\.",                       # Format filesystem
    r"dd\s+if=",                     # Disk operations
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;",  # Fork bomb
    r"chmod\s+(-R\s+)?777\s+/",     # Dangerous permissions on root
    # -- Remote code execution --
    r"curl\s+.*\|\s*(ba)?sh",       # curl | sh / curl | bash
    r"wget\s+.*\|\s*(ba)?sh",       # wget | sh / wget | bash
    r"curl\s+.*-o\s*/",             # curl download to root
    r"wget\s+.*-O\s*/",             # wget download to root
    # -- System damage --
    r">\s*/etc/passwd",              # Overwrite passwd
    r">\s*/etc/shadow",              # Overwrite shadow
    r"systemctl\s+(stop|disable)\s+(ssh|sshd|firewall)",
    r"iptables\s+-F",               # Flush firewall
    r"\bshutdown\b",                # Shutdown system
    r"\breboot\b",                  # Reboot system
    r"init\s+0",                    # Halt system
]

# Pre-compiled for call sites that iterate with `.search()`.
COMPILED_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS
]

# ---------------------------------------------------------------------------
# Plain-string list for substring matching (claude_sdk PreToolUse hook).
#
# These are literal command fragments checked via ``pattern in command``.
# Kept in sync with the regex list — if you add a regex above, add a
# corresponding substring here when a simple literal equivalent exists.
# ---------------------------------------------------------------------------
DANGEROUS_SUBSTRINGS: list[str] = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf *",
    "sudo rm",
    "> /dev/",
    "format ",
    "mkfs",
    "chmod 777 /",
    ":(){ :|:& };:",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "> /etc/passwd",
    "> /etc/shadow",
    "curl | sh",
    "curl | bash",
    "wget | sh",
    "wget | bash",
    "init 0",
    "shutdown",
    "reboot",
    "iptables -F",
]
