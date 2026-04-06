# A2A Protocol — JSON-RPC 2.0 error handling.
#
# Standard JSON-RPC error codes plus A2A-specific extensions.
# See: https://www.jsonrpc.org/specification#error_object

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 standard error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# ---------------------------------------------------------------------------
# A2A-specific error codes
# ---------------------------------------------------------------------------
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
TASK_NOT_MODIFIABLE = -32003
UNSUPPORTED_OPERATION = -32004
INCOMPATIBLE_OUTPUT_MODES = -32005


class JSONRPCError(Exception):
    """JSON-RPC 2.0 error with structured payload."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_response(self, request_id: int | str | None) -> dict[str, Any]:
        """Serialize to a JSON-RPC 2.0 error response envelope."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        }


def json_rpc_error_response(
    request_id: int | str | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response dict."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }


def json_rpc_success_response(
    request_id: int | str | None,
    result: Any,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response dict."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }
