"""PocketPaw CognitiveEngine bridge.

Routes soul cognitive tasks to PocketPaw's active agent backend,
or to a cheaper dedicated model via the Anthropic SDK when configured.

Changes:
  - 2026-04-04: Added optional `model` parameter for direct Anthropic API calls.
    When `model` is set (e.g. "claude-haiku-4-5-20251001"), bypasses the main
    backend and calls the Anthropic Messages API directly, reducing cost for
    the 5-6 cognitive calls per user message. Falls back to main backend on
    any failure or if the anthropic SDK is unavailable.

Created: feat/pocketpaw-cognitive-engine
- PocketPawCognitiveEngine implements the CognitiveEngine protocol
- Accepts a backend_provider callable for lazy backend resolution
- Streams AgentEvent responses and concatenates message-type events
- Falls back gracefully (returns empty string) if backend unavailable or errors
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw.agents.backend import AgentBackend

logger = logging.getLogger(__name__)

# System prompt used for cognitive-only calls.  The soul's cognitive pipeline
# expects structured JSON back; this prompt keeps the LLM focused on that task.
_COGNITIVE_SYSTEM_PROMPT = (
    "You are a JSON-only cognitive processor. "
    "Return only valid JSON with no explanation, preamble, or markdown fencing."
)


# Generate unique session keys so cognitive calls don't pollute each other's history.
def _cognitive_session_key() -> str:
    import uuid

    return f"__cognitive__{uuid.uuid4().hex[:8]}"


# Event types whose `content` field carries response text
_TEXT_EVENT_TYPES = frozenset({"message", "content", "text"})

# Event types that signal end of stream
_DONE_EVENT_TYPES = frozenset({"done", "stream_end"})


class PocketPawCognitiveEngine:
    """CognitiveEngine that uses PocketPaw's active agent backend.

    Wraps the agent backend's streaming `run()` call so the soul can use
    the same LLM for cognitive tasks (significance, fact extraction,
    reflection, sentiment) that drives the main conversation.

    When ``model`` is provided, uses a direct Anthropic SDK call with that
    model instead of routing through the main backend. This allows using a
    cheaper model (e.g. Haiku) for the high-volume cognitive pipeline while
    keeping the main conversation on a stronger model (e.g. Sonnet).

    The backend is resolved lazily via `backend_provider` so this engine
    can be created before the AgentRouter is initialised (which happens on
    the first in-bound message, after soul initialisation).

    Args:
        backend_provider: A zero-arg callable that returns the active
            AgentBackend instance, or None if no backend is ready yet.
        model: Optional model name for direct Anthropic API calls.
            Empty string or None means use the main backend.
        api_key: Optional Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
    """

    def __init__(
        self,
        backend_provider: Callable[[], AgentBackend | None],
        model: str = "",
        api_key: str | None = None,
    ) -> None:
        self._backend_provider = backend_provider
        self._model = model or ""
        self._api_key = api_key
        self._anthropic_client: Any | None = None  # Lazy-initialized

    def _get_anthropic_client(self) -> Any | None:
        """Lazily initialize and return the Anthropic async client.

        Returns None if the SDK is not installed or no API key is available.
        """
        if self._anthropic_client is not None:
            return self._anthropic_client

        try:
            import anthropic
        except ImportError:
            logger.warning(
                "anthropic SDK not installed — soul_cognitive_model requires it. "
                "Falling back to main backend."
            )
            return None

        key = self._api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            logger.warning(
                "No Anthropic API key available for soul cognitive model. "
                "Falling back to main backend."
            )
            return None

        self._anthropic_client = anthropic.AsyncAnthropic(api_key=key)
        return self._anthropic_client

    # ------------------------------------------------------------------
    # CognitiveEngine protocol
    # ------------------------------------------------------------------

    async def think(self, prompt: str) -> str:
        """Send a prompt to the configured model and return the full response.

        When a dedicated cognitive model is configured, tries the direct
        Anthropic API first. Falls back to the main backend on any failure.

        Args:
            prompt: The cognitive task prompt (contains a [TASK:xxx] marker
                and structured input as formatted by soul-protocol's CognitiveProcessor).

        Returns:
            The concatenated text response, or "" on failure.
        """
        # Try the dedicated cognitive model first
        if self._model:
            result = await self._think_direct(prompt)
            if result is not None:
                return result
            # Fall through to backend on failure

        backend = self._backend_provider()
        if backend is None:
            logger.debug("PocketPawCognitiveEngine.think(): no backend available, returning empty")
            return ""

        try:
            return await self._stream_to_text(backend, prompt)
        except Exception:
            logger.warning(
                "PocketPawCognitiveEngine.think() failed, soul will fall back to heuristic",
                exc_info=True,
            )
            return ""

    # ------------------------------------------------------------------
    # Direct Anthropic API path (cheaper model)
    # ------------------------------------------------------------------

    async def _think_direct(self, prompt: str) -> str | None:
        """Call the Anthropic Messages API directly with the configured model.

        Returns the response text on success, or None to signal fallback
        to the main backend.
        """
        client = self._get_anthropic_client()
        if client is None:
            return None

        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=_COGNITIVE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            # Extract text from the response content blocks
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            return "".join(text_parts).strip()
        except Exception:
            logger.warning(
                "Direct Anthropic call failed (model=%s), falling back to main backend",
                self._model,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Main backend path (streaming)
    # ------------------------------------------------------------------

    async def _stream_to_text(self, backend: Any, prompt: str) -> str:
        """Collect streamed agent events into a single response string.

        Iterates the backend's async generator, accumulating text from
        message-type events and stopping on done/stream_end events.

        Args:
            backend: An AgentBackend instance with a `run()` async generator.
            prompt: The prompt to send.

        Returns:
            Concatenated response text.
        """
        chunks: list[str] = []

        async for event in backend.run(
            message=prompt,
            system_prompt=_COGNITIVE_SYSTEM_PROMPT,
            session_key=_cognitive_session_key(),
        ):
            event_type = getattr(event, "type", "")
            content = getattr(event, "content", "") or ""

            if event_type in _TEXT_EVENT_TYPES and content:
                chunks.append(str(content))
            elif event_type in _DONE_EVENT_TYPES:
                break

        return "".join(chunks).strip()
