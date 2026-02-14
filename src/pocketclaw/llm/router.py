"""LLM Router - simple chat-completion utility for lightweight callers.

NOT used by agent backends (they have their own LLM clients).
This is a standalone utility for simple chat completions without tool use.
Potential consumers: Guardian AI, future doctor/audit commands, scripts.

Limitations:
  - No streaming (returns full response)
  - No token counting
  - Unbounded conversation history (call clear_history() to reset)
"""

import logging

import httpx

from pocketclaw.config import Settings

logger = logging.getLogger(__name__)


class LLMRouter:
    """Routes simple chat-completion requests to available backends.

    This is a utility class, not the main agent pipeline. Agent backends
    (claude_agent_sdk, pocketpaw_native, open_interpreter) have their own
    LLM clients. Use this for lightweight one-off completions.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.conversation_history: list[dict] = []
        self._available_backend: str | None = None

    async def _check_ollama(self) -> bool:
        """Check if Ollama is available."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.settings.ollama_host}/api/tags")
                return response.status_code == 200
        except Exception:
            return False

    async def _detect_backend(self) -> str | None:
        """Detect available LLM backend based on settings."""
        provider = self.settings.llm_provider

        if provider == "ollama":
            if await self._check_ollama():
                return "ollama"
            return None

        if provider == "openai":
            if self.settings.openai_api_key:
                return "openai"
            return None

        if provider == "anthropic":
            if self.settings.anthropic_api_key:
                return "anthropic"
            return None

        # Auto mode - try in order: Ollama → OpenAI → Anthropic
        if provider == "auto":
            if await self._check_ollama():
                return "ollama"
            if self.settings.openai_api_key:
                return "openai"
            if self.settings.anthropic_api_key:
                return "anthropic"

        return None

    async def chat(self, message: str) -> str:
        """Send a chat message and get a response."""
        if not self._available_backend:
            self._available_backend = await self._detect_backend()

        if not self._available_backend:
            return (
                "❌ No LLM backend available.\n\n"
                "Options:\n"
                "• Install [Ollama](https://ollama.ai) and run `ollama run llama3.2`\n"
                "• Add OpenAI API key in ⚙️ Settings\n"
                "• Add Anthropic API key in ⚙️ Settings"
            )

        self.conversation_history.append({"role": "user", "content": message})

        try:
            if self._available_backend == "ollama":
                response = await self._chat_ollama(message)
            elif self._available_backend == "openai":
                response = await self._chat_openai(message)
            elif self._available_backend == "anthropic":
                response = await self._chat_anthropic(message)
            else:
                response = "Unknown backend"

            self.conversation_history.append({"role": "assistant", "content": response})
            return response

        except Exception as e:
            logger.error(f"LLM error: {e}")
            return f"❌ LLM Error: {str(e)}"

    async def _chat_ollama(self, message: str) -> str:
        """Chat via Ollama."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.settings.ollama_host}/api/chat",
                json={
                    "model": self.settings.ollama_model,
                    "messages": self.conversation_history,
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("message", {}).get("content", "No response")

    async def _chat_openai(self, message: str) -> str:
        """Chat via OpenAI."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.settings.openai_api_key)

        response = await client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are PocketPaw, a helpful AI assistant running locally on the user's machine.",
                },
                *self.conversation_history,
            ],
        )

        return response.choices[0].message.content

    async def _chat_anthropic(self, message: str) -> str:
        """Chat via Anthropic."""
        from pocketclaw.llm.client import resolve_llm_client

        llm = resolve_llm_client(self.settings, force_provider="anthropic")
        client = llm.create_anthropic_client()

        response = await client.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=4096,
            system="You are PocketPaw, a helpful AI assistant running locally on the user's machine.",
            messages=self.conversation_history,
        )

        return response.content[0].text

    def clear_history(self) -> None:
        """Clear conversation history."""
        self.conversation_history = []
