"""Human-friendly error messages for LLM backend failures.

This module provides utilities to convert technical exceptions from various
LLM providers into user-friendly messages that don't expose raw stack traces.
"""

import logging

logger = logging.getLogger(__name__)


def format_anthropic_error(error: Exception) -> str:
    """Convert Anthropic SDK errors to human-friendly messages.

    Handles:
    - AuthenticationError (401): Invalid/missing API key
    - APIConnectionError: Network issues
    - RateLimitError (429): Too many requests
    - APITimeoutError: Request timeout
    - BadRequestError (400): Malformed request
    - PermissionDeniedError (403): Forbidden
    - NotFoundError (404): Resource not found
    """
    error_str = str(error)
    error_type = type(error).__name__

    try:
        import anthropic

        if isinstance(error, anthropic.AuthenticationError):
            return (
                "🔐 **Authentication Failed**\n\n"
                "Your Anthropic API key is invalid or missing.\n\n"
                "**To fix:**\n"
                "1. Open Settings → API Keys in the sidebar\n"
                "2. Enter your Anthropic API key (starts with `sk-ant-`)\n"
                "3. Save and try again"
            )

        if isinstance(error, anthropic.APIConnectionError):
            cause = getattr(error, "__cause__", None)
            if cause:
                cause_str = str(cause).lower()
                if "connect" in cause_str or "refused" in cause_str:
                    return (
                        "🌐 **Can't reach Anthropic API**\n\n"
                        "Your computer can't connect to Anthropic's servers.\n\n"
                        "**Check:**\n"
                        "- Your internet connection\n"
                        "- Any VPN or firewall blocking connections\n"
                        "- Try again in a few moments"
                    )
            return (
                "🌐 **Connection Error**\n\n"
                "Couldn't reach the Anthropic API. Check your internet connection."
            )

        if isinstance(error, anthropic.RateLimitError):
            return (
                "⏳ **Rate Limit Exceeded**\n\n"
                "You've made too many requests to Anthropic.\n\n"
                "**To fix:**\n"
                "- Wait a moment and try again\n"
                "- Check your API usage at anthropic.com/console"
            )

        if isinstance(error, anthropic.APITimeoutError):
            return (
                "⏱️ **Request Timed Out**\n\n"
                "The request took too long to complete.\n\n"
                "**Try:**\n"
                "- Check your internet connection\n"
                "- Try again later"
            )

        if isinstance(error, anthropic.BadRequestError):
            return (
                "⚠️ **Invalid Request**\n\n"
                f"The request was malformed: {error_str[:200]}\n\n"
                "This may be a bug. Try rephrasing your message."
            )

        if isinstance(error, anthropic.PermissionDeniedError):
            return (
                "🚫 **Access Denied**\n\n"
                "Your API key doesn't have permission for this request.\n\n"
                "**Check:**\n"
                "- Your API key permissions at anthropic.com\n"
                "- Try regenerating your API key"
            )

        if isinstance(error, anthropic.NotFoundError):
            return (
                "🔍 **Not Found**\n\n"
                "The requested resource wasn't found.\n\n"
                "This may be a bug or an invalid model specified."
            )

        if isinstance(error, anthropic.InternalServerError):
            return (
                "🔧 **Anthropic Server Error**\n\n"
                "An unexpected error occurred on Anthropic's servers.\n\n"
                "**Try:**\n"
                "- Wait a moment and try again\n"
                "- Check status at anthropic.com"
            )

    except ImportError:
        pass

    logger.debug(f"Unhandled Anthropic error type: {error_type}")
    return (
        f"⚠️ **Error**: {error_str[:200]}\n\n"
        "If this keeps happening, check your API key and try again."
    )


