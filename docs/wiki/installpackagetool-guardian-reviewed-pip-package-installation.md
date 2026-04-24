---
{
  "title": "InstallPackageTool: Guardian-Reviewed pip Package Installation",
  "summary": "The `InstallPackageTool` allows the PocketPaw agent to install Python packages via pip at runtime, subject to two security layers: a strict regex that validates the package spec format before any subprocess is spawned, and a Guardian AI review that checks the package name for typosquatting and malicious intent before installation proceeds. It runs with `trust_level = \"elevated\"` because it can permanently modify the Python environment.",
  "concepts": [
    "InstallPackageTool",
    "pip",
    "Guardian AI",
    "package validation",
    "regex",
    "shell injection prevention",
    "typosquatting",
    "subprocess",
    "sys.executable",
    "trust level",
    "supply chain security"
  ],
  "categories": [
    "builtin tools",
    "security",
    "package management",
    "agent capabilities"
  ],
  "source_docs": [
    "a97a82fef3850779"
  ],
  "backlinks": null,
  "word_count": 509,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pip_install.py` was created 2026-03-12. The motivation is practical: when a user asks the agent to process a file format or perform a computation that requires a library not installed by default, the agent should be able to install it rather than stopping to ask the user to install it manually. However, arbitrary package installation is a high-risk capability — a prompt-injected package name could install a supply chain attack payload.

The tool addresses this with two sequential security gates.

## Gate 1: Package spec regex validation

```python
_VALID_PACKAGE_SPEC_RE = re.compile(r"^[a-zA-Z0-9_\-\.\[\],~>=<!]+$")
```

This regex is the first and fastest gate. It allows only characters that are valid in a single pip package specifier:

- `a-zA-Z0-9_-\.` — package name characters
- `[],` — extras syntax (e.g., `requests[security]`)
- `~>=<!` — version specifier operators

Critically excluded: **whitespace, semicolons, pipes, ampersands, backticks, dollar signs, parentheses, newlines**. These are the characters used in shell injection attacks. Without this regex, a malicious prompt could craft a package spec like `requests; rm -rf ~` that would execute arbitrary shell commands when passed to `subprocess.run`.

The comment in the source is unusually explicit about what is excluded and why — it is documentation of the threat model, not just the implementation.

## Gate 2: Guardian AI review

After the regex passes, the tool calls the Guardian AI to review the package name:

```python
from pocketpaw.security import get_guardian

is_safe, reason = await get_guardian().check_command(package)
if not is_safe:
    return self._error(f"Package blocked by Guardian: {reason}")
```

The Guardian checks for:
- **Typosquatting**: `requets` instead of `requests`, `pandaas` instead of `pandas`
- **Known malicious packages**: packages previously flagged in PyPI security advisories
- **Suspicious naming patterns**: packages with names designed to look like popular libraries

The Guardian provides a `reason` string when it blocks a package, which the agent can relay to the user as an explanation.

## Subprocess execution

The actual installation runs as a subprocess:

```python
subprocess.run(
    [sys.executable, "-m", "pip", "install", package],
    timeout=self.timeout,
    ...
)
```

Using `sys.executable` (the current Python interpreter's path) rather than `"python"` or `"pip"` ensures the package is installed into the same virtual environment that PocketPaw is running in. Using `"python"` or `"pip"` could resolve to a different interpreter on systems with multiple Python versions.

The `timeout` (default 300 seconds) prevents a hung network request from blocking the agent indefinitely.

## Trust level

`trust_level = "elevated"` — higher than `standard` but below `high`. Package installation is a privileged action (modifies the environment) but does not access user data. The elevated trust level means pockets must explicitly grant this capability before the tool is available.

## Known Gaps

- **No uninstall tool**: Packages installed by the agent cannot be removed by the agent. There is no `UninstallPackageTool`.
- **No version pinning enforcement**: The tool accepts version specifiers but does not enforce that the installed version matches exactly. `pip install requests>=2.0` could install a version that breaks existing code.
- **No virtual environment isolation**: Packages are installed system-wide (relative to the running Python). There is no mechanism to install into a session-scoped or pocket-scoped virtual environment.