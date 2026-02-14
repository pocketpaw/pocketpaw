"""User-friendly error messages for LLM backend failures.

Created: 2026-02-13
Related: Issue #34 — Improve error messages when LLM backend is unreachable.

Instead of showing raw Python tracebacks, the ``format_backend_error``
function classifies common exceptions into actionable guidance the user
can act on (wrong API key, network down, Ollama not running, etc.).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def format_backend_error(exc: Exception) -> str:
    """Classify a backend exception into a human-friendly error message.

    Instead of showing raw Python tracebacks, this returns actionable
    guidance so users can self-diagnose common issues like wrong API keys,
    network problems, or rate limits.

    The function inspects ``type(exc).__module__`` and ``type(exc).__name__``
    so it works **without importing** the optional SDK packages (anthropic,
    openai, httpx …).

    Args:
        exc: The exception raised by an LLM backend or HTTP client.

    Returns:
        A Markdown-formatted string suitable for display in a chat message.
    """
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__ or ""

    # --- Anthropic SDK errors ---
    if "anthropic" in exc_module:
        if "AuthenticationError" in exc_type:
            return (
                "**Anthropic API key is invalid or missing.**\n\n"
                "Go to **Settings → API Keys** and check your Anthropic key. "
                "It should start with `sk-ant-`."
            )
        if "RateLimitError" in exc_type:
            return (
                "**Anthropic rate limit reached.** "
                "Please wait a moment and try again.\n\n"
                "If this keeps happening, check your plan limits at "
                "[console.anthropic.com](https://console.anthropic.com)."
            )
        if "APIConnectionError" in exc_type:
            return (
                "**Can't connect to the Anthropic API.**\n\n"
                "Check your internet connection and try again. "
                "If you're behind a proxy, make sure `HTTPS_PROXY` is set."
            )
        if "APIStatusError" in exc_type or "APIError" in exc_type:
            return (
                f"**Anthropic API returned an error:** {exc}\n\n"
                "This is usually temporary. Try again in a few seconds."
            )

    # --- OpenAI SDK errors ---
    if "openai" in exc_module:
        if "AuthenticationError" in exc_type:
            return (
                "**OpenAI API key is invalid or missing.**\n\n"
                "Go to **Settings → API Keys** and check your OpenAI key. "
                "It should start with `sk-`."
            )
        if "RateLimitError" in exc_type:
            return (
                "**OpenAI rate limit reached.** "
                "Please wait a moment and try again.\n\n"
                "If this keeps happening, check your usage at "
                "[platform.openai.com](https://platform.openai.com)."
            )
        if "APIConnectionError" in exc_type:
            return (
                "**Can't connect to the OpenAI API.**\n\n"
                "Check your internet connection and try again."
            )

    # --- httpx / httpcore connection errors (used by Anthropic & OpenAI SDKs) ---
    if "httpx" in exc_module or "httpcore" in exc_module:
        return (
            "**Network error — can't reach the LLM API.**\n\n"
            "Check your internet connection. If you're using Ollama, "
            "make sure it's running (`ollama serve`)."
        )

    # --- Standard library connection errors ---
    if isinstance(exc, ConnectionError):
        return (
            "**Connection failed.**\n\n"
            "The LLM backend is unreachable. Check your network or "
            "verify the backend is running."
        )

    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (
        111,  # Connection refused
        113,  # No route to host
        101,  # Network unreachable
    ):
        return (
            "**Can't connect to the LLM backend** — connection refused.\n\n"
            "If you're using Ollama, start it with `ollama serve`.\n"
            "Otherwise, check that the configured API endpoint is correct."
        )

    # --- Fallback: clean up the raw error ---
    return (
        f"**Something went wrong:** {exc}\n\n"
        "If this keeps happening, try switching the agent backend in "
        "**Settings → General**, or check the logs for details."
    )
