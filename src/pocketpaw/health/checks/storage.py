"""Storage health checks -- disk space, audit log, memory directory."""

from __future__ import annotations

from pocketpaw.health.checks.result import HealthCheckResult


def check_disk_space() -> HealthCheckResult:
    """Check that ~/.pocketpaw/ isn't too large."""
    from pocketpaw.config import get_config_dir

    config_dir = get_config_dir()
    try:
        total = sum(f.stat().st_size for f in config_dir.rglob("*") if f.is_file())
        total_mb = total / (1024 * 1024)
        if total_mb > 500:
            return HealthCheckResult(
                check_id="disk_space",
                name="Disk Space",
                category="storage",
                status="warning",
                message=f"Data directory is {total_mb:.0f} MB (>500 MB)",
                fix_hint="Clear old sessions or audit logs in ~/.pocketpaw/",
            )
        return HealthCheckResult(
            check_id="disk_space",
            name="Disk Space",
            category="storage",
            status="ok",
            message=f"Data directory: {total_mb:.1f} MB",
            fix_hint="",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="disk_space",
            name="Disk Space",
            category="storage",
            status="warning",
            message=f"Could not check disk usage: {e}",
            fix_hint="",
        )


def check_audit_log_writable() -> HealthCheckResult:
    """Check that audit.jsonl is writable."""
    from pocketpaw.config import get_config_dir

    audit_path = get_config_dir() / "audit.jsonl"
    if not audit_path.exists():
        try:
            audit_path.touch()
            return HealthCheckResult(
                check_id="audit_log_writable",
                name="Audit Log Writable",
                category="storage",
                status="ok",
                message="Audit log is writable",
                fix_hint="",
            )
        except Exception as e:
            return HealthCheckResult(
                check_id="audit_log_writable",
                name="Audit Log Writable",
                category="storage",
                status="warning",
                message=f"Cannot create audit log: {e}",
                fix_hint="Check permissions on ~/.pocketpaw/",
            )

    try:
        with audit_path.open("a"):
            pass
        return HealthCheckResult(
            check_id="audit_log_writable",
            name="Audit Log Writable",
            category="storage",
            status="ok",
            message="Audit log is writable",
            fix_hint="",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="audit_log_writable",
            name="Audit Log Writable",
            category="storage",
            status="warning",
            message=f"Audit log not writable: {e}",
            fix_hint="Check permissions: chmod 600 ~/.pocketpaw/audit.jsonl",
        )


def check_memory_dir_accessible() -> HealthCheckResult:
    """Check that memory directory exists and is writable."""
    from pocketpaw.config import get_config_dir

    memory_dir = get_config_dir() / "memory"
    if not memory_dir.exists():
        try:
            memory_dir.mkdir(exist_ok=True)
        except Exception as e:
            return HealthCheckResult(
                check_id="memory_dir_accessible",
                name="Memory Directory",
                category="storage",
                status="warning",
                message=f"Cannot create memory directory: {e}",
                fix_hint="Check permissions on ~/.pocketpaw/",
            )

    if memory_dir.is_dir():
        return HealthCheckResult(
            check_id="memory_dir_accessible",
            name="Memory Directory",
            category="storage",
            status="ok",
            message="Memory directory is accessible",
            fix_hint="",
        )
    return HealthCheckResult(
        check_id="memory_dir_accessible",
        name="Memory Directory",
        category="storage",
        status="warning",
        message="Memory path exists but is not a directory",
        fix_hint="Remove the file at ~/.pocketpaw/memory and restart.",
    )
