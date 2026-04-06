"""API key health checks -- primary key presence, format validation, and encryption."""

from __future__ import annotations

import importlib.util
import json

from pocketpaw.health.checks.constants import NON_ANTHROPIC_PROVIDERS, NON_OPENAI_PROVIDERS
from pocketpaw.health.checks.result import HealthCheckResult


def check_api_key_primary() -> HealthCheckResult:
    """Check that an API key exists for the selected backend."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    backend = settings.agent_backend

    if backend == "claude_agent_sdk":
        return _check_claude_sdk_key(settings)
    elif backend == "google_adk":
        return _check_google_adk_key(settings)
    elif backend == "openai_agents":
        return _check_openai_agents_key(settings)
    elif backend in ("codex_cli", "opencode", "copilot_sdk"):
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


# -- Per-backend helpers ------------------------------------------------------


def _check_claude_sdk_key(settings) -> HealthCheckResult:
    import os

    sdk_provider = getattr(settings, "claude_sdk_provider", None) or "anthropic"
    if sdk_provider in NON_ANTHROPIC_PROVIDERS:
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
        status="warning",
        message=(
            "No Anthropic API key found — required for Claude SDK backend. "
            "OAuth tokens from Free/Pro/Max plans are not permitted for third-party use."
        ),
        fix_hint=(
            "Get an API key at https://console.anthropic.com/settings/keys "
            "and add it in Settings > API Keys, or set ANTHROPIC_API_KEY env var."
        ),
        details=[
            "Anthropic's policy prohibits third-party use of OAuth tokens from Free/Pro/Max plans.",
            "Get an API key from https://console.anthropic.com/settings/keys",
            "Set it in PocketPaw Settings > API Keys, or as ANTHROPIC_API_KEY env var.",
            "Alternatively, switch to Ollama (Local) for free local inference.",
        ],
    )


def _check_google_adk_key(settings) -> HealthCheckResult:
    import os

    adk_provider = getattr(settings, "google_adk_provider", None) or "google"
    if adk_provider == "litellm":
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="ok",
            message="Google ADK using LiteLLM provider (no Google key needed)",
            fix_hint="",
        )

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
        status="warning",
        message="No Google API key found for Google ADK backend",
        fix_hint=("Set your Google API key in Settings > API Keys, or set GOOGLE_API_KEY env var."),
    )


def _check_openai_agents_key(settings) -> HealthCheckResult:
    import os

    agents_provider = getattr(settings, "openai_agents_provider", None) or "openai"
    if agents_provider in NON_OPENAI_PROVIDERS:
        return HealthCheckResult(
            check_id="api_key_primary",
            name="Primary API Key",
            category="config",
            status="ok",
            message=(f"OpenAI Agents using {agents_provider} provider (no OpenAI key needed)"),
            fix_hint="",
        )

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
        status="warning",
        message="No OpenAI API key found for OpenAI Agents backend",
        fix_hint=("Set your OpenAI API key in Settings > API Keys, or set OPENAI_API_KEY env var."),
    )


def check_api_key_format() -> HealthCheckResult:
    """Validate that configured API keys match expected prefix patterns."""
    from pocketpaw.config import _API_KEY_PATTERNS, get_settings

    settings = get_settings()
    warnings = []

    for field_name, validator in _API_KEY_PATTERNS.items():
        value = getattr(settings, field_name, None)
        pattern = validator["pattern"]
        if value and isinstance(value, str) and not pattern.match(value):
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

    if text.startswith("gAAAA"):
        return HealthCheckResult(
            check_id="secrets_encrypted",
            name="Secrets Encrypted",
            category="config",
            status="ok",
            message=f"Encrypted secrets file is valid ({len(raw)} bytes)",
            fix_hint="",
        )

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
