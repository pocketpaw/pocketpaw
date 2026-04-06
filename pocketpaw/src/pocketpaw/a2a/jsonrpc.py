# A2A Protocol — JSON-RPC 2.0 dispatcher.
#
# Parses incoming JSON-RPC requests, validates the envelope, and dispatches
# to the appropriate handler by method name.

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.a2a.errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JSONRPCError,
    json_rpc_error_response,
    json_rpc_success_response,
)

logger = logging.getLogger(__name__)

# Type alias for async handler: (params, request_id) -> result
HandlerFn = Any  # Callable[[dict, int|str|None], Awaitable[Any]]
StreamHandlerFn = Any  # Callable[[dict, int|str|None], AsyncGenerator]


class A2ADispatcher:
    """JSON-RPC 2.0 method dispatcher for A2A protocol."""

    def __init__(self) -> None:
        self._methods: dict[str, HandlerFn] = {}
        self._stream_methods: dict[str, StreamHandlerFn] = {}

    def register(self, method: str, handler: HandlerFn) -> None:
        """Register a handler for a JSON-RPC method."""
        self._methods[method] = handler

    def register_stream(self, method: str, handler: StreamHandlerFn) -> None:
        """Register a streaming handler for a JSON-RPC method."""
        self._stream_methods[method] = handler

    def _validate_envelope(self, obj: dict[str, Any]) -> tuple[str, dict, int | str | None]:
        """Validate JSON-RPC 2.0 envelope. Returns (method, params, id)."""
        if not isinstance(obj, dict):
            raise JSONRPCError(INVALID_REQUEST, "Request must be a JSON object")
        if obj.get("jsonrpc") != "2.0":
            raise JSONRPCError(INVALID_REQUEST, 'Missing or invalid "jsonrpc" field')
        method = obj.get("method")
        if not method or not isinstance(method, str):
            raise JSONRPCError(INVALID_REQUEST, 'Missing or invalid "method" field')
        params = obj.get("params", {})
        if not isinstance(params, dict):
            raise JSONRPCError(INVALID_PARAMS, '"params" must be an object')
        request_id = obj.get("id")
        return method, params, request_id

    async def dispatch(self, raw_body: bytes) -> dict | list:
        """Parse and dispatch a JSON-RPC request (or batch).

        Returns a JSON-RPC response dict (or list for batch).
        """
        # Parse JSON
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as exc:
            return json_rpc_error_response(None, PARSE_ERROR, f"Parse error: {exc}")

        # Batch request
        if isinstance(parsed, list):
            if not parsed:
                return json_rpc_error_response(None, INVALID_REQUEST, "Empty batch")
            results = []
            for item in parsed:
                result = await self._dispatch_single(item)
                if result is not None:  # notifications (no id) return None
                    results.append(result)
            return (
                results
                if results
                else json_rpc_error_response(
                    None, INVALID_REQUEST, "All requests were notifications"
                )
            )

        return await self._dispatch_single(parsed)

    async def _dispatch_single(self, obj: Any) -> dict[str, Any] | None:
        """Dispatch a single JSON-RPC request object."""
        request_id = None
        try:
            method, params, request_id = self._validate_envelope(obj)

            if method in self._methods:
                result = await self._methods[method](params, request_id)
                return json_rpc_success_response(request_id, result)

            if method in self._stream_methods:
                # For non-streaming dispatch of a stream method, fall through to error
                # (streaming is handled separately via dispatch_stream)
                raise JSONRPCError(
                    INVALID_REQUEST,
                    f"Method '{method}' requires streaming. Use the SSE endpoint.",
                )

            raise JSONRPCError(METHOD_NOT_FOUND, f"Method not found: {method}")

        except JSONRPCError as exc:
            return exc.to_response(request_id)
        except Exception as exc:
            logger.exception("Internal error in JSON-RPC dispatch")
            return json_rpc_error_response(request_id, INTERNAL_ERROR, f"Internal error: {exc}")

    async def dispatch_stream(self, raw_body: bytes):
        """Parse a JSON-RPC request and return an async generator for SSE streaming.

        Yields JSON-RPC response envelope dicts.
        """
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as exc:
            yield json_rpc_error_response(None, PARSE_ERROR, f"Parse error: {exc}")
            return

        request_id = None
        try:
            method, params, request_id = self._validate_envelope(parsed)

            if method in self._stream_methods:
                async for event in self._stream_methods[method](params, request_id):
                    yield event
                return

            if method in self._methods:
                # Non-streaming method called on stream endpoint: execute and wrap
                result = await self._methods[method](params, request_id)
                yield json_rpc_success_response(request_id, result)
                return

            raise JSONRPCError(METHOD_NOT_FOUND, f"Method not found: {method}")

        except JSONRPCError as exc:
            yield exc.to_response(request_id)
        except Exception as exc:
            logger.exception("Internal error in JSON-RPC stream dispatch")
            yield json_rpc_error_response(request_id, INTERNAL_ERROR, f"Internal error: {exc}")
