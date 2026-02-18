# Tests for the Health Engine (health/store.py, checks.py, engine.py, playbooks.py, tools)
# Created: 2026-02-17
# Updated: 2026-02-17 — Phase 2: added ContextHub health_status integration tests
# Covers: ErrorStore, HealthCheckResult, individual checks, HealthEngine,
#         playbook diagnostics, health agent tools, ContextHub health_status source.

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.health.checks import (
    CONNECTIVITY_CHECKS,
    STARTUP_CHECKS,
    HealthCheckResult,
    check_api_key_format,
    check_api_key_primary,
    check_audit_log_writable,
    check_backend_deps,
    check_config_exists,
    check_config_permissions,
    check_config_valid_json,
    check_disk_space,
    check_llm_reachable,
    check_memory_dir_accessible,
    check_secrets_encrypted,
)
from pocketpaw.health.engine import HealthEngine
from pocketpaw.health.playbooks import PLAYBOOKS, diagnose_config
from pocketpaw.health.store import ErrorStore

# Patch targets — check functions use local `from pocketpaw.config import ...`
# so we patch at the source module, not pocketpaw.health.checks.
_P_CONFIG_PATH = "pocketpaw.config.get_config_path"
_P_CONFIG_DIR = "pocketpaw.config.get_config_dir"
_P_SETTINGS = "pocketpaw.config.get_settings"


# =============================================================================
# HealthCheckResult dataclass
# =============================================================================


class TestHealthCheckResult:
    def test_basic_fields(self):
        r = HealthCheckResult(
            check_id="test_check",
            name="Test Check",
            category="config",
            status="ok",
            message="All good",
            fix_hint="",
        )
        assert r.check_id == "test_check"
        assert r.status == "ok"
        assert r.timestamp  # auto-set

    def test_to_dict(self):
        r = HealthCheckResult(
            check_id="test",
            name="Test",
            category="config",
            status="warning",
            message="Problem found",
            fix_hint="Fix it",
            timestamp="2026-01-01T00:00:00",
        )
        d = r.to_dict()
        assert d["check_id"] == "test"
        assert d["status"] == "warning"
        assert d["fix_hint"] == "Fix it"
        assert d["timestamp"] == "2026-01-01T00:00:00"

    def test_timestamp_auto_set(self):
        r = HealthCheckResult(
            check_id="t", name="T", category="c", status="ok", message="m", fix_hint=""
        )
        assert r.timestamp != ""
        assert "T" in r.timestamp  # ISO format contains T

    def test_custom_timestamp_preserved(self):
        r = HealthCheckResult(
            check_id="t",
            name="T",
            category="c",
            status="ok",
            message="m",
            fix_hint="",
            timestamp="custom",
        )
        assert r.timestamp == "custom"


# =============================================================================
# ErrorStore
# =============================================================================


