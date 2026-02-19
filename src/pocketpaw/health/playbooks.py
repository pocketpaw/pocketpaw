# Repair playbooks — pure data mapping check_id to diagnostic info.
# Created: 2026-02-17
# Used by config_doctor tool and health modal UI.

from __future__ import annotations

PLAYBOOKS: dict[str, dict] = {
    "api_key_primary": {
        "symptom": "Agent fails to respond or returns authentication errors",
        "causes": [
            "API key not set in Settings or environment",
            "API key was revoked or expired",
            "Wrong backend selected (e.g. claude_agent_sdk but Anthropic key missing)",
        ],
        "fix_steps": [
            "Open Settings > API Keys in the dashboard",
            "Paste a valid API key for your selected backend",
            "Or set ANTHROPIC_API_KEY (or POCKETPAW_ANTHROPIC_API_KEY) in your shell environment",
            "Restart PocketPaw after changing environment variables",
        ],
        "auto_fixable": False,
    },
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
    "config_valid_json": {
        "symptom": "PocketPaw ignores your settings and uses defaults",
        "causes": [
            "Manual edit introduced syntax error in config.json",
            "File was corrupted (disk issue, interrupted write)",
        ],
        "fix_steps": [
            "Open ~/.pocketpaw/config.json in a text editor",
            "Use a JSON validator (e.g. jsonlint.com) to find syntax errors",
            "Or delete the file and reconfigure from the dashboard",
        ],
        "auto_fixable": False,
    },
    "backend_deps": {
        "symptom": "Agent fails to start or returns import errors",
        "causes": [
            "Required package not installed for selected backend",
            "Virtual environment changed or recreated without dependencies",
        ],
        "fix_steps": [
            "For Claude Agent SDK: pip install claude-code-sdk",
            "For PocketPaw Native: pip install anthropic",
            "For Open Interpreter: pip install open-interpreter",
            "Or reinstall PocketPaw: pip install pocketpaw",
        ],
        "auto_fixable": False,
    },
    "disk_space": {
        "symptom": "Performance degradation, memory search slow",
        "causes": [
            "Large number of chat sessions accumulated",
            "Audit log grew very large",
            "Deep Work project files taking up space",
        ],
        "fix_steps": [
            "Delete old sessions from the Sessions panel",
            "Clear audit logs: truncate ~/.pocketpaw/audit.jsonl",
            "Archive or delete completed Deep Work projects",
        ],
        "auto_fixable": False,
    },
    "config_permissions": {
        "symptom": "Config file readable by other users on the system",
        "causes": [
            "File created with wrong umask",
            "Permissions changed manually",
        ],
        "fix_steps": [
            "Run: chmod 600 ~/.pocketpaw/config.json",
            "Run: chmod 600 ~/.pocketpaw/secrets.enc",
        ],
        "auto_fixable": True,
    },
    "secrets_encrypted": {
        "symptom": "API keys may be stored in plaintext",
        "causes": [
            "Never saved settings from dashboard (first run)",
            "Encrypted file was deleted",
        ],
        "fix_steps": [
            "Open Settings > API Keys in the dashboard and save",
            "This automatically encrypts all secret fields",
        ],
        "auto_fixable": False,
    },
}


def diagnose_config(section: str = "") -> str:
    """Run config-related checks and return a formatted diagnostic report.

    Args:
        section: Optional focus area ('api_keys', 'backend', 'storage', or '' for all).

    Returns:
        Human-readable diagnostic report with playbook hints.
    """
    from pocketpaw.health.checks import STARTUP_CHECKS, HealthCheckResult

    results: list[HealthCheckResult] = []
    for check_fn in STARTUP_CHECKS:
        try:
            result = check_fn()
            # Filter by section if specified
            if section:
                if section == "api_keys" and result.check_id not in (
                    "api_key_primary",
                    "api_key_format",
                    "secrets_encrypted",
                ):
                    continue
                elif section == "backend" and result.check_id not in (
                    "backend_deps",
                    "api_key_primary",
                ):
                    continue
                elif section == "storage" and result.category != "storage":
                    continue
            results.append(result)
        except Exception as e:
            results.append(
                HealthCheckResult(
                    check_id="error",
                    name="Check Error",
                    category="config",
                    status="warning",
                    message=f"Check failed: {e}",
                    fix_hint="",
                )
            )

    lines = ["# Config Diagnosis Report\n"]
    issues_found = 0

    for r in results:
        icon = {"ok": "[OK]", "warning": "[WARN]", "critical": "[FAIL]"}.get(r.status, "[?]")
        lines.append(f"{icon} {r.name}: {r.message}")

        if r.status != "ok":
            issues_found += 1
            if r.fix_hint:
                lines.append(f"    Fix: {r.fix_hint}")

            # Add playbook info if available
            playbook = PLAYBOOKS.get(r.check_id)
            if playbook:
                lines.append(f"    Symptom: {playbook['symptom']}")
                lines.append("    Possible causes:")
                for cause in playbook["causes"]:
                    lines.append(f"      - {cause}")

    lines.append(f"\nTotal: {len(results)} checks, {issues_found} issue(s)")
    return "\n".join(lines)
