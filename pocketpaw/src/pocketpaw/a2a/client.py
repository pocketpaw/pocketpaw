from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx

from pocketpaw.a2a.models import AgentCard, Task, TaskSendParams


def _handle_response(response: httpx.Response) -> bytes:
    """Check for errors and return the raw response bytes."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"A2A remote agent error {e.response.status_code}: {e.response.text}"
        ) from e
    return response.content


def _check_status(response: httpx.Response) -> None:
    """Status-only check for streaming and void responses. Never reads .content or .text."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"A2A remote agent error {e.response.status_code}") from e


class A2AClient:
    """Asynchronous client for interacting with external A2A agents.

    Can be used as an async context manager to share a single
    ``httpx.AsyncClient`` across multiple calls. This is useful for multi-turn
    workflows where opening a new TCP connection per request adds latency::

        async with A2AClient() as client:
            card = await client.get_agent_card(url)
            task = await client.send_task(url, params)

    When used without the context manager each method opens and closes its
    own connection, which is fine for one-off calls.
    """

    def __init__(self, timeout: float = 120.0, auth_headers: dict[str, str] | None = None) -> None:
        self.timeout = timeout
        self.auth_headers = auth_headers or {}
        self._shared_client: httpx.AsyncClient | None = None
        self._card_cache: dict[str, tuple[float, AgentCard]] = {}
        self._card_cache_ttl = 60.0

    # ------------------------------------------------------------------
    # Async context manager, shared persistent connection
    # ------------------------------------------------------------------

    async def __aenter__(self) -> A2AClient:
        self._shared_client = httpx.AsyncClient(timeout=self.timeout, headers=self.auth_headers)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._shared_client is not None:
            await self._shared_client.aclose()
            self._shared_client = None

    @asynccontextmanager
    async def _get_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the shared client if one exists, otherwise open a temporary one."""
        if self._shared_client is not None:
            yield self._shared_client
        else:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.auth_headers) as client:
                yield client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_agent_card(self, base_url: str) -> AgentCard:
        """Fetch the Agent Card capabilities manifest from a remote agent. Cached with 60s TTL."""
        now = time.monotonic()
        cached = self._card_cache.get(base_url)
        if cached and (now - cached[0]) < self._card_cache_ttl:
            return cached[1]

        url = f"{base_url.rstrip('/')}/.well-known/agent.json"
        async with self._get_client() as client:
            response = await client.get(url)
            content = _handle_response(response)
            card = AgentCard.model_validate_json(content)
            self._card_cache[base_url] = (now, card)
            return card

    async def send_task(self, base_url: str, params: TaskSendParams) -> Task:
        """Submit a task to a remote A2A agent (blocking response).

        Per the A2A spec, the agent may return either a Task or a direct Message.
        If a Message is returned, it is wrapped in a synthetic completed Task.
        """
        url = f"{base_url.rstrip('/')}/a2a/tasks/send"
        async with self._get_client() as client:
            response = await client.post(
                url, json=params.model_dump(mode="json", exclude_none=True)
            )
            content = _handle_response(response)
            data = json.loads(content)

            # If the response has a "status" field, it's a Task
            if "status" in data:
                return Task.model_validate(data)

            # Otherwise treat as a direct Message response
            from pocketpaw.a2a.models import A2AMessage, TaskState, TaskStatus

            message = A2AMessage.model_validate(data)
            return Task(
                id=params.id,
                context_id=params.context_id,
                session_id=params.session_id,
                status=TaskStatus(state=TaskState.COMPLETED, message=message),
                history=[params.message, message],
            )

    async def send_task_stream(
        self, base_url: str, params: TaskSendParams
    ) -> AsyncGenerator[str, None]:
        """Submit a task and yield SSE events from a remote A2A agent."""
        url = f"{base_url.rstrip('/')}/a2a/tasks/send/stream"
        payload = params.model_dump(mode="json", exclude_none=True)
        async with self._get_client() as client:
            async with client.stream("POST", url, json=payload) as response:
                _check_status(response)
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        yield line[5:].strip()

    async def get_task(self, base_url: str, task_id: str) -> Task:
        """Poll the current status of a previously submitted task."""
        url = f"{base_url.rstrip('/')}/a2a/tasks/{task_id}"
        async with self._get_client() as client:
            response = await client.get(url)
            content = _handle_response(response)
            return Task.model_validate_json(content)

    async def cancel_task(self, base_url: str, task_id: str) -> None:
        """Request cancellation of an in-flight task."""
        url = f"{base_url.rstrip('/')}/a2a/tasks/{task_id}/cancel"
        async with self._get_client() as client:
            response = await client.post(url)
            _check_status(response)
