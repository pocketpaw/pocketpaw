# Health check functions — pure Python, no LLM.
# Created: 2026-02-17
# Updated: 2026-02-17 — fix check_secrets_encrypted: was doing json.loads() on
#   Fernet-encrypted bytes (always fails). Now checks for Fernet token signature.
# Each check returns a HealthCheckResult dataclass.

from __future__ import annotations

import importlib.util
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    check_id: str  # e.g. "api_key_primary"
    name: str  # e.g. "Primary API Key"
    category: str  # "config" | "connectivity" | "storage"
    status: str  # "ok" | "warning" | "critical"
    message: str  # e.g. "Anthropic API key is configured"
    fix_hint: str  # e.g. "Set your API key in Settings > API Keys"
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(tz=UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "message": self.message,
            "fix_hint": self.fix_hint,
            "timestamp": self.timestamp,
        }


# =============================================================================
# Config checks (sync, fast)
# =============================================================================


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
            status="ok",
            message="Permission check skipped on Windows",
            fix_hint="",
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


def check_api_key_primary() -> HealthCheckResult:
    """Check that an API key exists for the selected backend."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    backend = settings.agent_backend

    if backend == "claude_agent_sdk":
        # Claude Agent SDK uses its own auth (ANTHROPIC_API_KEY env var)
        # Check both settings and env
        import os

        has_key = bool(settings.anthropic_api_key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
        if has_key:
            return HealthCheckResult(
                check_id="api_key_primary",
                name="Primary API Key",
                category="config",
                status="ok",
                message="Anthropic API key is configured",
                fix_hint="",
            )
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="critical",
            message="No Anthropic API key found for Claude Agent SDK backend",
            fix_hint="Set your API key in Settings > API Keys, or set ANTHROPIC_API_KEY env var.",
        )

    elif backend == "pocketpaw_native":
        provider = settings.llm_provider
        if provider == "ollama":
            return HealthCheckResult(
                check_id="api_key_primary",
                name="Primary API Key",
                category="config",
                status="ok",
                message="Ollama backend (no API key needed)",
                fix_hint="",
            )
        elif provider == "anthropic" or provider == "auto":
            if settings.anthropic_api_key:
                return HealthCheckResult(
                    check_id="api_key_primary",
                    name="Primary API Key",
                    category="config",
                    status="ok",
                    message="Anthropic API key configured for Native backend",
                    fix_hint="",
                )
        elif provider == "openai":
            if settings.openai_api_key:
                return HealthCheckResult(
                    check_id="api_key_primary",
                    name="Primary API Key",
                    category="config",
                    status="ok",
                    message="OpenAI API key configured",
                    fix_hint="",
                )
        elif provider == "gemini":
            if settings.google_api_key:
                return HealthCheckResult(
                    check_id="api_key_primary",
                    name="Primary API Key",
                    category="config",
                    status="ok",
                    message="Google API key configured for Gemini",
                    fix_hint="",
                )

        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="critical",
            message=f"No API key for {backend} with provider={provider}",
            fix_hint="Set your API key in Settings > API Keys.",
        )

    elif backend == "open_interpreter":
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="ok",
            message="Open Interpreter manages its own credentials",
            fix_hint="",
        )

    return HealthCheckResult(
        check_id="api_key_primary",
        name="Primary API Key",
        category="config",
        status="warning",
        message=f"Unknown backend: {backend}",
        fix_hint="Set agent_backend to 'claude_agent_sdk', 'pocketpaw_native', or 'open_interpreter'.",
    )


# API key format patterns
_KEY_PATTERNS = {
    "anthropic_api_key": re.compile(r"^sk-ant-"),
    "openai_api_key": re.compile(r"^sk-"),
}


def check_api_key_format() -> HealthCheckResult:
    """Validate that configured API keys match expected prefix patterns."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    warnings = []

    for field_name, pattern in _KEY_PATTERNS.items():
        value = getattr(settings, field_name, None)
        if value and not pattern.match(value):
            warnings.append(f"{field_name} doesn't match expected format ({pattern.pattern})")

    if warnings:
        return HealthCheckResult(
            check_id="api_key_format",
            name="API Key Format",
            category="config",
            status="warning",
            message="; ".join(warnings),
            fix_hint="Double-check your API keys for typos or truncation.",
        )
    return HealthCheckResult(
        check_id="api_key_format",
        name="API Key Format",
        category="config",
        status="ok",
        message="API key formats look correct",
        fix_hint="",
    )


