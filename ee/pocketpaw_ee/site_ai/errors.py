# ee/pocketpaw_ee/site_ai/errors.py — the site AI API's error codes and wire shape.
#
# Every refusal on a site-facing AI route is JSON ``{detail: code, message,
# field?, retry_after?}`` with the status below (the pinned contract). Messages are
# fixed, human strings: no provider text, model id or exception detail ever lands
# in one. ``unavailable`` stream reasons are in ``UNAVAILABLE_REASONS``.

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

STATUS: dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "budget_exhausted": 402,
    "wallet_empty": 402,
    "browser_disabled": 403,
    "preset_required": 403,
    "model_not_allowed": 403,
    "origin_not_allowed": 403,
    "turnstile_required": 403,
    "request_too_large": 413,
    "rate_limited": 429,
    "provider_unavailable": 503,
}

MESSAGES: dict[str, str] = {
    "invalid_request": "The request isn't valid.",
    "unauthorized": "This site isn't allowed to call that.",
    "budget_exhausted": "This site's AI budget is used up for now.",
    "wallet_empty": "AI is unavailable for this site right now.",
    "browser_disabled": "AI can't be called from the browser on this site.",
    "preset_required": "Browser calls on this site must name a preset.",
    "model_not_allowed": "That model isn't available for this site.",
    "origin_not_allowed": "This page's address isn't allowed for this site.",
    "turnstile_required": "Please confirm you're a person and try again.",
    "request_too_large": "The request is too large.",
    "rate_limited": "Too many requests. Wait a moment and try again.",
    "provider_unavailable": "The AI service didn't respond. Try again in a moment.",
}

UNAVAILABLE_REASONS = ("limit", "paused", "temporary", "browser_disabled")


class AiError(Exception):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        field: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or MESSAGES[code]
        self.field = field
        self.retry_after = retry_after
        super().__init__(code)

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"detail": self.code, "message": self.message}
        if self.field is not None:
            out["field"] = self.field
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


def invalid(field: str, message: str) -> AiError:
    return AiError("invalid_request", message, field=field)


def error_response(exc: AiError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return JSONResponse(exc.body(), status_code=exc.status, headers=headers)


__all__ = ["MESSAGES", "STATUS", "UNAVAILABLE_REASONS", "AiError", "error_response", "invalid"]
