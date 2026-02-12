"""Open Interpreter agent wrapper - Direct Ollama Implementation.
Changes:
 2026-02-12 - Completely rewritten to bypass Open Interpreter's broken streaming
              using direct litellm calls for reliable Ollama support
 2026-02-05 - Emit tool_use/tool_result events for Activity panel
"""
import asyncio
import logging
from collections.abc import AsyncIterator
from pocketclaw.config import Settings

logger = logging.getLogger(__name__)


class OpenInterpreterAgent:
    """Wraps Direct LiteLLM calls for Ollama support.
    In the Agent SDK architecture, this serves as the EXECUTOR layer:
    - Executes code and system commands (via LLM reasoning)
    - Handles file operations
    - Provides sandboxed execution environment
    
    CRITICAL: Uses direct litellm instead of Open Interpreter to avoid
    the httpx/streaming bug with Ollama.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._stop_flag = False
        self._semaphore = asyncio.Semaphore(1)
        self._initialize()

    def _initialize(self) -> None:
        """Initialize LiteLLM for the configured provider."""
        try:
            import litellm
            
            provider = self.settings.llm_provider

            # Configure LiteLLM based on provider
            if provider == "anthropic" and self.settings.anthropic_api_key:
                self._model = self.settings.anthropic_model
                litellm.api_key = self.settings.anthropic_api_key
                logger.info(f"🤖 Using Anthropic: {self._model}")
            elif provider == "openai" and self.settings.openai_api_key:
                self._model = self.settings.openai_model
                litellm.api_key = self.settings.openai_api_key
                logger.info(f"🤖 Using OpenAI: {self._model}")
            elif provider == "ollama":
                # For Ollama, model format is "ollama/model-name"
                self._model = f"ollama/{self.settings.ollama_model}"
                # Set Ollama base URL
                litellm.api_base = self.settings.ollama_host
                logger.info(f"🤖 Using Ollama: {self.settings.ollama_model} at {self.settings.ollama_host}")
            elif provider == "auto":
                # Auto-select: cloud APIs first, fallback to Ollama
                if self.settings.anthropic_api_key:
                    self._model = self.settings.anthropic_model
                    litellm.api_key = self.settings.anthropic_api_key
                    logger.info(f"🤖 Auto-selected Anthropic: {self._model}")
                elif self.settings.openai_api_key:
                    self._model = self.settings.openai_model
                    litellm.api_key = self.settings.openai_api_key
                    logger.info(f"🤖 Auto-selected OpenAI: {self._model}")
                else:
                    self._model = f"ollama/{self.settings.ollama_model}"
                    litellm.api_base = self.settings.ollama_host
                    logger.info(f"🤖 Auto-selected Ollama: {self.settings.ollama_model}")
            
            logger.info("=" * 50)
            logger.info("🔧 EXECUTOR: LiteLLM initialized (Direct Ollama Mode)")
            logger.info(" └─ Role: Code execution, file ops, system commands")
            logger.info("=" * 50)
        except ImportError:
            logger.error("❌ LiteLLM not installed. Run: pip install litellm")
            self._model = None
        except Exception as e:
            logger.error(f"❌ Failed to initialize LiteLLM: {e}")
            self._model = None

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        system_message: str | None = None,
    ) -> AsyncIterator[dict]:
        """Run a message through LiteLLM with real-time response streaming.
        Args:
            message: User message to process.
            system_prompt: Dynamic system prompt from AgentContextBuilder.
            history: Recent session history (prepended as summary to prompt).
            system_message: Legacy kwarg, superseded by system_prompt.
        """
        if not self._model:
            yield {"type": "message", "content": "❌ LiteLLM not available."}
            return

        # Semaphore(1) ensures only one session runs at a time
        async with self._semaphore:
            self._stop_flag = False

            # Apply system prompt if provided
            effective_system = system_prompt or system_message
            
            # Build message history for context
            messages = []
            
            # Add system message
            if effective_system:
                messages.append({
                    "role": "system",
                    "content": effective_system
                })
            
            # Add conversation history
            if history:
                for msg in history[-10:]:  # Last 10 messages for context
                    messages.append({
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", "")
                    })
            
            # Add current message
            messages.append({
                "role": "user",
                "content": message
            })

            # Use a queue to stream chunks from the sync thread to the async generator
            chunk_queue: asyncio.Queue = asyncio.Queue()

            def run_sync():
                """Run LiteLLM completion in a thread, push chunks to queue."""
                try:
                    import litellm
                    
                    # Call LiteLLM with streaming disabled for Ollama
                    # This avoids the httpx streaming bug entirely
                    response = litellm.completion(
                        model=self._model,
                        messages=messages,
                        stream=False,  # CRITICAL: Disable streaming to avoid Ollama/httpx bug
                        temperature=0.7,
                        max_tokens=2048,
                    )
                    
                    # Extract the response content
                    if hasattr(response, 'choices') and len(response.choices) > 0:
                        content = response.choices[0].message.content
                        
                        # Chunk the response for UI streaming effect
                        for chunk in self._chunk_text(content, chunk_size=50):
                            if self._stop_flag:
                                break
                            asyncio.run_coroutine_threadsafe(
                                chunk_queue.put({
                                    "type": "message",
                                    "content": chunk
                                }),
                                loop,
                            )
                    else:
                        asyncio.run_coroutine_threadsafe(
                            chunk_queue.put({
                                "type": "error",
                                "content": "No response from model"
                            }),
                            loop,
                        )
                        
                except Exception as e:
                    logger.exception(f"Error during LiteLLM completion: {e}")
                    asyncio.run_coroutine_threadsafe(
                        chunk_queue.put({
                            "type": "error",
                            "content": f"Agent error: {str(e)}"
                        }),
                        loop,
                    )
                finally:
                    # Signal completion
                    asyncio.run_coroutine_threadsafe(chunk_queue.put(None), loop)

            try:
                loop = asyncio.get_event_loop()
                # Start the sync function in a thread
                executor_future = loop.run_in_executor(None, run_sync)

                # Yield chunks as they arrive
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            chunk_queue.get(), timeout=60.0
                        )
                        if chunk is None:  # End signal
                            break
                        yield chunk
                    except TimeoutError:
                        yield {"type": "message", "content": "⏳ Still processing..."}

                # Wait for executor to finish
                await executor_future
            except Exception as e:
                logger.error(f"LiteLLM error: {e}")
                yield {"type": "error", "content": f"❌ Agent error: {str(e)}"}

    def _chunk_text(self, text: str, chunk_size: int = 50) -> list[str]:
        """Split text into small chunks for UI streaming effect.
        
        Args:
            text: Text to chunk
            chunk_size: Target characters per chunk
            
        Returns:
            List of text chunks
        """
        if len(text) <= chunk_size:
            return [text]
        
        chunks = []
        current_chunk = ""
        
        for word in text.split():
            if len(current_chunk) + len(word) + 1 > chunk_size:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = word
            else:
                if current_chunk:
                    current_chunk += " " + word
                else:
                    current_chunk = word
        
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks if chunks else [text]

    async def stop(self) -> None:
        """Stop the agent execution."""
        self._stop_flag = True
