# ee/pocketpaw_ee/cloud/lens/client.py — httpx client for the paw-lens read API.
#
# One call shape: ``request(method, base_url, path, token=..., params=...,
# json=...)`` returns the upstream JSON or raises a CloudError. Error mapping:
#   * timeout / connect / transport error, non-JSON body, other non-2xx
#                                           → 503 ``lens.unavailable``
#   * upstream 401 / 403 (token mismatch)   → 503 ``lens.misconfigured``
#     (our config problem, not the caller's)
#   * upstream 404                          → 404 ``lens.not_found``
#   * upstream 400                          → 400 ``lens.bad_request``
# The ``X-Lens-Token`` value is never logged; log lines carry method, path and
# status only. ``_transport`` is injectable so tests use httpx.MockTransport.

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, NotFound

logger = logging.getLogger(__name__)

LENS_TIMEOUT_SECONDS = 3.0


def _unavailable() -> CloudError:
    return CloudError(503, "lens.unavailable", "Agent health data is unavailable right now.")


class LensClient:
    """Thin async client for paw-lens. Raises CloudError on any failure."""

    def __init__(
        self,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = LENS_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = _transport  # tests inject httpx.MockTransport
        self._timeout = timeout

    async def request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        token: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                base_url=base_url,
                timeout=self._timeout,
                headers={"X-Lens-Token": token},
            ) as client:
                resp = await client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            logger.warning("paw-lens %s %s failed: %s", method, path, type(exc).__name__)
            raise _unavailable() from exc

        status = resp.status_code
        if status in (401, 403):
            logger.error("paw-lens rejected the token (%s) on %s %s", status, method, path)
            raise CloudError(503, "lens.misconfigured", "Agent health is misconfigured.")
        if status == 404:
            raise NotFound("lens")
        if status == 400:
            raise BadRequest("lens.bad_request", "paw-lens rejected the request.")
        if status // 100 != 2:
            logger.warning("paw-lens %s %s returned %s", method, path, status)
            raise _unavailable()
        try:
            return resp.json()
        except ValueError as exc:
            logger.warning("paw-lens %s %s returned non-JSON", method, path)
            raise _unavailable() from exc


__all__ = ["LENS_TIMEOUT_SECONDS", "LensClient"]
