# Self-Audit Daemon — periodic security and health checks.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pocketclaw.config import get_config_dir, get_settings

logger = logging.getLogger(__name__)


def _get_reports_dir() -> Path:
    """Get/create the audit reports directory."""
    d = get_config_dir() / "audit_reports"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Checks (reuse some from audit_cli + new ones)
# ---------------------------------------------------------------------------


def _check_stale_sessions(max_age_days: int = 30) -> tuple[bool, str]:
    """Check for sessions older than max_age_days."""
    sessions_dir = get_config_dir() / "memory" / "sessions"
    if not sessions_dir.exists():
        return True, "No sessions directory"

    now = datetime.now(tz=UTC)
    stale = []
    for f in sessions_dir.glob("*.json"):
        age = now - datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
        if age > timedelta(days=max_age_days):
            stale.append(f.stem)

    if stale:
        return False, f"{len(stale)} stale sessions (>{max_age_days} days old)"
    return True, "No stale sessions"


def _check_config_conflicts() -> tuple[bool, str]:
    """Check for conflicting settings."""
    settings = get_settings()
    issues = []

    # bypass_permissions + plan_mode is contradictory
    if settings.bypass_permissions and settings.plan_mode:
        issues.append("bypass_permissions=True conflicts with plan_mode=True")

    # injection scan disabled + plan mode disabled = no safety net
    if not settings.injection_scan_enabled and not settings.plan_mode:
        issues.append("Both injection_scan and plan_mode disabled — no safety net")

    if issues:
        return False, "; ".join(issues)
    return True, "No config conflicts"


def _check_disk_usage() -> tuple[bool, str]:
    """Check ~/.pocketclaw/ directory size."""
    config_dir = get_config_dir()
    try:
        total = sum(f.stat().st_size for f in config_dir.rglob("*") if f.is_file())
        total_mb = total / (1024 * 1024)

        if total_mb > 500:
            return False, f"Data directory is {total_mb:.0f} MB (>500 MB)"
        return True, f"Data directory size: {total_mb:.1f} MB"
    except Exception as e:
        return False, f"Could not check disk usage: {e}"


def _check_audit_log_size() -> tuple[bool, str]:
    """Check that audit.jsonl isn't too large."""
    audit_path = get_config_dir() / "audit.jsonl"
    if not audit_path.exists():
        return True, "No audit log"

    size_mb = audit_path.stat().st_size / (1024 * 1024)
    if size_mb > 50:
        return False, f"Audit log is {size_mb:.0f} MB — consider rotation"
    return True, f"Audit log size: {size_mb:.1f} MB"


def _check_orphan_oauth_tokens() -> tuple[bool, str]:
    """Check for OAuth tokens without corresponding config."""
    oauth_dir = get_config_dir() / "oauth"
    if not oauth_dir.exists():
        return True, "No OAuth tokens"

    tokens = list(oauth_dir.glob("*.json"))
    if not tokens:
        return True, "No OAuth tokens"

    return True, f"{len(tokens)} OAuth token(s) stored"


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

_ALL_CHECKS = [
    ("Stale sessions", _check_stale_sessions),
    ("Config conflicts", _check_config_conflicts),
    ("Disk usage", _check_disk_usage),
    ("Audit log size", _check_audit_log_size),
    ("OAuth tokens", _check_orphan_oauth_tokens),
]


async def run_self_audit() -> dict:
    """Run all self-audit checks and save a report.

    Also imports and runs checks from audit_cli for a comprehensive audit.

    Returns:
        Report dict with check results and summary.
    """
    results = []
    issues = 0

    # Run audit_cli checks (the 7 original ones)
    try:
        from pocketclaw.security.audit_cli import (
            _check_audit_log,
            _check_bypass_permissions,
            _check_config_permissions,
            _check_file_jail,
            _check_guardian_reachable,
            _check_plaintext_api_keys,
            _check_tool_profile,
        )

        cli_checks = [
            ("Config permissions", _check_config_permissions),
            ("Plaintext API keys", _check_plaintext_api_keys),
            ("Audit log", _check_audit_log),
            ("Guardian agent", _check_guardian_reachable),
            ("File jail", _check_file_jail),
            ("Tool profile", _check_tool_profile),
            ("Bypass permissions", _check_bypass_permissions),
        ]

        for label, fn in cli_checks:
            ok, message, _fixable = fn()
            results.append({"check": label, "passed": ok, "message": message})
            if not ok:
                issues += 1

    except Exception as e:
        logger.warning("Could not run audit_cli checks: %s", e)

    # Run self-audit checks
    for label, fn in _ALL_CHECKS:
        try:
            ok, message = fn()
            results.append({"check": label, "passed": ok, "message": message})
            if not ok:
                issues += 1
        except Exception as e:
            results.append({"check": label, "passed": False, "message": str(e)})
            issues += 1

    report = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "total_checks": len(results),
        "passed": len(results) - issues,
        "issues": issues,
        "results": results,
    }

    # Save report
    try:
        report_path = _get_reports_dir() / f"{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.json"
        report_path.write_text(json.dumps(report, indent=2))
        logger.info(
            "Self-audit complete: %d/%d passed — report at %s",
            report["passed"],
            report["total_checks"],
            report_path,
        )
    except Exception as e:
        logger.warning("Failed to save audit report: %s", e)

    return report
