"""Pytest configuration."""

from unittest.mock import patch

import pytest

from pocketpaw.security.audit import AuditLogger


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path):
    """Prevent tests from writing to the real ~/.pocketpaw/audit.jsonl.

    Creates a temp audit logger per test and patches the singleton so
    ToolRegistry.execute() and any other callers write to a throwaway file.
    """
    temp_logger = AuditLogger(log_path=tmp_path / "audit.jsonl")
    with (
        patch("pocketpaw.security.audit._audit_logger", temp_logger),
        patch("pocketpaw.security.audit.get_audit_logger", return_value=temp_logger),
        patch("pocketpaw.tools.registry.get_audit_logger", return_value=temp_logger),
    ):
        yield temp_logger
