"""Config file health checks."""

from __future__ import annotations

import json

from pocketpaw.health.checks.result import HealthCheckResult


def check_config_exists() -> HealthCheckResult:
    """Check that ~/.pocketpaw/config.json exists."""
    from pocketpaw.config import get_config_path

    path = get_config_path()
    if path.exists():
        return HealthCheckResult(
            check_id="config_exists",
            name="Config File",
            category="config",
            status="ok",
            message=f"Config file exists at {path}",
            fix_hint="",
        )
    return HealthCheckResult(
        check_id="config_exists",
        name="Config File",
        category="config",
        status="warning",
        message="No config file found — using defaults",
        fix_hint="Open the dashboard Settings to create a config file.",
    )


def check_config_valid_json() -> HealthCheckResult:
    """Check that config.json is valid JSON."""
    from pocketpaw.config import get_config_path

    path = get_config_path()
    if not path.exists():
        return HealthCheckResult(
            check_id="config_valid_json",
            name="Config JSON Valid",
            category="config",
            status="ok",
            message="No config file (defaults used)",
            fix_hint="",
        )
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return HealthCheckResult(
            check_id="config_valid_json",
            name="Config JSON Valid",
            category="config",
            status="ok",
            message="Config file is valid JSON",
            fix_hint="",
        )
    except (json.JSONDecodeError, Exception) as e:
        return HealthCheckResult(
            check_id="config_valid_json",
            name="Config JSON Valid",
            category="config",
            status="critical",
            message=f"Config file has invalid JSON: {e}",
            fix_hint="Fix the JSON syntax in ~/.pocketpaw/config.json or delete it to reset.",
        )


def check_config_permissions() -> HealthCheckResult:
    """Check config file permissions are 600."""
    import sys

    from pocketpaw.config import get_config_path

    if sys.platform == "win32":
        return HealthCheckResult(
            check_id="config_permissions",
            name="Config Permissions",
            category="config",
            status="warning",
            message="Permission check skipped on Windows",
            fix_hint="Ensure your user profile is protected by a password.",
        )

    path = get_config_path()
    if not path.exists():
        return HealthCheckResult(
            check_id="config_permissions",
            name="Config Permissions",
            category="config",
            status="ok",
            message="No config file to check",
            fix_hint="",
        )

    mode = path.stat().st_mode & 0o777
    if mode <= 0o600:
        return HealthCheckResult(
            check_id="config_permissions",
            name="Config Permissions",
            category="config",
            status="ok",
            message=f"Config file permissions: {oct(mode)}",
            fix_hint="",
        )
    return HealthCheckResult(
        check_id="config_permissions",
        name="Config Permissions",
        category="config",
        status="warning",
        message=f"Config file permissions too open: {oct(mode)} (should be 600)",
        fix_hint="Run: chmod 600 ~/.pocketpaw/config.json",
    )