def check_backend_deps() -> HealthCheckResult:
    """Check that required packages are importable for the selected backend."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    backend = settings.agent_backend
    missing = []

    if backend == "claude_agent_sdk":
        if importlib.util.find_spec("claude_code_sdk") is None:
            missing.append("claude-code-sdk")
    elif backend == "pocketpaw_native":
        if importlib.util.find_spec("anthropic") is None:
            missing.append("anthropic")
    elif backend == "open_interpreter":
        if importlib.util.find_spec("interpreter") is None:
            missing.append("open-interpreter")

    if missing:
        return HealthCheckResult(
            check_id="backend_deps",
            name="Backend Dependencies",
            category="config",
            status="critical",
            message=f"Missing packages for {backend}: {', '.join(missing)}",
            fix_hint=f"Install: pip install {' '.join(missing)}",
        )
    return HealthCheckResult(
        check_id="backend_deps",
        name="Backend Dependencies",
        category="config",
        status="ok",
        message=f"All dependencies available for {backend}",
        fix_hint="",
    )


def check_secrets_encrypted() -> HealthCheckResult:
    """Check that secrets.enc exists and contains a valid Fernet token.

    secrets.enc is Fernet-encrypted binary data (base64url), NOT plain JSON.
    Fernet tokens start with version byte 0x80, which base64-encodes to 'gAAAA'.
    """
    from pocketpaw.config import get_config_dir

    secrets_path = get_config_dir() / "secrets.enc"
    if not secrets_path.exists():
        return HealthCheckResult(
            check_id="secrets_encrypted",
            name="Secrets Encrypted",
            category="config",
            status="warning",
            message="No encrypted secrets file found",
            fix_hint="Save settings in the dashboard to create encrypted credentials.",
        )

    raw = secrets_path.read_bytes()
    if len(raw) == 0:
        return HealthCheckResult(
            check_id="secrets_encrypted",
            name="Secrets Encrypted",
            category="config",
            status="warning",
            message="Encrypted secrets file is empty",
            fix_hint="Re-save your API keys in Settings to regenerate.",
        )

    # Fernet tokens are base64url text starting with version byte 0x80 → "gAAAA"
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return HealthCheckResult(
            check_id="secrets_encrypted",
            name="Secrets Encrypted",
            category="config",
            status="warning",
            message="Encrypted secrets file contains invalid binary data",
            fix_hint="Re-save your API keys in Settings to regenerate.",
        )

    # Valid Fernet token check
    if text.startswith("gAAAA"):
        return HealthCheckResult(
            check_id="secrets_encrypted",
            name="Secrets Encrypted",
            category="config",
            status="ok",
            message=f"Encrypted secrets file is valid ({len(raw)} bytes)",
            fix_hint="",
        )

    # If it parses as JSON, it's plaintext (not encrypted) — that's wrong
    try:
        json.loads(text)
        return HealthCheckResult(
            check_id="secrets_encrypted",
            name="Secrets Encrypted",
            category="config",
            status="warning",
            message="Secrets file contains plaintext JSON — not encrypted",
            fix_hint="Re-save your API keys in Settings to encrypt them.",
        )
    except (json.JSONDecodeError, ValueError):
        pass

    return HealthCheckResult(
        check_id="secrets_encrypted",
        name="Secrets Encrypted",
        category="config",
        status="warning",
        message="Secrets file exists but is not a recognized Fernet token",
        fix_hint="Re-save your API keys in Settings to regenerate.",
    )


# =============================================================================
# Storage checks (sync, fast)
# =============================================================================


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
        # Try creating it
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


# =============================================================================
# Connectivity checks (async, background)
# =============================================================================


async def check_llm_reachable() -> HealthCheckResult:
    """Check that the configured LLM API responds (5s timeout)."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    backend = settings.agent_backend

    if backend == "claude_agent_sdk":
        # Test Anthropic API with a lightweight call
        try:
            import httpx

            api_key = settings.anthropic_api_key
            import os

            if not api_key:
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="critical",
                    message="No API key to test connectivity",
                    fix_hint="Set your Anthropic API key first.",
                )

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
            if resp.status_code in (200, 401, 403):
                # 200 = valid key, 401/403 = key exists but invalid
                if resp.status_code == 200:
                    return HealthCheckResult(
                        check_id="llm_reachable",
                        name="LLM Reachable",
                        category="connectivity",
                        status="ok",
                        message="Anthropic API is reachable and key is valid",
                        fix_hint="",
                    )
                else:
                    return HealthCheckResult(
                        check_id="llm_reachable",
                        name="LLM Reachable",
                        category="connectivity",
                        status="critical",
                        message=f"Anthropic API reachable but key is invalid (HTTP {resp.status_code})",
                        fix_hint="Check your API key in Settings > API Keys.",
                    )
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="warning",
                message=f"Anthropic API returned HTTP {resp.status_code}",
                fix_hint="Check https://status.anthropic.com for outages.",
            )
        except Exception as e:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="critical",
                message=f"Cannot reach Anthropic API: {e}",
                fix_hint="Check your internet connection or https://status.anthropic.com",
            )

    elif backend == "pocketpaw_native":
        provider = settings.llm_provider
        if provider == "ollama":
            try:
                import httpx

                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{settings.ollama_host}/api/tags")
                if resp.status_code == 200:
                    return HealthCheckResult(
                        check_id="llm_reachable",
                        name="LLM Reachable",
                        category="connectivity",
                        status="ok",
                        message=f"Ollama is reachable at {settings.ollama_host}",
                        fix_hint="",
                    )
            except Exception as e:
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="critical",
                    message=f"Cannot reach Ollama at {settings.ollama_host}: {e}",
                    fix_hint="Start Ollama with: ollama serve",
                )

    # Fallback for other backends
    return HealthCheckResult(
        check_id="llm_reachable",
        name="LLM Reachable",
        category="connectivity",
        status="ok",
        message=f"Connectivity check not implemented for {backend}",
        fix_hint="",
    )


# =============================================================================
# Check registry
# =============================================================================

# Sync checks (run at startup, fast)
STARTUP_CHECKS = [
    check_config_exists,
    check_config_valid_json,
    check_config_permissions,
    check_api_key_primary,
    check_api_key_format,
    check_backend_deps,
    check_secrets_encrypted,
    check_disk_space,
    check_audit_log_writable,
    check_memory_dir_accessible,
]

# Async checks (run in background, may be slow)
CONNECTIVITY_CHECKS = [
    check_llm_reachable,
]