class TestErrorStore:
    @pytest.fixture
    def store(self, tmp_path):
        return ErrorStore(path=tmp_path / "errors.jsonl")

    def test_record_returns_id(self, store):
        error_id = store.record(message="test error", source="test")
        assert isinstance(error_id, str)
        assert len(error_id) == 12

    def test_record_creates_file(self, store):
        store.record(message="test error")
        assert store.path.exists()

    def test_record_appends_valid_jsonl(self, store):
        store.record(message="error one")
        store.record(message="error two")
        lines = store.path.read_text().strip().splitlines()
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["message"] == "error one"
        assert "id" in entry
        assert "timestamp" in entry

    def test_get_recent_empty(self, store):
        assert store.get_recent() == []

    def test_get_recent_returns_newest_first(self, store):
        store.record(message="first")
        store.record(message="second")
        store.record(message="third")
        recent = store.get_recent(limit=2)
        assert len(recent) == 2
        assert recent[0]["message"] == "third"
        assert recent[1]["message"] == "second"

    def test_get_recent_search_filter(self, store):
        store.record(message="API key invalid", source="auth")
        store.record(message="disk full", source="storage")
        store.record(message="API timeout", source="auth")

        results = store.get_recent(search="api")
        assert len(results) == 2
        # newest first
        assert "timeout" in results[0]["message"]
        assert "invalid" in results[1]["message"]

    def test_get_recent_search_case_insensitive(self, store):
        store.record(message="AuthenticationError occurred")
        results = store.get_recent(search="authenticationerror")
        assert len(results) == 1

    def test_record_fields(self, store):
        store.record(
            message="test",
            source="test.module",
            severity="warning",
            traceback="Traceback...",
            context={"project_id": "abc"},
        )
        entry = json.loads(store.path.read_text().strip())
        assert entry["source"] == "test.module"
        assert entry["severity"] == "warning"
        assert entry["traceback"] == "Traceback..."
        assert entry["context"]["project_id"] == "abc"

    def test_clear(self, store):
        store.record(message="test")
        assert store.path.exists()
        store.clear()
        assert not store.path.exists()

    def test_clear_nonexistent_is_safe(self, store):
        store.clear()  # should not raise

    def test_rotate_when_small(self, store):
        store.record(message="small")
        rotated = store.rotate_if_needed(max_size_mb=10)
        assert rotated is False

    def test_rotate_when_large(self, tmp_path):
        store = ErrorStore(path=tmp_path / "errors.jsonl")
        # Write enough to exceed 0.001 MB (about 1KB)
        for i in range(100):
            store.record(message=f"Error number {i}" * 10)

        rotated = store.rotate_if_needed(max_size_mb=0.001)
        assert rotated is True
        # Original file moved to .1
        assert (tmp_path / "errors.jsonl.1").exists()
        # Original file no longer exists (moved)
        assert not store.path.exists()

    def test_rotate_shifts_existing(self, tmp_path):
        store = ErrorStore(path=tmp_path / "errors.jsonl")
        # Create pre-existing rotated file
        (tmp_path / "errors.jsonl.1").write_text("old data\n")

        # Write enough to trigger rotation
        for i in range(100):
            store.record(message=f"Error number {i}" * 10)

        store.rotate_if_needed(max_size_mb=0.001)
        # Old .1 should be shifted to .2
        assert (tmp_path / "errors.jsonl.2").exists()
        assert (tmp_path / "errors.jsonl.1").exists()

    def test_rotate_nonexistent(self, store):
        assert store.rotate_if_needed() is False

    def test_record_handles_write_error(self, tmp_path):
        # Point at a non-writable path
        store = ErrorStore(path=tmp_path / "no_such_dir" / "deep" / "errors.jsonl")
        # Should not raise, just logs warning
        error_id = store.record(message="test")
        assert isinstance(error_id, str)

    def test_get_recent_handles_corrupt_line(self, store):
        # Write one valid and one invalid line
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with store.path.open("w") as f:
            f.write(json.dumps({"message": "good"}) + "\n")
            f.write("this is not json\n")
            f.write(json.dumps({"message": "also good"}) + "\n")
        results = store.get_recent()
        assert len(results) == 2


# =============================================================================
# Individual health checks (config)
# =============================================================================


