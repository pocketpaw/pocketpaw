import asyncio
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlparse

from pocketpaw.a2a.client import A2AClient
from pocketpaw.a2a.models import A2AMessage, TaskSendParams, TextPart
from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool


class A2ADelegateTool(BaseTool):
    """Tool for delegating tasks to external A2A-compatible agents."""

    @property
    def name(self) -> str:
        return "delegate_to_a2a_agent"

    @property
    def description(self) -> str:
        return (
            "Delegates a task to an external A2A-compatible agent on the network. "
            "Provide the base URL of the agent and a clear description of the task. "
            "Note: This tool blocks while waiting for the remote agent to complete "
            "the task (up to a 120-second timeout)."
        )

    @property
    def trust_level(self) -> str:
        # Elevated because this tool makes outbound HTTP requests to external URLs.
        # Relies on the SSRF check below; still warrants elevated scrutiny.
        return "elevated"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_url": {
                    "type": "string",
                    "description": "The base URL of the external A2A agent (e.g., 'http://localhost:8001').",
                },
                "task": {
                    "type": "string",
                    "description": "The complete instructions or query for the external agent.",
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Optional. The ID of an existing A2A task to continue "
                        "a multi-turn conversation."
                    ),
                },
            },
            "required": ["agent_url", "task"],
        }

    async def execute(self, agent_url: str, task: str, task_id: str | None = None) -> str:
        # Prevent SSRF: validate agent_url scheme and host
        settings = get_settings()
        allowed_agents = [url.rstrip("/") for url in settings.a2a_trusted_agents]
        base_url = agent_url.rstrip("/")

        if base_url not in allowed_agents:
            try:
                parsed = urlparse(agent_url)
                if parsed.scheme not in ("http", "https"):
                    return self._error("Invalid URL scheme. Only HTTP/HTTPS are allowed.")

                hostname = parsed.hostname
                if not hostname:
                    return self._error("Invalid URL hostname.")

                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                loop = asyncio.get_running_loop()
                # Use getaddrinfo to get all resolved IPs (protects against multi-A record bypass)
                addr_infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, port)

                # Check ALL returned IPs and collect the first safe one to pin.
                # Pinning the resolved IP prevents DNS rebinding (TOCTOU) attacks
                # where an attacker flips DNS between this check and the HTTP request.
                pinned_ip = None
                for addr in addr_infos:
                    ip_str = addr[4][0]
                    ip = ipaddress.ip_address(ip_str)

                    if (
                        ip.is_private
                        or ip.is_loopback
                        or ip.is_link_local
                        or ip.is_multicast
                        or ip.is_reserved
                    ):
                        return self._error(
                            f"SSRF Protection: Hostname resolves to local/private IP ({ip_str}), "
                            "denied. Add this URL to the 'a2a_trusted_agents' allowlist in "
                            "settings to permit."
                        )
                    if pinned_ip is None:
                        pinned_ip = ip_str

                if pinned_ip is None:
                    return self._error(f"Could not resolve hostname '{hostname}'.")

                # Rewrite the URL to use the pinned IP, preventing DNS rebinding.
                # Pass the original hostname via the Host header so TLS and
                # virtual-host routing still work.
                pinned_url = agent_url.replace(hostname, pinned_ip, 1)
                pinned_host = hostname

            except socket.gaierror as e:
                return self._error(f"Could not resolve hostname '{hostname}': {e}")
            except Exception as e:
                return self._error(f"URL validation failed: {e}")
        else:
            pinned_url = None
            pinned_host = None

        # Use pinned URL if we resolved DNS ourselves (prevents rebinding).
        # The Host header preserves the original hostname for TLS/virtual-host routing.
        effective_url = pinned_url if pinned_url else agent_url
        auth_headers = {"Host": pinned_host} if pinned_host else {}

        async with A2AClient(auth_headers=auth_headers) as client:
            try:
                # 1. Discover capabilities
                card = await client.get_agent_card(effective_url)
            except Exception as e:
                return self._error(
                    f"Failed to fetch Agent Card from {agent_url}: {e}. "
                    "Ensure the agent is running and supports A2A."
                )

            # 2. Support multi-turn by fetching history if task_id provided
            history_messages: list[A2AMessage] = []
            if task_id:
                try:
                    existing_task = await client.get_task(effective_url, task_id)
                    # Ensure the external agent actually supports state transitions
                    if not card.capabilities.state_transition_history:
                        return self._error(
                            f"Agent at {agent_url} does not support multi-turn task history."
                        )

                    # Preserve the message-level structure so the remote agent can
                    # distinguish its own previous responses from user turns.
                    # Do NOT flatten parts across messages, that loses role info.
                    history_messages = list(existing_task.history)
                except Exception as e:
                    return self._error(
                        f"Failed to retrieve existing task {task_id} from {agent_url}: {e}"
                    )

            # 3. Formulate task parameters
            # The new user turn is sent as a fresh message; prior turns go in history.
            new_message = A2AMessage(role="user", parts=[TextPart(text=task)])

            # If continuing, we MUST send the same task_id
            send_kwargs: dict[str, Any] = {"message": new_message, "history": history_messages}
            if task_id:
                send_kwargs["id"] = task_id

            params = TaskSendParams(**send_kwargs)

            try:
                # 4. Submit task (blocking send for now)
                result_task = await client.send_task(effective_url, params)
            except Exception as e:
                return self._error(f"Failed to submit task to {agent_url}: {e}")

        # Extract final response message
        if not result_task.status.message or not result_task.status.message.parts:
            return self._error("Agent returned no content.")

        agent_reply = " ".join(
            part.text for part in result_task.status.message.parts if part.type == "text"
        )

        status_state = result_task.status.state.value

        # Build response with artifacts if present
        response_data: dict[str, Any] = {
            "agent_name": card.name,
            "task_id": result_task.id,
            "status": status_state,
            "reply": agent_reply,
        }

        if result_task.artifacts:
            response_data["artifacts"] = [
                {
                    "name": artifact.name,
                    "description": artifact.description,
                    "content": " ".join(
                        part.text for part in artifact.parts if hasattr(part, "text")
                    ),
                }
                for artifact in result_task.artifacts
            ]

        return self._success(json.dumps(response_data, indent=2))
