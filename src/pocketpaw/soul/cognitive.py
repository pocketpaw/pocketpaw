"""PocketPaw CognitiveEngine bridge.

Routes soul cognitive tasks to PocketPaw's active agent backend.
No extra API key — uses the same LLM powering the conversation.

Created: feat/pocketpaw-cognitive-engine
- PocketPawCognitiveEngine implements the CognitiveEngine protocol
- Accepts a backend_provider callable for lazy backend resolution
- Streams AgentEvent responses and concatenates message-type events
- Falls back gracefully (returns empty string) if backend unavailable or errors
"""

from __future__ import annotations

import logging
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

# Sentinel session key so cognitive calls are isolated from conversation history.
_COGNITIVE_SESSION_KEY = "__cognitive__"

# Event types whose `content` field carries response text
_TEXT_EVENT_TYPES = frozenset({"message", "content", "text"})

# Event types that signal end of stream
_DONE_EVENT_TYPES = frozenset({"done", "stream_end"})


class PocketPawCognitiveEngine:
    """CognitiveEngine that uses PocketPaw's active agent backend.

    Wraps the agent backend's streaming `run()` call so the soul can use
    the same LLM for cognitive tasks (significance, fact extraction,
    reflection, sentiment) that drives the main conversation.

    The backend is resolved lazily via `backend_provider` so this engine
    can be created before the AgentRouter is initialised (which happens on
    the first in-bound message, after soul initialisation).

    Args:
        backend_provider: A zero-arg callable that returns the active
            AgentBackend instance, or None if no backend is ready yet.
    """

    def __init__(self, backend_provider: Callable[[], AgentBackend | None]) -> None:
        self._backend_provider = backend_provider

    # ------------------------------------------------------------------
    # CognitiveEngine protocol
    # ------------------------------------------------------------------

    async def think(self, prompt: str) -> str:
        """Send a prompt to the active backend and return the full response.

        Streams events from `backend.run()` and concatenates the content
        from all message-type events.  Returns an empty string on any
        failure so the soul falls back to heuristics gracefully.

        Args:
            prompt: The cognitive task prompt (contains a [TASK:xxx] marker
                and structured input as formatted by soul-protocol's CognitiveProcessor).

        Returns:
            The concatenated text response from the backend, or "" on failure.
        """
        backend = self._backend_provider()
        if backend is None:
            logger.debug(
                "PocketPawCognitiveEngine.think(): no backend available, returning empty"
            )
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
    # Internal helpers
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
            session_key=_COGNITIVE_SESSION_KEY,
        ):
            event_type = getattr(event, "type", "")
            content = getattr(event, "content", "") or ""

            if event_type in _TEXT_EVENT_TYPES and content:
                chunks.append(str(content))
            elif event_type in _DONE_EVENT_TYPES:
                break

        return "".join(chunks).strip()