class TestCheckConfigExists:
    def test_config_exists(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_exists()
            assert r.status == "ok"
            assert r.check_id == "config_exists"

    def test_config_missing(self, tmp_path):
        config_path = tmp_path / "config.json"
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_exists()
            assert r.status == "warning"


class TestCheckConfigValidJson:
    def test_valid_json(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text('{"key": "value"}')
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_valid_json()
            assert r.status == "ok"

    def test_invalid_json(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{broken json")
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_valid_json()
            assert r.status == "critical"
            assert "invalid JSON" in r.message

    def test_no_config_file(self, tmp_path):
        config_path = tmp_path / "config.json"
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_valid_json()
            assert r.status == "ok"


class TestCheckConfigPermissions:
    def test_permissions_ok(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        config_path.chmod(0o600)
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_permissions()
            assert r.status == "ok"

    def test_permissions_too_open(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        config_path.chmod(0o644)
        with patch(_P_CONFIG_PATH, return_value=config_path):
            r = check_config_permissions()
            assert r.status == "warning"
            assert "too open" in r.message


class TestCheckApiKeyPrimary:
    def _mock_settings(self, **kwargs):
        settings = MagicMock()
        settings.agent_backend = kwargs.get("agent_backend", "claude_agent_sdk")
        settings.anthropic_api_key = kwargs.get("anthropic_api_key", "")
        settings.openai_api_key = kwargs.get("openai_api_key", "")
        settings.google_api_key = kwargs.get("google_api_key", "")
        settings.llm_provider = kwargs.get("llm_provider", "auto")
        return settings

    def test_claude_sdk_with_key(self):
        settings = self._mock_settings(anthropic_api_key="sk-ant-test123")
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_primary()
            assert r.status == "ok"
            assert r.check_id == "api_key_primary"

    def test_claude_sdk_no_key(self):
        settings = self._mock_settings(anthropic_api_key="")
        with (
            patch(_P_SETTINGS, return_value=settings),
            patch.dict("os.environ", {}, clear=True),
        ):
            r = check_api_key_primary()
            assert r.status == "critical"

    def test_claude_sdk_env_var(self):
        settings = self._mock_settings(anthropic_api_key="")
        with (
            patch(_P_SETTINGS, return_value=settings),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-env"}),
        ):
            r = check_api_key_primary()
            assert r.status == "ok"

    def test_native_ollama_no_key_needed(self):
        settings = self._mock_settings(agent_backend="pocketpaw_native", llm_provider="ollama")
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_primary()
            assert r.status == "ok"
            assert "Ollama" in r.message

    def test_native_anthropic_with_key(self):
        settings = self._mock_settings(
            agent_backend="pocketpaw_native",
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-key",
        )
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_primary()
            assert r.status == "ok"

    def test_native_anthropic_no_key(self):
        settings = self._mock_settings(
            agent_backend="pocketpaw_native",
            llm_provider="anthropic",
            anthropic_api_key="",
        )
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_primary()
            assert r.status == "critical"

    def test_open_interpreter(self):
        settings = self._mock_settings(agent_backend="open_interpreter")
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_primary()
            assert r.status == "ok"
            assert "Open Interpreter" in r.message

    def test_unknown_backend(self):
        settings = self._mock_settings(agent_backend="unknown_thing")
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_primary()
            assert r.status == "warning"
            assert "Unknown backend" in r.message


class TestCheckApiKeyFormat:
    def test_valid_anthropic_key(self):
        settings = MagicMock()
        settings.anthropic_api_key = "sk-ant-valid123"
        settings.openai_api_key = ""
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_format()
            assert r.status == "ok"

    def test_invalid_anthropic_key_format(self):
        settings = MagicMock()
        settings.anthropic_api_key = "bad-key-format"
        settings.openai_api_key = ""
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_format()
            assert r.status == "warning"
            assert "anthropic_api_key" in r.message

    def test_no_keys_is_ok(self):
        settings = MagicMock()
        settings.anthropic_api_key = ""
        settings.openai_api_key = ""
        with patch(_P_SETTINGS, return_value=settings):
            r = check_api_key_format()
            assert r.status == "ok"


class TestCheckBackendDeps:
    def test_claude_sdk_present(self):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        with (
            patch(_P_SETTINGS, return_value=settings),
            patch("importlib.util.find_spec", return_value=MagicMock()),
        ):
            r = check_backend_deps()
            assert r.status == "ok"

    def test_claude_sdk_missing(self):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        with (
            patch(_P_SETTINGS, return_value=settings),
            patch("importlib.util.find_spec", return_value=None),
        ):
            r = check_backend_deps()
            assert r.status == "critical"
            assert "claude-code-sdk" in r.message


class TestCheckSecretsEncrypted:
    def test_real_fernet_encrypted_file(self, tmp_path):
        """A real Fernet-encrypted secrets.enc should be recognized as valid.
        This was the original bug: the check tried json.loads() on encrypted
        bytes, which always fails because Fernet output is base64, not JSON.
        """

        from cryptography.fernet import Fernet

        # Create a real Fernet-encrypted file (same as CredentialStore._save)
        key = Fernet.generate_key()
        fernet = Fernet(key)
        plaintext = json.dumps({"anthropic_api_key": "sk-ant-test123"}).encode()
        encrypted = fernet.encrypt(plaintext)

        secrets = tmp_path / "secrets.enc"
        secrets.write_bytes(encrypted)

        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_secrets_encrypted()
            assert r.status == "ok", f"Real Fernet file should be ok, got: {r.status} — {r.message}"
            assert "valid" in r.message.lower() or "bytes" in r.message.lower()

    def test_no_secrets_file(self, tmp_path):
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_secrets_encrypted()
            assert r.status == "warning"
            assert "No encrypted" in r.message

    def test_corrupt_secrets(self, tmp_path):
        secrets = tmp_path / "secrets.enc"
        secrets.write_bytes(b"\x00\x01\x02\x03")  # Random garbage bytes
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_secrets_encrypted()
            assert r.status == "warning"

    def test_plaintext_json_secrets(self, tmp_path):
        """If secrets.enc accidentally contains plaintext JSON, warn about it."""
        secrets = tmp_path / "secrets.enc"
        secrets.write_text(json.dumps({"key": "value"}))
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_secrets_encrypted()
            assert r.status == "warning"
            assert "plaintext" in r.message.lower()

    def test_empty_file(self, tmp_path):
        secrets = tmp_path / "secrets.enc"
        secrets.write_bytes(b"")
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_secrets_encrypted()
            assert r.status == "warning"


# =============================================================================
# Individual health checks (storage)
# =============================================================================


class TestCheckDiskSpace:
    def test_small_directory(self, tmp_path):
        (tmp_path / "config.json").write_text("{}")
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_disk_space()
            assert r.status == "ok"
            assert r.check_id == "disk_space"


class TestCheckAuditLogWritable:
    def test_audit_exists_writable(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_audit_log_writable()
            assert r.status == "ok"

    def test_audit_created_on_check(self, tmp_path):
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_audit_log_writable()
            assert r.status == "ok"
            assert (tmp_path / "audit.jsonl").exists()


class TestCheckMemoryDirAccessible:
    def test_memory_dir_exists(self, tmp_path):
        (tmp_path / "memory").mkdir()
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_memory_dir_accessible()
            assert r.status == "ok"

    def test_memory_dir_created(self, tmp_path):
        with patch(_P_CONFIG_DIR, return_value=tmp_path):
            r = check_memory_dir_accessible()
            assert r.status == "ok"
            assert (tmp_path / "memory").exists()


# =============================================================================
# Connectivity checks (async)
# =============================================================================


class TestCheckLlmReachable:
    @pytest.mark.asyncio
    async def test_no_api_key(self):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = ""
        with (
            patch(_P_SETTINGS, return_value=settings),
            patch.dict("os.environ", {}, clear=True),
        ):
            r = await check_llm_reachable()
            assert r.status == "critical"
            assert "No API key" in r.message

    @pytest.mark.asyncio
    async def test_open_interpreter_fallback(self):
        settings = MagicMock()
        settings.agent_backend = "open_interpreter"
        with patch(_P_SETTINGS, return_value=settings):
            r = await check_llm_reachable()
            assert r.status == "ok"
            assert "not implemented" in r.message


# =============================================================================
# Check registries
# =============================================================================


class TestCheckRegistries:
    def test_startup_checks_count(self):
        assert len(STARTUP_CHECKS) == 10

    def test_connectivity_checks_count(self):
        assert len(CONNECTIVITY_CHECKS) == 1

    def test_all_startup_checks_are_callable(self):
        for check in STARTUP_CHECKS:
            assert callable(check)


# =============================================================================
# HealthEngine
# =============================================================================


class TestHealthEngine:
    @pytest.fixture
    def engine(self, tmp_path):
        """Create a HealthEngine with isolated ErrorStore."""
        eng = HealthEngine()
        eng._error_store = ErrorStore(path=tmp_path / "errors.jsonl")
        return eng

    def test_initial_status_healthy(self, engine):
        assert engine.overall_status == "healthy"

    def test_run_startup_checks(self, engine, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text('{"agent_backend": "claude_agent_sdk"}')
        config_path.chmod(0o600)

        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = "sk-ant-test"
        settings.openai_api_key = ""
        settings.google_api_key = ""
        settings.llm_provider = "auto"

        with (
            patch(_P_CONFIG_PATH, return_value=config_path),
            patch(_P_CONFIG_DIR, return_value=tmp_path),
            patch(_P_SETTINGS, return_value=settings),
            patch("importlib.util.find_spec", return_value=MagicMock()),
        ):
            results = engine.run_startup_checks()
            assert len(results) == 10
            # All should be ok with valid config + key
            statuses = {r.status for r in results}
            assert "critical" not in statuses

    def test_overall_status_unhealthy(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
            HealthCheckResult("b", "B", "config", "critical", "broken", "fix it"),
        ]
        assert engine.overall_status == "unhealthy"

    def test_overall_status_degraded(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
            HealthCheckResult("b", "B", "config", "warning", "hmm", "check it"),
        ]
        assert engine.overall_status == "degraded"

    def test_overall_status_healthy_all_ok(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
            HealthCheckResult("b", "B", "config", "ok", "also fine", ""),
        ]
        assert engine.overall_status == "healthy"

    def test_summary(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
            HealthCheckResult("b", "B", "config", "critical", "broken", "fix"),
        ]
        engine._last_check = "2026-01-01T00:00:00"
        s = engine.summary
        assert s["status"] == "unhealthy"
        assert s["check_count"] == 2
        assert len(s["issues"]) == 1
        assert s["issues"][0]["check_id"] == "b"
        assert s["last_check"] == "2026-01-01T00:00:00"

    def test_summary_no_issues(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
        ]
        s = engine.summary
        assert s["status"] == "healthy"
        assert s["issues"] == []

    def test_health_prompt_section_healthy(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
        ]
        assert engine.get_health_prompt_section() == ""

    def test_health_prompt_section_degraded(self, engine):
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
            HealthCheckResult("b", "B Key", "config", "warning", "maybe wrong", "try this"),
        ]
        section = engine.get_health_prompt_section()
        assert "DEGRADED" in section
        assert "[WARNING]" in section
        assert "B Key" in section
        assert "try this" in section
        assert "health_check" in section

    def test_health_prompt_section_unhealthy(self, engine):
        engine._results = [
            HealthCheckResult("c", "C", "config", "critical", "bad", "fix"),
        ]
        section = engine.get_health_prompt_section()
        assert "UNHEALTHY" in section
        assert "[CRITICAL]" in section

    def test_record_error(self, engine):
        error_id = engine.record_error(message="test error", source="test", traceback="tb")
        assert len(error_id) == 12
        errors = engine.get_recent_errors()
        assert len(errors) == 1
        assert errors[0]["message"] == "test error"

    def test_record_error_never_raises(self, engine):
        # Force internal failure
        engine._error_store = MagicMock()
        engine._error_store.record.side_effect = RuntimeError("boom")
        # Should not raise
        result = engine.record_error(message="test")
        assert result == ""

    def test_get_recent_errors_never_raises(self, engine):
        engine._error_store = MagicMock()
        engine._error_store.get_recent.side_effect = RuntimeError("boom")
        result = engine.get_recent_errors()
        assert result == []

    @pytest.mark.asyncio
    async def test_run_connectivity_checks_merges(self, engine):
        # Pre-populate startup results
        engine._results = [
            HealthCheckResult("a", "A", "config", "ok", "fine", ""),
        ]
        settings = MagicMock()
        settings.agent_backend = "open_interpreter"
        with patch(_P_SETTINGS, return_value=settings):
            await engine.run_connectivity_checks()
        # Should have both startup + connectivity results
        check_ids = [r.check_id for r in engine.results]
        assert "a" in check_ids
        assert "llm_reachable" in check_ids

    @pytest.mark.asyncio
    async def test_run_all_checks(self, engine, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        settings = MagicMock()
        settings.agent_backend = "open_interpreter"
        settings.anthropic_api_key = ""
        settings.openai_api_key = ""
        settings.google_api_key = ""
        settings.llm_provider = "auto"

        with (
            patch(_P_CONFIG_PATH, return_value=config_path),
            patch(_P_CONFIG_DIR, return_value=tmp_path),
            patch(_P_SETTINGS, return_value=settings),
            patch("importlib.util.find_spec", return_value=MagicMock()),
        ):
            results = await engine.run_all_checks()
            assert len(results) >= 10  # startup + connectivity

    def test_startup_check_exception_handled(self, engine):
        """If an individual check raises, engine still returns results."""

        def bad_check():
            raise RuntimeError("check exploded")

        with patch(
            "pocketpaw.health.engine.STARTUP_CHECKS",
            [bad_check],
        ):
            results = engine.run_startup_checks()
            assert len(results) == 1
            assert results[0].status == "warning"
            assert "check exploded" in results[0].message


# =============================================================================
# Playbooks
# =============================================================================


class TestPlaybooks:
    def test_playbooks_have_required_keys(self):
        for check_id, pb in PLAYBOOKS.items():
            assert "symptom" in pb, f"Missing symptom for {check_id}"
            assert "causes" in pb, f"Missing causes for {check_id}"
            assert "fix_steps" in pb, f"Missing fix_steps for {check_id}"
            assert "auto_fixable" in pb, f"Missing auto_fixable for {check_id}"
            assert isinstance(pb["causes"], list)
            assert isinstance(pb["fix_steps"], list)
            assert len(pb["causes"]) >= 1
            assert len(pb["fix_steps"]) >= 1

    def test_known_playbook_ids(self):
        expected = {
            "api_key_primary",
            "llm_reachable",
            "config_valid_json",
            "backend_deps",
            "disk_space",
            "config_permissions",
            "secrets_encrypted",
        }
        assert set(PLAYBOOKS.keys()) == expected

    def test_diagnose_config_all(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = "sk-ant-valid"
        settings.openai_api_key = ""
        settings.google_api_key = ""
        settings.llm_provider = "auto"

        with (
            patch(_P_CONFIG_PATH, return_value=config_path),
            patch(_P_CONFIG_DIR, return_value=tmp_path),
            patch(_P_SETTINGS, return_value=settings),
            patch("importlib.util.find_spec", return_value=MagicMock()),
        ):
            report = diagnose_config()
            assert "Config Diagnosis Report" in report
            assert "Total:" in report
            assert "checks" in report

    def test_diagnose_config_section_api_keys(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = ""
        settings.openai_api_key = ""
        settings.google_api_key = ""
        settings.llm_provider = "auto"

        with (
            patch(_P_CONFIG_PATH, return_value=config_path),
            patch(_P_CONFIG_DIR, return_value=tmp_path),
            patch(_P_SETTINGS, return_value=settings),
            patch.dict("os.environ", {}, clear=True),
        ):
            report = diagnose_config(section="api_keys")
            assert "[FAIL]" in report or "[WARN]" in report
            # Should include playbook info for api_key_primary
            assert "Symptom:" in report or "Possible causes:" in report

    def test_diagnose_config_section_storage(self, tmp_path):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"

        with (
            patch(_P_CONFIG_PATH, return_value=tmp_path / "config.json"),
            patch(_P_CONFIG_DIR, return_value=tmp_path),
            patch(_P_SETTINGS, return_value=settings),
        ):
            report = diagnose_config(section="storage")
            assert "Disk Space" in report or "Audit" in report or "Memory" in report


# =============================================================================
# Health singleton
# =============================================================================


class TestHealthSingleton:
    def test_get_health_engine_returns_same_instance(self):
        import pocketpaw.health as health_mod

        # Reset singleton
        health_mod._instance = None
        try:
            e1 = health_mod.get_health_engine()
            e2 = health_mod.get_health_engine()
            assert e1 is e2
        finally:
            health_mod._instance = None

    def test_get_health_engine_creates_health_engine(self):
        import pocketpaw.health as health_mod

        health_mod._instance = None
        try:
            e = health_mod.get_health_engine()
            assert isinstance(e, HealthEngine)
        finally:
            health_mod._instance = None


# =============================================================================
# Health Tools
# =============================================================================


class TestHealthCheckTool:
    def test_tool_definition(self):
        from pocketpaw.tools.builtin.health import HealthCheckTool

        tool = HealthCheckTool()
        assert tool.name == "health_check"
        assert "diagnostics" in tool.description
        params = tool.parameters
        assert "include_connectivity" in params["properties"]

    @pytest.mark.asyncio
    async def test_execute_startup_only(self, tmp_path):
        from pocketpaw.tools.builtin.health import HealthCheckTool

        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = "sk-ant-test"
        settings.openai_api_key = ""
        settings.google_api_key = ""
        settings.llm_provider = "auto"

        import pocketpaw.health as health_mod

        health_mod._instance = None
        try:
            with (
                patch(_P_CONFIG_PATH, return_value=config_path),
                patch(_P_CONFIG_DIR, return_value=tmp_path),
                patch(_P_SETTINGS, return_value=settings),
                patch("importlib.util.find_spec", return_value=MagicMock()),
            ):
                tool = HealthCheckTool()
                result = await tool.execute(include_connectivity=False)
                assert "System Status:" in result
                assert "Total:" in result
        finally:
            health_mod._instance = None


class TestErrorLogTool:
    def test_tool_definition(self):
        from pocketpaw.tools.builtin.health import ErrorLogTool

        tool = ErrorLogTool()
        assert tool.name == "error_log"
        assert "limit" in tool.parameters["properties"]
        assert "search" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute_empty(self):
        import pocketpaw.health as health_mod
        from pocketpaw.tools.builtin.health import ErrorLogTool

        health_mod._instance = None
        try:
            engine = health_mod.get_health_engine()
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                engine._error_store = ErrorStore(path=Path(td) / "errors.jsonl")
                tool = ErrorLogTool()
                result = await tool.execute()
                assert "No errors found" in result
        finally:
            health_mod._instance = None

    @pytest.mark.asyncio
    async def test_execute_with_errors(self):
        import pocketpaw.health as health_mod
        from pocketpaw.tools.builtin.health import ErrorLogTool

        health_mod._instance = None
        try:
            engine = health_mod.get_health_engine()
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                engine._error_store = ErrorStore(path=Path(td) / "errors.jsonl")
                engine.record_error(message="Test failure", source="test.module")
                tool = ErrorLogTool()
                result = await tool.execute()
                assert "Recent Errors" in result
                assert "Test failure" in result
                assert "test.module" in result
        finally:
            health_mod._instance = None


class TestConfigDoctorTool:
    def test_tool_definition(self):
        from pocketpaw.tools.builtin.health import ConfigDoctorTool

        tool = ConfigDoctorTool()
        assert tool.name == "config_doctor"
        assert "section" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute(self, tmp_path):
        from pocketpaw.tools.builtin.health import ConfigDoctorTool

        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = "sk-ant-test"
        settings.openai_api_key = ""
        settings.google_api_key = ""
        settings.llm_provider = "auto"

        with (
            patch(_P_CONFIG_PATH, return_value=config_path),
            patch(_P_CONFIG_DIR, return_value=tmp_path),
            patch(_P_SETTINGS, return_value=settings),
            patch("importlib.util.find_spec", return_value=MagicMock()),
        ):
            tool = ConfigDoctorTool()
            result = await tool.execute(section="")
            assert "Config Diagnosis Report" in result


# =============================================================================
# Phase 2: ContextHub health_status integration
# =============================================================================


class TestContextHubHealthStatus:
    """Test that health_status is a registered context source in ContextHub."""

    def test_health_status_in_available_sources(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        assert "health_status" in hub.AVAILABLE_SOURCES

    @pytest.mark.asyncio
    async def test_gather_health_status(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        mock_summary = {
            "status": "degraded",
            "check_count": 5,
            "issues": [
                {
                    "check_id": "api_key",
                    "status": "warning",
                    "name": "API Key",
                    "message": "Missing",
                }
            ],
            "last_check": "2026-02-17T12:00:00Z",
        }

        mock_engine = MagicMock()
        mock_engine.summary = mock_summary

        with patch("pocketpaw.health.get_health_engine", return_value=mock_engine):
            result = await hub.gather(["health_status"])
            assert "health_status" in result
            assert result["health_status"]["status"] == "degraded"
            assert result["health_status"]["check_count"] == 5

    @pytest.mark.asyncio
    async def test_gather_health_status_import_failure(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        # Clear cache to force re-gather
        hub._cache.clear()

        with patch(
            "pocketpaw.daemon.context.ContextHub._gather_health_status",
            side_effect=Exception("no module"),
        ):
            result = await hub.gather(["health_status"])
            assert "health_status" in result
            assert "Error" in str(result["health_status"])

    def test_format_health_status_healthy(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        data = {"status": "healthy", "issues": [], "last_check": "2026-02-17T12:00:00Z"}
        formatted = hub._format_health_status(data)
        assert "[Health Status] HEALTHY" in formatted
        assert "All checks passing" in formatted

    def test_format_health_status_degraded(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        data = {
            "status": "degraded",
            "issues": [{"status": "warning", "name": "Disk Space", "message": "90% full"}],
            "last_check": "2026-02-17T12:00:00Z",
        }
        formatted = hub._format_health_status(data)
        assert "[Health Status] DEGRADED" in formatted
        assert "[WARNING] Disk Space: 90% full" in formatted
        assert "Last checked:" in formatted

    def test_format_health_status_in_context_string(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        context = {
            "health_status": {
                "status": "unhealthy",
                "issues": [{"status": "critical", "name": "LLM", "message": "Down"}],
            },
        }
        formatted = hub.format_context_string(context)
        assert "UNHEALTHY" in formatted
        assert "[CRITICAL] LLM: Down" in formatted

    @pytest.mark.asyncio
    async def test_apply_template_health_status(self):
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()
        context = {
            "health_status": {
                "status": "healthy",
                "issues": [],
                "last_check": "2026-02-17T12:00:00Z",
            },
        }
        template = "System health: {{health_status}}"
        result = hub.apply_template(template, context)
        assert "HEALTHY" in result
        assert "{{health_status}}" not in result

    @pytest.mark.asyncio
    async def test_gather_all_includes_health(self):
        """Gathering all sources includes health_status."""
        from pocketpaw.daemon.context import ContextHub

        hub = ContextHub()

        mock_engine = MagicMock()
        mock_engine.summary = {
            "status": "healthy",
            "check_count": 0,
            "issues": [],
            "last_check": "",
        }

        with patch("pocketpaw.health.get_health_engine", return_value=mock_engine):
            result = await hub.gather()  # None = all sources
            assert "health_status" in result
