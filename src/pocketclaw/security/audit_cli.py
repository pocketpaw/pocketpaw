# Security Audit CLI — run security checks and print a report.
# Created: 2026-02-06
# Part of Phase 1 Quick Wins

import logging
import os
import stat

from pocketclaw.config import get_config_dir, get_config_path, get_settings

logger = logging.getLogger(__name__)


def _check_config_permissions() -> tuple[bool, str, bool]:
    """Check that config file is not world-readable. Returns (ok, message, fixable)."""
    config_path = get_config_path()
    if not config_path.exists():
        return True, "Config file does not exist yet (OK)", False

    mode = config_path.stat().st_mode
    world_read = mode & stat.S_IROTH
    group_read = mode & stat.S_IRGRP
    if world_read or group_read:
        return (
            False,
            f"Config file {config_path} is readable by group/others (mode {oct(mode)})",
            True,
        )
    return True, f"Config file permissions OK ({oct(mode)})", False


def _fix_config_permissions() -> None:
    """Set config file to owner-only read/write."""
    config_path = get_config_path()
    if config_path.exists():
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)


def _check_plaintext_api_keys() -> tuple[bool, str, bool]:
    """Check if API keys appear in plain config file."""
    config_path = get_config_path()
    if not config_path.exists():
        return True, "No config file to check", False

    content = config_path.read_text()
    key_fields = [
        "anthropic_api_key",
        "openai_api_key",
        "tavily_api_key",
        "brave_search_api_key",
        "google_api_key",
        "discord_bot_token",
        "slack_bot_token",
        "slack_app_token",
        "whatsapp_access_token",
    ]
    found = []
    for field in key_fields:
        # Check if the key exists and has a non-null, non-empty value
        if f'"{field}": "' in content:
            # Crude check: value is not null/empty
            import json

            try:
                data = json.loads(content)
                val = data.get(field)
                if val:
                    found.append(field)
            except Exception:
                pass

    if found:
        return (
            False,
            f"API keys stored in plain config: {', '.join(found)}. "
            "Consider using environment variables instead.",
            False,
        )
    return True, "No API keys in plain config file", False


def _check_audit_log() -> tuple[bool, str, bool]:
    """Check that audit log exists and is writable."""
    audit_path = get_config_dir() / "audit.jsonl"
    if not audit_path.exists():
        return False, f"Audit log missing: {audit_path}", True
    if not os.access(audit_path, os.W_OK):
        return False, f"Audit log not writable: {audit_path}", True
    return True, f"Audit log OK: {audit_path}", False


def _fix_audit_log() -> None:
    """Create audit log if missing, fix permissions."""
    audit_path = get_config_dir() / "audit.jsonl"
    if not audit_path.exists():
        audit_path.touch()
    os.chmod(audit_path, stat.S_IRUSR | stat.S_IWUSR)


def _check_guardian_reachable() -> tuple[bool, str, bool]:
    """Check that Guardian agent has an API key configured."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return (
            False,
            "Guardian agent disabled — no Anthropic API key set",
            False,
        )
    return True, "Guardian agent API key configured", False


def _check_file_jail() -> tuple[bool, str, bool]:
    """Check that file_jail_path is configured and valid."""
    settings = get_settings()
    jail = settings.file_jail_path
    if not jail.exists():
        return False, f"File jail path does not exist: {jail}", False
    if not jail.is_dir():
        return False, f"File jail path is not a directory: {jail}", False
    return True, f"File jail OK: {jail}", False


def _check_tool_profile() -> tuple[bool, str, bool]:
    """Warn if tool profile is 'full'."""
    settings = get_settings()
    if settings.tool_profile == "full":
        return (
            False,
            "Tool profile is 'full' — all tools unrestricted. "
            "Consider 'coding' profile for tighter security.",
            False,
        )
    return True, f"Tool profile: {settings.tool_profile}", False


def _check_bypass_permissions() -> tuple[bool, str, bool]:
    """Warn if bypass_permissions is enabled."""
    settings = get_settings()
    if settings.bypass_permissions:
        return (
            False,
            "bypass_permissions is enabled — agent skips permission prompts",
            False,
        )
    return True, "Permission prompts enabled", False


async def run_security_audit(fix: bool = False) -> int:
    """Run security checks, print report, return exit code (0=pass, 1=issues).

    Args:
        fix: If True, auto-fix fixable issues (file permissions, missing audit log).

    Returns:
        0 if all checks pass, 1 if any issues found.
    """
    checks = [
        ("Config file permissions", _check_config_permissions, _fix_config_permissions),
        ("Plaintext API keys", _check_plaintext_api_keys, None),
        ("Audit log", _check_audit_log, _fix_audit_log),
        ("Guardian agent", _check_guardian_reachable, None),
        ("File jail", _check_file_jail, None),
        ("Tool profile", _check_tool_profile, None),
        ("Bypass permissions", _check_bypass_permissions, None),
    ]

    print("\n" + "=" * 60)
    print("  POCKETPAW SECURITY AUDIT")
    print("=" * 60 + "\n")

    issues = 0
    fixed = 0

    for label, check_fn, fix_fn in checks:
        ok, message, fixable = check_fn()

        if ok:
            print(f"  [PASS] {label}: {message}")
        else:
            issues += 1
            if fix and fixable and fix_fn:
                try:
                    fix_fn()
                    print(f"  [FIXED] {label}: {message}")
                    fixed += 1
                    issues -= 1
                except Exception as e:
                    print(f"  [FAIL] {label}: {message} (fix failed: {e})")
            elif fixable:
                print(f"  [WARN] {label}: {message} (fixable with --fix)")
            else:
                print(f"  [WARN] {label}: {message}")

    print()
    print("-" * 60)
    total = len(checks)
    passed = total - issues
    summary = f"  {passed}/{total} checks passed"
    if fixed:
        summary += f", {fixed} auto-fixed"
    print(summary)
    print("-" * 60 + "\n")

    return 0 if issues == 0 else 1
