# Health check functions — pure Python, no LLM.
# Created: 2026-02-17
# Updated: 2026-02-18 — added check_version_update (PyPI version check via update_check module).
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
    details: list[str] | None = None


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
            "details": self.details,
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
        # API key is REQUIRED for the Anthropic provider. OAuth tokens from
        # Free/Pro/Max plans are not permitted for third-party use.
        # See: https://code.claude.com/docs/en/legal-and-compliance
        import os

        # Skip check for non-Anthropic providers (Ollama, OpenAI-compatible, Gemini)
        sdk_provider = getattr(settings, "claude_sdk_provider", None) or "anthropic"
        if sdk_provider in ("ollama", "openai_compatible", "gemini"):
            return HealthCheckResult(
                check_id="api_key_primary",
                name="Primary API Key",
                category="config",
                status="ok",
                message=f"Claude SDK using {sdk_provider} provider (no Anthropic key needed)",
                fix_hint="",
            )

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
            message=(
                "No Anthropic API key found — required for Claude SDK backend. "
                "OAuth tokens from Free/Pro/Max plans are not permitted for third-party use."
            ),
            fix_hint=(
                "Get an API key at https://console.anthropic.com/api-keys "
                "and add it in Settings > API Keys, or set ANTHROPIC_API_KEY env var."
            ),
            details=[
                "Anthropic's policy prohibits third-party use of OAuth tokens from Free/Pro/Max plans.",
                "Get an API key from https://console.anthropic.com/api-keys",
                "Set it in PocketPaw Settings > API Keys, or as ANTHROPIC_API_KEY env var.",
                "Alternatively, switch to Ollama (Local) for free local inference.",
            ]
        )

    elif backend == "google_adk":
        import os

        has_key = bool(settings.google_api_key) or bool(os.environ.get("GOOGLE_API_KEY"))
        if has_key:
            return HealthCheckResult(
                check_id="api_key_primary",
                name="Primary API Key",
                category="config",
                status="ok",
                message="Google API key is configured for Google ADK",
                fix_hint="",
            )
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="critical",
            message="No Google API key found for Google ADK backend",
            fix_hint=(
                "Set your Google API key in Settings > API Keys,"
                " or set GOOGLE_API_KEY env var."
            ),
        )

    elif backend == "openai_agents":
        import os

        has_key = bool(settings.openai_api_key) or bool(os.environ.get("OPENAI_API_KEY"))
        if has_key:
            return HealthCheckResult(
                check_id="api_key_primary",
                name="Primary API Key",
                category="config",
                status="ok",
                message="OpenAI API key is configured for OpenAI Agents",
                fix_hint="",
            )
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="critical",
            message="No OpenAI API key found for OpenAI Agents backend",
            fix_hint=(
                "Set your OpenAI API key in Settings > API Keys,"
                " or set OPENAI_API_KEY env var."
            ),
        )

    elif backend in ("codex_cli", "opencode", "copilot_sdk"):
        # Subprocess-based backends manage their own auth
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="ok",
            message=f"{backend} manages its own credentials",
            fix_hint="",
        )

    # Check if it's a legacy backend name
    from pocketpaw.agents.registry import _LEGACY_BACKENDS

    if backend in _LEGACY_BACKENDS:
        fallback = _LEGACY_BACKENDS[backend]
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="warning",
            message=f"Backend '{backend}' has been removed — will fall back to '{fallback}'",
            fix_hint=f"Update agent_backend to '{fallback}' in Settings.",
        )

    from pocketpaw.agents.registry import list_backends

    return HealthCheckResult(
        check_id="api_key_primary",
        name="Primary API Key",
        category="config",
        status="warning",
        message=f"Unknown backend: {backend}",
        fix_hint=f"Set agent_backend to one of: {', '.join(list_backends())}",
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

    _BACKEND_DEPS: dict[str, tuple[str, str]] = {
        "claude_agent_sdk": ("claude_agent_sdk", "claude-agent-sdk"),
        "google_adk": ("google.adk", "pocketpaw[google-adk]"),
        "openai_agents": ("agents", "pocketpaw[openai-agents]"),
    }
    # codex_cli, opencode, copilot_sdk are subprocess backends — no pip deps

    dep = _BACKEND_DEPS.get(backend)
    if dep:
        spec_name, pip_name = dep
        if importlib.util.find_spec(spec_name) is None:
            missing.append(pip_name)

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
                        message=(
                            "Anthropic API reachable but key is invalid"
                            f" (HTTP {resp.status_code})"
                        ),
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

    elif backend == "google_adk":
        try:
            import os

            import httpx

            api_key = settings.google_api_key or os.environ.get("GOOGLE_API_KEY", "")
            if not api_key:
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="critical",
                    message="No Google API key to test connectivity",
                    fix_hint="Set your Google API key first.",
                )

            model = settings.google_adk_model or "gemini-2.5-flash"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                    params={"key": api_key},
                )
            if resp.status_code == 200:
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="ok",
                    message=f"Google AI API is reachable (model: {model})",
                    fix_hint="",
                )
            elif resp.status_code in (401, 403):
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="critical",
                    message=f"Google AI API reachable but key is invalid (HTTP {resp.status_code})",
                    fix_hint="Check your Google API key in Settings > API Keys.",
                )
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="warning",
                message=f"Google AI API returned HTTP {resp.status_code}",
                fix_hint="Check your Google API key and model name.",
            )
        except Exception as e:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="critical",
                message=f"Cannot reach Google AI API: {e}",
                fix_hint="Check your internet connection.",
            )

    elif backend == "openai_agents":
        try:
            import os

            import httpx

            api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="critical",
                    message="No OpenAI API key to test connectivity",
                    fix_hint="Set your OpenAI API key first.",
                )

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code == 200:
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="ok",
                    message="OpenAI API is reachable and key is valid",
                    fix_hint="",
                )
            elif resp.status_code in (401, 403):
                return HealthCheckResult(
                    check_id="llm_reachable",
                    name="LLM Reachable",
                    category="connectivity",
                    status="critical",
                    message=f"OpenAI API reachable but key is invalid (HTTP {resp.status_code})",
                    fix_hint="Check your OpenAI API key in Settings > API Keys.",
                )
        except Exception as e:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="critical",
                message=f"Cannot reach OpenAI API: {e}",
                fix_hint="Check your internet connection.",
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
# Update checks (sync, uses cached PyPI data — 2s timeout max)
# =============================================================================


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
                fix_hint=f"Run: pip install --upgrade pocketpaw  |  Changelog: github.com/pocketpaw/pocketpaw/releases/tag/v{latest}",
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
    check_version_update,
]

# Async checks (run in background, may be slow)
CONNECTIVITY_CHECKS = [
    check_llm_reachable,
]
