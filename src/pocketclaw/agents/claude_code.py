"""Claude Code agent wrapper using Anthropic's computer use."""

import asyncio
import logging
from typing import AsyncIterator, Optional

from pocketclaw.config import Settings
from pocketclaw.tools.screenshot import take_screenshot

logger = logging.getLogger(__name__)


class ClaudeCodeAgent:
    """Wraps Claude's computer use capability for autonomous task execution."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._stop_flag = False
        self._initialize()

    def _initialize(self) -> None:
        """Initialize the Anthropic client."""
        from pocketclaw.llm.client import resolve_llm_client

        try:
            llm = resolve_llm_client(self.settings, force_provider="anthropic")
            if not llm.api_key:
                logger.warning("⚠️ Claude Code requires Anthropic API key")
                return

            self._client = llm.create_anthropic_client()
            logger.info("✅ Claude Code agent initialized")

        except ImportError:
            logger.error("❌ Anthropic not installed. Run: pip install anthropic")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Claude Code: {e}")

    async def run(self, message: str) -> AsyncIterator[dict]:
        """Run a message through Claude Code."""
        if not self._client:
            yield {
                "type": "message",
                "content": "❌ Claude Code requires Anthropic API key. Add it in ⚙️ Settings.",
            }
            return

        self._stop_flag = False

        try:
            # Get current screenshot for context
            screenshot_bytes = take_screenshot()

            # Build messages with computer use tools
            messages = [{"role": "user", "content": message}]

            # Add screenshot if available
            if screenshot_bytes:
                import base64

                screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Current screen state is attached. User request: {message}",
                            },
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": screenshot_b64,
                                },
                            },
                        ],
                    }
                ]

            # Call Claude with computer use tools
            response = await self._client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=4096,
                system="""You are PocketPaw, an AI agent running on the user's local machine.
You can help the user with tasks by analyzing their screen and providing guidance.
When you need to execute commands, provide them as bash code blocks.
Be concise and helpful.""",
                messages=messages,
                tools=[
                    {
                        "name": "bash",
                        "description": "Run a bash command on the user's machine",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "description": "The bash command to run",
                                }
                            },
                            "required": ["command"],
                        },
                    },
                    {
                        "name": "computer",
                        "description": "Control the computer (take screenshot, click, type)",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["screenshot", "click", "type", "key"],
                                    "description": "The action to perform",
                                },
                                "coordinate": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "X, Y coordinates for click",
                                },
                                "text": {"type": "string", "description": "Text to type"},
                            },
                            "required": ["action"],
                        },
                    },
                ],
            )

            # Process response
            for block in response.content:
                if self._stop_flag:
                    break

                if block.type == "text":
                    yield {"type": "message", "content": block.text}

                elif block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input

                    if tool_name == "bash":
                        command = tool_input.get("command", "")
                        yield {"type": "code", "content": f"$ {command}"}

                        # Execute command
                        result = await self._execute_bash(command)
                        yield {"type": "message", "content": f"```\n{result}\n```"}

                    elif tool_name == "computer":
                        action = tool_input.get("action")
                        if action == "screenshot":
                            yield {"type": "message", "content": "📸 Taking screenshot..."}
                        elif action == "click":
                            coord = tool_input.get("coordinate", [0, 0])
                            yield {
                                "type": "message",
                                "content": f"🖱️ Clicking at ({coord[0]}, {coord[1]})",
                            }
                            await self._click(coord[0], coord[1])
                        elif action == "type":
                            text = tool_input.get("text", "")
                            yield {"type": "message", "content": f"⌨️ Typing: {text[:50]}..."}
                            await self._type_text(text)

        except Exception as e:
            logger.error(f"Claude Code error: {e}")
            yield {"type": "message", "content": f"❌ Agent error: {str(e)}"}

    async def _execute_bash(self, command: str) -> str:
        """Execute a bash command and return output."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            output = stdout.decode() if stdout else ""
            if stderr:
                output += f"\nSTDERR: {stderr.decode()}"

            return output[:2000]  # Limit output size
        except Exception as e:
            return f"Error: {str(e)}"

    async def _click(self, x: int, y: int) -> None:
        """Click at coordinates."""
        try:
            import pyautogui

            pyautogui.click(x, y)
        except Exception as e:
            logger.error(f"Click failed: {e}")

    async def _type_text(self, text: str) -> None:
        """Type text."""
        try:
            import pyautogui

            pyautogui.typewrite(text, interval=0.02)
        except Exception as e:
            logger.error(f"Type failed: {e}")

    async def stop(self) -> None:
        """Stop the agent execution."""
        self._stop_flag = True