def format_ollama_error(error: Exception, error_str: str = "") -> str:
    """Convert Ollama-specific errors to human-friendly messages.

    Handles:
    - Connection refused: Ollama not running
    - Timeout: Ollama not responding
    - Model not found: Model not downloaded
    """
    error_str = error_str or str(error)
    error_lower = error_str.lower()

    if "connection refused" in error_lower or "connectex" in error_lower:
        return (
            "🦙 **Ollama Not Running**\n\n"
            "Ollama isn't running on your computer.\n\n"
            "**To start Ollama:**\n"
            "```bash\nollama serve\n```\n"
            "Or open the Ollama app.\n\n"
            "Then try your message again."
        )

    if "timeout" in error_lower or "timed out" in error_lower:
        return (
            "⏱️ **Ollama Timeout**\n\n"
            "Ollama took too long to respond.\n\n"
            "**Try:**\n"
            "- A smaller model: `Settings → General → Ollama Model → llama3.2:3b`\n"
            "- Check your system resources\n"
            "- Try again"
        )

    if "model not found" in error_lower or "model" in error_lower and "not found" in error_lower:
        return (
            "📦 **Model Not Found**\n\n"
            "The requested Ollama model isn't downloaded.\n\n"
            "**To fix:**\n"
            "```bash\nollama pull <model-name>\n```\n"
            "Or choose a different model in Settings → General."
        )

    if "context length" in error_lower or "context_window" in error_lower:
        return (
            "📝 **Conversation Too Long**\n\n"
            "The conversation has exceeded the model's context limit.\n\n"
            "**To fix:**\n"
            "- Start a new session (/new)\n"
            "- Use a model with larger context (e.g., llama3.1:70b)"
        )

    return (
        f"⚠️ **Ollama Error**: {error_str[:200]}\n\n"
        "Check that Ollama is running and try again."
    )


def format_openai_error(error: Exception) -> str:
    """Convert OpenAI SDK errors to human-friendly messages.

    Handles:
    - AuthenticationError: Invalid/missing API key
    - APIConnectionError: Network issues
    - RateLimitError: Too many requests
    - Timeout: Request timeout
    """
    error_str = str(error)

    try:
        import openai

        if isinstance(error, openai.AuthenticationError):
            return (
                "🔐 **OpenAI Authentication Failed**\n\n"
                "Your OpenAI API key is invalid or missing.\n\n"
                "**To fix:**\n"
                "1. Open Settings → API Keys in the sidebar\n"
                "2. Enter your OpenAI API key (starts with `sk-`)\n"
                "3. Save and try again"
            )

        if isinstance(error, openai.RateLimitError):
            return (
                "⏳ **OpenAI Rate Limit**\n\n"
                "You've made too many requests to OpenAI.\n\n"
                "**Try:**\n"
                "- Wait a moment and try again\n"
                "- Check your API usage at platform.openai.com"
            )

        if isinstance(error, (openai.APIConnectionError, openai.Timeout)):
            return (
                "🌐 **Can't reach OpenAI**\n\n"
                "Your computer can't connect to OpenAI's servers.\n\n"
                "**Check:**\n"
                "- Your internet connection\n"
                "- Any VPN or firewall blocking connections\n"
                "- Try again in a few moments"
            )

    except ImportError:
        pass

    return (
        f"⚠️ **OpenAI Error**: {error_str[:200]}\n\n"
        "Check your API key and try again."
    )


def format_connection_error(error: Exception, backend: str) -> str:
    """Convert generic connection errors based on configured backend.

    Args:
        error: The exception that occurred
        backend: The backend name (anthropic, openai, ollama, open_interpreter)
    """
    error_str = str(error)

    if backend in ("anthropic", "pocketpaw_native"):
        try:
            import anthropic
            if isinstance(error, anthropic.APIConnectionError):
                return format_anthropic_error(error)
        except ImportError:
            pass

    if backend == "openai" or "openai" in backend:
        try:
            import openai
            if isinstance(error, (openai.APIConnectionError, openai.Timeout)):
                return format_openai_error(error)
        except ImportError:
            pass

    if backend == "ollama" or "ollama" in backend:
        return format_ollama_error(error, error_str)

    error_lower = error_str.lower()
    if "connection" in error_lower or "refused" in error_lower or "timeout" in error_lower:
        return (
            f"🌐 **Connection Error**\n\n"
            f"Couldn't connect to the {backend} service.\n\n"
            "**Check:**\n"
            "- Your internet connection\n"
            "- The service is running\n"
            "- Try again"
        )

    return (
        f"⚠️ **{backend.title()} Error**: {error_str[:200]}\n\n"
        "If this keeps happening, check your settings and try again."
    )
