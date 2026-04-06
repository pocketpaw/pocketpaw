"""Integration and update health checks."""

from __future__ import annotations

from pocketpaw.health.checks.result import HealthCheckResult


def check_version_update() -> HealthCheckResult:
    """Check if a newer version of PocketPaw is available on PyPI."""
    try:
        from importlib.metadata import version as get_version

        from pocketpaw.config import get_config_dir
        from pocketpaw.update_check import check_for_updates

        current = get_version("pocketpaw")
        config_dir = get_config_dir()
        info = check_for_updates(current, config_dir)

        if info is None:
            return HealthCheckResult(
                check_id="version_update",
                name="Version Update",
                category="updates",
                status="ok",
                message=f"Running v{current} (update check unavailable)",
                fix_hint="",
            )

        if info.get("update_available"):
            latest = info["latest"]
            return HealthCheckResult(
                check_id="version_update",
                name="Version Update",
                category="updates",
                status="warning",
                message=f"Update available: v{current} \u2192 v{latest}",
                fix_hint=(
                    f"Run: pip install --upgrade pocketpaw  |  "
                    f"Changelog: github.com/pocketpaw/pocketpaw/releases/tag/v{latest}"
                ),
            )

        return HealthCheckResult(
            check_id="version_update",
            name="Version Update",
            category="updates",
            status="ok",
            message=f"Running v{current} (latest)",
            fix_hint="",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="version_update",
            name="Version Update",
            category="updates",
            status="ok",
            message=f"Could not check version: {e}",
            fix_hint="",
        )


def check_gws_binary() -> HealthCheckResult:
    """Check whether the Google Workspace CLI (gws) is installed."""
    import shutil

    if shutil.which("gws"):
        return HealthCheckResult(
            check_id="gws_binary",
            name="Google Workspace CLI",
            category="integrations",
            status="ok",
            message="gws binary found in PATH",
            fix_hint="",
        )
    return HealthCheckResult(
        check_id="gws_binary",
        name="Google Workspace CLI",
        category="integrations",
        status="warning",
        message="gws not found — Google Workspace MCP preset won't work without it",
        fix_hint="Install: npm i -g @googleworkspace/cli",
    )
