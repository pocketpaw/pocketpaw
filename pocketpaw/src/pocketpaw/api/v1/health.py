# Health router — health summary, errors, check, security audit, self-audit.
# Created: 2026-02-20
#
# Extracted from dashboard.py health + audit endpoints.

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query

from pocketpaw.api.v1.schemas.common import OkResponse
from pocketpaw.api.v1.schemas.health import (
    HealthSummary,
    SecurityAuditResponse,
    SelfAuditReportSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


@router.get("/version")
async def get_version():
    """Return PocketPaw version and basic server info."""
    import sys

    from pocketpaw import __version__
    from pocketpaw.config import Settings

    settings = Settings.load()
    return {
        "version": __version__,
        "python": sys.version,
        "agent_backend": settings.agent_backend,
    }


@router.get("/health", response_model=HealthSummary)
async def get_health_status():
    """Get current health engine summary."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        return engine.summary
    except Exception as e:
        return HealthSummary(error=str(e))


@router.get("/health/errors")
async def get_health_errors(limit: int = Query(20, ge=1, le=500), search: str = ""):
    """Get recent errors from the persistent error log."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        return engine.get_recent_errors(limit=limit, search=search)
    except Exception:
        return []


@router.delete("/health/errors")
async def clear_health_errors():
    """Clear the persistent error log."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        engine.error_store.clear()
        return {"cleared": True}
    except Exception as e:
        return {"cleared": False, "error": str(e)}


@router.post("/health/check")
async def trigger_health_check():
    """Run all health checks and return results."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        await engine.run_all_checks()
        return engine.summary
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


@router.get("/audit")
async def get_audit_log(limit: int = Query(100, ge=1, le=1000)):
    """Get audit log entries."""
    from pocketpaw.security import get_audit_logger

    audit_logger = get_audit_logger()
    if not audit_logger.log_path.exists():
        return []

    logs: list[dict] = []
    try:
        with open(audit_logger.log_path) as f:
            lines = f.readlines()

        for line in reversed(lines):
            if len(logs) >= limit:
                break
            try:
                logs.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []

    return logs


@router.delete("/audit", response_model=OkResponse)
async def clear_audit_log():
    """Clear the audit log file."""
    from pocketpaw.security import get_audit_logger

    audit_logger = get_audit_logger()
    try:
        if audit_logger.log_path.exists():
            audit_logger.log_path.write_text("")
        return OkResponse()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/security-audit", response_model=SecurityAuditResponse)
async def run_security_audit():
    """Run security audit checks and return results."""
    from pocketpaw.security.audit_cli import (
        _check_audit_log,
        _check_bypass_permissions,
        _check_config_permissions,
        _check_file_jail,
        _check_guardian_reachable,
        _check_plaintext_api_keys,
        _check_tool_profile,
    )

    checks = [
        ("Config file permissions", _check_config_permissions),
        ("Plaintext API keys", _check_plaintext_api_keys),
        ("Audit log", _check_audit_log),
        ("Guardian agent", _check_guardian_reachable),
        ("File jail", _check_file_jail),
        ("Tool profile", _check_tool_profile),
        ("Bypass permissions", _check_bypass_permissions),
    ]

    results = []
    issues = 0
    for label, fn in checks:
        try:
            ok, message, fixable = fn()
            results.append({"check": label, "passed": ok, "message": message, "fixable": fixable})
            if not ok:
                issues += 1
        except Exception as e:
            results.append({"check": label, "passed": False, "message": str(e), "fixable": False})
            issues += 1

    total = len(results)
    return SecurityAuditResponse(total=total, passed=total - issues, issues=issues, results=results)


@router.get("/self-audit/reports")
async def get_self_audit_reports():
    """List recent self-audit reports."""
    from pocketpaw.config import get_config_dir

    reports_dir = get_config_dir() / "audit_reports"
    if not reports_dir.exists():
        return []

    reports = []
    for f in sorted(reports_dir.glob("*.json"), reverse=True)[:20]:
        try:
            data = json.loads(f.read_text())
            reports.append(
                SelfAuditReportSummary(
                    date=f.stem,
                    total=data.get("total_checks", 0),
                    passed=data.get("passed", 0),
                    issues=data.get("issues", 0),
                ).model_dump()
            )
        except Exception:
            pass
    return reports


@router.get("/self-audit/reports/{date}")
async def get_self_audit_report(date: str):
    """Get a specific self-audit report by date."""
    from pocketpaw.config import get_config_dir

    report_path = get_config_dir() / "audit_reports" / f"{date}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return json.loads(report_path.read_text())


@router.post("/self-audit/run")
async def run_self_audit():
    """Trigger a self-audit run and return the report."""
    from pocketpaw.daemon.self_audit import run_self_audit

    report = await run_self_audit()
    return report
