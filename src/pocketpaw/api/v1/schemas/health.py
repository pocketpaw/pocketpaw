# Health schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class HealthSummary(BaseModel):
    """Health engine summary."""

    status: str = "unknown"
    check_count: int = 0
    issues: list[dict] = []
    error: str | None = None


class HealthErrorEntry(BaseModel):
    """A single health error log entry."""

    timestamp: str
    level: str
    message: str
    source: str = ""


class SecurityCheckResult(BaseModel):
    """Result of a single security check."""

    check: str
    passed: bool
    message: str
    fixable: bool


class SecurityAuditResponse(BaseModel):
    """Security audit run response."""

    total: int
    passed: int
    issues: int
    results: list[SecurityCheckResult]


class SelfAuditReportSummary(BaseModel):
    """Summary of a self-audit report."""

    date: str
    total: int = 0
    passed: int = 0
    issues: int = 0
