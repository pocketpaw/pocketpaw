"""
Shared dangerous-command patterns — single source of truth.

Every security rail in the codebase (Guardian, ShellTool, pocketpaw_native,
claude_sdk) imports from here.  **Do not define ad-hoc pattern lists elsewhere.**

Exports:
  DANGEROUS_PATTERNS           – raw regex strings (case-insensitive intent)
  COMPILED_DANGEROUS_PATTERNS  – pre-compiled ``re.Pattern`` objects (IGNORECASE)
  DANGEROUS_SUBSTRINGS         – plain lowercase strings for substring matching
                                 (used by claude_sdk's PreToolUse hook)
  is_substring_blocked()       – canonical helper; always prefer this over
                                 iterating DANGEROUS_SUBSTRINGS directly so that
                                 case-insensitive matching is guaranteed.
"""

import re

# ---------------------------------------------------------------------------
# Regex patterns — union of every pattern previously in:
#   shell.py, pocketpaw_native.py, guardian.py, and claude_sdk.py
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS: list[str] = [
    # -- Destructive file operations --
    r"rm\s+(-[rf]+\s+)*[/~]",  # rm -rf /, rm -r -f ~, etc.
    r"rm\s+[/~]\s+(-[rf]+\s*)+",  # rm / -rf, rm ~ -fr
    r"rm\s+(-[rf]+\s+)*\*",  # rm -rf *
    r"sudo\s+rm\b",  # Any sudo rm
    r">\s*/dev/",  # Write to devices
    r">\s*/etc/",  # Overwrite system config
    r"mkfs\.",  # Format filesystem
    r"dd\s+if=",  # Disk operations
    r"dd\s+.*of=/dev/",  # Disk device writes via dd
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;",  # Fork bomb
    r"chmod\s+(-R\s+)?777\s+/",  # Dangerous permissions on root
    r"find\s+/\s+.*-delete",  # find / -delete
    r"mv\s+/etc/(passwd|shadow|sudoers)",  # Move critical system files
    r"chown\s+(-R\s+)?root\s*:\s*root\s+/",  # Recursive chown on root
    # -- Remote code execution --
    r"curl\s+.*\|\s*(ba)?sh",  # curl | sh / curl | bash
    r"wget\s+.*\|\s*(ba)?sh",  # wget | sh / wget | bash
    r"curl\s+.*-o\s*/",  # curl download to root
    r"wget\s+.*-O\s*/",  # wget download to root
    # -- Obfuscation / indirect execution --
    r"base64\s+(-d|--decode)\s*\|\s*(ba)?sh",  # base64 -d | sh
    r"\|\s*base64\s+(-d|--decode)\s*\|\s*(ba)?sh",  # pipe to base64 decode to shell
    r"xxd\s+-r\s*.*\|\s*(ba)?sh",  # xxd hex decode to shell
    r"\beval\s+[\"'\$]",  # eval "...", eval $VAR
    r"\bexec\s+[\"'\$]",  # exec "...", exec $VAR
    r"\$\{IFS\}",  # IFS injection style spacing bypass
    r"echo\s+.*\|\s*base64\s+(-d|--decode)",  # echo ... | base64 -d
    r"\b(invoke-expression|iex)\b\s*[\(\$]",  # PowerShell IEX execution
    r"new-object\s+net\.webclient",  # PowerShell download primitive
    r"python[23]?\s+-c\s+.*os\.(system|exec)",  # python -c OS command execution
    # -- Privilege escalation --
    r"sudo\s+(-i|-s)\b",  # sudo -i / sudo -s (interactive root shell)
    r"sudo\s+su\b",  # sudo su
    r"usermod\s+.*-aG\s+sudo",  # Add user to sudo group
    r"echo\s+.*>>\s*/etc/sudoers",  # Append to sudoers
    r"visudo",  # Edit sudoers
    # -- Data exfiltration --
    r"curl\s+.*-d\s+@/etc/",  # curl POST with system files
    r"\bnc\b.*<\s*/etc/",  # netcat with system file redirect
    # -- Reverse shells --
    r"\bnc\b.*-e\s+/bin/(ba)?sh",  # nc -e /bin/sh
    r"bash\s+-i\s+>&\s+/dev/tcp/",  # bash -i >& /dev/tcp/
    # Python / Perl reverse shell — bounded to avoid ReDoS (#895). The
    # previous `.*socket.*connect` chain had two unbounded `.*` quantifiers
    # which backtrack pathologically on long inputs. Bounded `.{0,500}`
    # matches the typical one-liner without exponential cost.
    r"python[23]?\s+-c\s+.{0,500}?socket.{0,200}?connect",
    r"perl\s+-e\s+.{0,500}?socket.{0,200}?INET",
    r"ruby\s+-rsocket\s+-e",  # Ruby reverse shell
    # -- Crontab / scheduled task injection --
    r"crontab\s+-[elr]",  # crontab edit/list/remove
    r"echo\s+.*>>\s*/etc/cron",  # Append to cron dirs
    r"\bat\b\s+\d",  # at command for scheduling
    # -- SSH key injection --
    r"ssh-keygen\s+.*-f\s+/",  # ssh-keygen writing to absolute path
    r"echo\s+.*>>\s*~?/\.ssh/authorized_keys",  # Inject SSH key
    # -- System damage --
    r">\s*/etc/passwd",  # Overwrite passwd
    r">\s*/etc/shadow",  # Overwrite shadow
    r"systemctl\s+(stop|disable)\s+(ssh|sshd|firewall)",
    r"iptables\s+-F",  # Flush firewall
    r"ufw\s+(disable|reset)",  # Disable/reset UFW firewall
    r"\bshutdown\b",  # Shutdown system
    r"\breboot\b",  # Reboot system
    r"init\s+0",  # Halt system
    r"fdisk\s+/dev/",  # Disk partitioning
    r"parted\s+/dev/",  # Disk partitioning
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
    "rm / -rf",
    "rm -rf ~",
    "rm ~ -rf",
    "rm -rf *",
    "sudo rm",
    "> /dev/",
    "format ",
    "mkfs",
    "chmod 777 /",
    ":(){ :|:& };:",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "dd of=/dev/",
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
    # Obfuscation / indirect execution
    "base64 -d | sh",
    "base64 -d | bash",
    "base64 --decode | sh",
    "base64 --decode | bash",
    'eval "',
    "eval $",
    "eval '",
    "${ifs}",
    "invoke-expression",
    "iex ",
    "new-object net.webclient",
    "os.system(",
    "os.exec(",
    # Privilege escalation
    "sudo -i",
    "sudo -s",
    "sudo su",
    ">> /etc/sudoers",
    "visudo",
    # Data exfiltration
    "curl -d @/etc/",
    # Reverse shells
    "nc -e /bin/sh",
    "nc -e /bin/bash",
    "bash -i >& /dev/tcp/",
    # Crontab / scheduled task injection
    "crontab -e",
    "crontab -r",
    ">> /etc/cron",
    # SSH key injection
    ">> ~/.ssh/authorized_keys",
    ">> /root/.ssh/authorized_keys",
    # Additional system damage
    "ufw disable",
    "ufw reset",
    "fdisk /dev/",
    "parted /dev/",
    "find / -delete",
]


# ---------------------------------------------------------------------------
# Canonical helper — always use this instead of raw ``sub in command`` so
# that case-insensitivity is enforced at the source and call sites cannot
# accidentally re-introduce the case-sensitive bypass (OWASP A01).
# ---------------------------------------------------------------------------


def is_substring_blocked(command: str) -> str | None:
    """Return the first matching substring if *command* is dangerous, else ``None``.

    Matching is case-insensitive: ``'SUDO RM'`` is equivalent to ``'sudo rm'``.
    Prefer this helper over iterating :data:`DANGEROUS_SUBSTRINGS` directly.
    """
    command_lower = command.lower()
    for sub in DANGEROUS_SUBSTRINGS:
        if sub in command_lower:
            return sub
    return None
