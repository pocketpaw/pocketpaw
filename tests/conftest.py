"""Pytest configuration."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pocketclaw.security.audit import AuditLogger


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path):
    """Prevent tests from writing to the real ~/.pocketclaw/audit.jsonl.

    Creates a temp audit logger per test and patches the singleton so
    ToolRegistry.execute() and any other callers write to a throwaway file.
    """
    temp_logger = AuditLogger(log_path=tmp_path / "audit.jsonl")
    with (
        patch("pocketclaw.security.audit._audit_logger", temp_logger),
        patch("pocketclaw.security.audit.get_audit_logger", return_value=temp_logger),
        patch("pocketclaw.tools.registry.get_audit_logger", return_value=temp_logger),
    ):
        yield temp_logger
