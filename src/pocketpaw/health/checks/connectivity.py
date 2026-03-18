"""Connectivity health checks -- LLM API reachability.

Provider-aware: routes to the correct endpoint based on the active
provider for each backend (e.g. OpenRouter, LiteLLM, Ollama).
"""

from __future__ import annotations

from pocketpaw.health.checks.constants import NON_ANTHROPIC_PROVIDERS, NON_OPENAI_PROVIDERS
from pocketpaw.health.checks.result import HealthCheckResult

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


async def check_llm_reachable() -> HealthCheckResult:
    """Check that the configured LLM API responds (5s timeout)."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    backend = settings.agent_backend

    if backend == "claude_agent_sdk":
        provider = getattr(settings, "claude_sdk_provider", None) or "anthropic"
        if provider in NON_ANTHROPIC_PROVIDERS:
            return await _check_alt_provider_reachable(settings, provider)
        return await _check_anthropic_reachable(settings)

    elif backend == "google_adk":
        provider = getattr(settings, "google_adk_provider", None) or "google"
        if provider == "litellm":
            return await _check_alt_provider_reachable(settings, "litellm")
        return await _check_google_reachable(settings)

    elif backend == "openai_agents":
        provider = getattr(settings, "openai_agents_provider", None) or "openai"
        if provider in NON_OPENAI_PROVIDERS:
            return await _check_alt_provider_reachable(settings, provider)
        return await _check_openai_reachable(settings)

    return HealthCheckResult(
        check_id="llm_reachable",
        name="LLM Reachable",
        category="connectivity",
        status="ok",
        message=f"Connectivity check not implemented for {backend}",
        fix_hint="",
    )


# -- Provider-specific checks -------------------------------------------------


async def _check_alt_provider_reachable(settings, provider: str) -> HealthCheckResult:
    """Connectivity check for non-default providers (OpenRouter, LiteLLM, Ollama, etc.)."""
    if provider == "ollama":
        return await _check_ollama_reachable(settings)
    elif provider == "openrouter":
        return await _check_openrouter_reachable(settings)
    elif provider == "litellm":
        return await _check_litellm_reachable(settings)
    elif provider == "openai_compatible":
        return await _check_openai_compat_reachable(settings)

    return HealthCheckResult(
        check_id="llm_reachable",
        name="LLM Reachable",
        category="connectivity",
        status="ok",
        message=f"Connectivity check not implemented for provider '{provider}'",
        fix_hint="",
    )


async def _check_ollama_reachable(settings) -> HealthCheckResult:
    """Ping the Ollama server at its configured host."""
    import httpx

    host = getattr(settings, "ollama_host", "http://localhost:11434")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{host.rstrip('/')}/api/tags")
        if resp.status_code == 200:
            data = resp.json()
            model_count = len(data.get("models", []))
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="ok",
                message=f"Ollama is reachable at {host} ({model_count} models available)",
                fix_hint="",
            )
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="warning",
            message=f"Ollama returned HTTP {resp.status_code}",
            fix_hint=f"Check that Ollama is running at {host}",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="critical",
            message=f"Cannot reach Ollama at {host}: {e}",
            fix_hint="Start Ollama with: ollama serve",
        )


async def _check_openrouter_reachable(settings) -> HealthCheckResult:
    """Ping the OpenRouter /models endpoint."""
    import os

    import httpx

    api_key = getattr(settings, "openrouter_api_key", None) or os.environ.get(
        "OPENROUTER_API_KEY", ""
    )
    if not api_key:
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="warning",
            message="No OpenRouter API key to test connectivity",
            fix_hint="Set your OpenRouter API key in Settings > API Keys.",
        )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _OPENROUTER_MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="ok",
                message="OpenRouter API is reachable and key is valid",
                fix_hint="",
            )
        elif resp.status_code in (401, 403):
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="critical",
                message=f"OpenRouter reachable but key is invalid (HTTP {resp.status_code})",
                fix_hint="Check your OpenRouter API key in Settings > API Keys.",
            )
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="warning",
            message=f"OpenRouter API returned HTTP {resp.status_code}",
            fix_hint="Check https://openrouter.ai/status for outages.",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="critical",
            message=f"Cannot reach OpenRouter API: {e}",
            fix_hint="Check your internet connection.",
        )


async def _check_litellm_reachable(settings) -> HealthCheckResult:
    """Ping the LiteLLM proxy /health or /models endpoint."""
    import os

    import httpx

    base_url = getattr(settings, "litellm_base_url", "") or os.environ.get(
        "LITELLM_BASE_URL", "http://localhost:4000"
    )
    api_key = getattr(settings, "litellm_api_key", None) or os.environ.get("LITELLM_API_KEY", "")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/health", headers=headers)
        if resp.status_code == 200:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="ok",
                message=f"LiteLLM proxy is reachable at {base_url}",
                fix_hint="",
            )
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="warning",
            message=f"LiteLLM proxy returned HTTP {resp.status_code}",
            fix_hint=f"Check that LiteLLM is running at {base_url}",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="critical",
            message=f"Cannot reach LiteLLM proxy at {base_url}: {e}",
            fix_hint="Start LiteLLM with: litellm --config /path/to/config.yaml",
        )


async def _check_openai_compat_reachable(settings) -> HealthCheckResult:
    """Ping a generic OpenAI-compatible endpoint's /models route."""
    import os

    import httpx

    base_url = getattr(settings, "openai_compatible_base_url", "") or ""
    if not base_url:
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="warning",
            message="No OpenAI-compatible base URL configured",
            fix_hint="Set the base URL in Settings > General.",
        )

    api_key = getattr(settings, "openai_compatible_api_key", None) or os.environ.get(
        "OPENAI_COMPATIBLE_API_KEY", ""
    )
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
        if resp.status_code == 200:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="ok",
                message=f"OpenAI-compatible endpoint is reachable at {base_url}",
                fix_hint="",
            )
        elif resp.status_code in (401, 403):
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="critical",
                message=f"Endpoint reachable but auth failed (HTTP {resp.status_code})",
                fix_hint="Check your API key for the OpenAI-compatible endpoint.",
            )
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="warning",
            message=f"OpenAI-compatible endpoint returned HTTP {resp.status_code}",
            fix_hint=f"Check that the server is running at {base_url}",
        )
    except Exception as e:
        return HealthCheckResult(
            check_id="llm_reachable",
            name="LLM Reachable",
            category="connectivity",
            status="critical",
            message=f"Cannot reach endpoint at {base_url}: {e}",
            fix_hint="Check the base URL and that the server is running.",
        )


# -- Original provider checks (Anthropic, Google, OpenAI) ---------------------


async def _check_anthropic_reachable(settings) -> HealthCheckResult:
    try:
        import os

        import httpx

        api_key = settings.anthropic_api_key
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="warning",
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
        if resp.status_code == 200:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="ok",
                message="Anthropic API is reachable and key is valid",
                fix_hint="",
            )
        elif resp.status_code in (401, 403):
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="critical",
                message=(f"Anthropic API reachable but key is invalid (HTTP {resp.status_code})"),
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


async def _check_google_reachable(settings) -> HealthCheckResult:
    try:
        import os

        import httpx

        api_key = settings.google_api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="warning",
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


async def _check_openai_reachable(settings) -> HealthCheckResult:
    try:
        import os

        import httpx

        api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="warning",
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
        else:
            return HealthCheckResult(
                check_id="llm_reachable",
                name="LLM Reachable",
                category="connectivity",
                status="warning",
                message=f"OpenAI API returned unexpected HTTP {resp.status_code}",
                fix_hint="Check https://status.openai.com for outages.",
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
