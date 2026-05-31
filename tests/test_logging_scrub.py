# Security scrub tests for tool params + audit fallback + dangerous-cmd log.
# Added: 2026-04-16 for security sprint cluster C (#890, #893).

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Unit tests for the scrub helpers
# ---------------------------------------------------------------------------


class TestScrubParams:
    def test_known_secret_field_masked(self):
        from pocketpaw.security.scrub import scrub_params

        out = scrub_params({"openai_api_key": "sk-abcdef123", "prompt": "hi"})
        assert out["openai_api_key"] == "***"
        assert out["prompt"] == "hi"

    def test_pattern_matched_fields_masked(self):
        from pocketpaw.security.scrub import scrub_params

        out = scrub_params(
            {
                "some_api_key": "sk-12345",
                "custom_token": "abc",
                "client_secret": "xyz",
                "password": "hunter2",
                "Authorization": "Bearer x",
                "harmless": "keep-me",
            }
        )
        assert out["some_api_key"] == "***"
        assert out["custom_token"] == "***"
        assert out["client_secret"] == "***"
        assert out["password"] == "***"
        assert out["Authorization"] == "***"
        assert out["harmless"] == "keep-me"

    def test_nested_dict_is_scrubbed(self):
        from pocketpaw.security.scrub import scrub_params

        out = scrub_params(
            {
                "config": {"openai_api_key": "sk-x", "temperature": 0.5},
                "name": "go",
            }
        )
        assert out["config"]["openai_api_key"] == "***"
        assert out["config"]["temperature"] == 0.5
        assert out["name"] == "go"

    def test_empty_dict_returns_empty(self):
        from pocketpaw.security.scrub import scrub_params

        assert scrub_params({}) == {}


class TestScrubCommand:
    def test_strips_bearer_tokens(self):
        from pocketpaw.security.scrub import scrub_command

        out = scrub_command("curl -H 'Authorization: Bearer sk-12345abc' https://api.example.com")
        assert "sk-12345abc" not in out
        assert "Bearer" in out  # we keep the word, scrub only the value

    def test_strips_openai_keys_in_free_text(self):
        from pocketpaw.security.scrub import scrub_command

        out = scrub_command("echo sk-proj-abcdef1234567890ABCDEF")
        assert "sk-proj-abcdef1234567890ABCDEF" not in out

    def test_strips_slack_bot_tokens(self):
        from pocketpaw.security.scrub import scrub_command

        out = scrub_command("curl -d 'token=xoxb-12345-abcdef-ghijkl' ...")
        assert "xoxb-12345-abcdef-ghijkl" not in out


# ---------------------------------------------------------------------------
# Registry — log_tool_use must scrub params before writing
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal BaseTool subclass — avoids the import cost of all 40+ built-in tools."""

    def __init__(self):
        from pocketpaw.tools.protocol import BaseTool  # noqa: F401

    @property
    def name(self):
        return "ingest_key"

    @property
    def description(self):
        return "fake"

    @property
    def trust_level(self):
        return "standard"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"openai_api_key": {"type": "string"}}}

    @property
    def definition(self):
        from pocketpaw.tools.protocol import ToolDefinition

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    async def execute(self, **kwargs):
        return "ok"


async def test_registry_scrubs_params_in_audit(tmp_path):
    """When a tool is invoked with a secret-looking param, the audit log must
    not contain the raw value. Verifies the registry wires scrub_params
    through before calling audit.log_tool_use() — this is #890.
    """
    from pocketpaw.security.audit import AuditLogger
    from pocketpaw.tools.registry import ToolRegistry

    log_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(log_path=log_path)

    with patch("pocketpaw.tools.registry.get_audit_logger", return_value=audit):
        reg = ToolRegistry()
        reg.register(_FakeTool())
        await reg.execute("ingest_key", openai_api_key="sk-dont-leak-me")

    raw = log_path.read_text()
    assert "sk-dont-leak-me" not in raw, f"secret leaked into audit log: {raw}"
    # The params key should still be present (so operators know what was called)
    assert '"openai_api_key"' in raw
    assert "***" in raw


# ---------------------------------------------------------------------------
# Audit fallback — when file write fails, fallback to system logger must scrub
# ---------------------------------------------------------------------------


def test_audit_fallback_scrubs_params(caplog):
    """When the JSONL write raises, the fallback logs the event to the system
    logger. That log line must not contain raw secrets — this is #893.
    """
    from pocketpaw.security.audit import AuditEvent, AuditLogger, AuditSeverity

    logger = AuditLogger(log_path=Path("/root/cannot-write-here/audit.jsonl"))
    ev = AuditEvent.create(
        severity=AuditSeverity.INFO,
        actor="agent",
        action="tool_use",
        target="ingest_key",
        status="attempt",
        params={"openai_api_key": "sk-dont-leak-me"},
    )

    with caplog.at_level(logging.CRITICAL, logger="audit"):
        logger.log(ev)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "FAILED TO WRITE AUDIT LOG" in joined
    assert "sk-dont-leak-me" not in joined, f"secret leaked via fallback: {joined}"


# ---------------------------------------------------------------------------
# Round-trip — JSON should still be parseable after scrub
# ---------------------------------------------------------------------------


def test_scrubbed_event_round_trips_through_json():
    from pocketpaw.security.scrub import scrub_event_dict

    ev = {
        "action": "tool_use",
        "params": {"openai_api_key": "sk-abcdef0123", "q": "what is love"},
        "command": "curl -H 'Authorization: Bearer sk-abc123def456xyz'",
    }
    out = scrub_event_dict(ev)
    # Must still serialize cleanly
    roundtrip = json.loads(json.dumps(out))
    assert roundtrip["params"]["openai_api_key"] == "***"
    assert "sk-abc123def456xyz" not in roundtrip["command"]
