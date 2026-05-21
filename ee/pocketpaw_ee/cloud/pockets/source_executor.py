# source_executor.py — Server-side executor for pocket read-only data sources.
# Created: 2026-05-21 (RFC 04 alpha) — runs the GET "bindings" declared in a
#   pocket's `rippleSpec.sources` against the pocket's single configured
#   backend and returns the JSON results. Read-only (GET) only — write
#   bindings land in RFC 04 Milestone 2.
#
# SSRF BOUNDARY. This module is the ONLY pocket-domain module that makes
# outbound HTTP. Every defense from the locked security review is enforced
# here: strict base-URL re-validation, path-traversal rejection, same-host
# assertion after URL join, DNS rebinding check, no redirect following,
# tight timeouts, a 512 KB response cap, error-message sanitization, and a
# per-pocket rate limit.
#
# IMPORT-LINTER: must NOT import `pocketpaw_ee.cloud.models.*`. The executor
# receives base_url / auth / spec by parameter only — `pockets/service.py`
# owns all Beanie access.

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
import urllib.parse
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from pocketpaw.security.url_validators import _host_is_internal, validate_external_url_strict

logger = logging.getLogger(__name__)

# --- limits / policy --------------------------------------------------------
_PER_SOURCE_TIMEOUT_S = 10.0
_MAX_RESPONSE_BYTES = 524_288  # 512 KB (D11)
_RATE_LIMIT_MAX = 10  # runs per window per pocket (D16)
_RATE_LIMIT_WINDOW_S = 60.0
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)  # D10

# Per-pocket run timestamps for the rate limiter. In-memory is fine for the
# alpha — a single process owns the run endpoint. M3 moves this to a shared
# store when refresh-cost controls land.
_run_log: dict[str, list[float]] = {}

# Default refresh policy for a source that omits ``refresh``.
_DEFAULT_REFRESH: list[Literal["pocket_open", "manual"]] = ["pocket_open"]


class SourceBinding(BaseModel):
    """One read-only data binding parsed from `rippleSpec.sources`.

    Unknown keys on a source entry are ignored — the spec may carry fields
    a later milestone reads. ``method`` is a Literal so only GET is ever
    accepted (write verbs are Milestone 2).
    """

    method: Literal["GET"] = "GET"
    path: str
    bind: str
    refresh: list[Literal["pocket_open", "manual"]] = Field(
        default_factory=lambda: _DEFAULT_REFRESH.copy()
    )


def _normalize_bind(bind: str) -> str:
    """Strip a leading ``state.`` from a bind path.

    ``state.prs`` and ``prs`` both target the ``prs`` key of pocket state.
    """
    return bind[len("state.") :] if bind.startswith("state.") else bind


def _rate_limited(pocket_id: str) -> bool:
    """Return True when ``pocket_id`` has used its run budget for the window.

    Records the call timestamp when it returns False (call permitted).
    """
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_S
    stamps = [t for t in _run_log.get(pocket_id, []) if t >= window_start]
    if len(stamps) >= _RATE_LIMIT_MAX:
        _run_log[pocket_id] = stamps
        return True
    stamps.append(now)
    _run_log[pocket_id] = stamps
    return False


def _strip_query(url: str) -> str:
    """Return ``url`` with query string and fragment removed — safe to log."""
    return urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()


class _SourceError(Exception):
    """Internal: a per-source failure with an already-sanitized message."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _resolve_url(base_url: str, path: str) -> str:
    """Join ``path`` onto ``base_url`` and reject anything that escapes it.

    Implements D7: reject absolute URLs, ``..`` segments, null bytes /
    non-printable chars, and any join whose resulting netloc differs from
    the base. Returns the safe absolute URL.
    """
    if "\x00" in path or any(not ch.isprintable() for ch in path):
        raise _SourceError("source path contains illegal characters", code="bad_path")

    split_path = urllib.parse.urlsplit(path)
    if split_path.scheme or split_path.netloc:
        raise _SourceError("source path must be relative, not an absolute URL", code="bad_path")

    # Percent-decode and reject traversal segments.
    decoded = urllib.parse.unquote(split_path.path)
    if any(seg == ".." for seg in decoded.split("/")):
        raise _SourceError("source path may not contain '..' segments", code="bad_path")

    joined = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if urllib.parse.urlsplit(joined).netloc != urllib.parse.urlsplit(base_url).netloc:
        raise _SourceError("source path resolves to a different host", code="bad_path")
    return joined


async def _assert_host_external(hostname: str) -> None:
    """D8 — resolve ``hostname`` and reject if any IP is internal.

    Guards against DNS rebinding: the base URL may be a public name that
    resolves to a private address. ``getaddrinfo`` runs in a worker thread
    so the event loop is not blocked.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except socket.gaierror as exc:
        raise _SourceError("backend host could not be resolved", code="dns_error") from exc

    for info in infos:
        # info[4] is the sockaddr — (host, port[, flowinfo, scope_id]).
        ip = str(info[4][0])
        # strip a zone id (fe80::1%eth0) before parsing
        ip = ip.split("%", 1)[0]
        if _host_is_internal(ip):
            raise _SourceError("backend host resolves to an internal address", code="bad_host")
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise _SourceError("backend host resolves to an internal address", code="bad_host")


def _auth_headers(auth_type: str, auth_header: str | None, token: str) -> dict[str, str]:
    """Build the request auth header for the configured auth type.

    ``none`` adds no header. Unknown types are treated as ``none`` —
    the DTO Literal already constrains the wire input.
    """
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "api_key":
        return {(auth_header or "X-Api-Key"): token}
    if auth_type == "basic":
        return {"Authorization": f"Basic {token}"}
    return {}


def _select_sources(
    bindings: dict[str, SourceBinding],
    *,
    trigger: str | None,
    only_source: str | None,
) -> dict[str, SourceBinding]:
    """Pick which sources to run.

    ``only_source`` wins (single named source); else if ``trigger`` is set,
    every source whose ``refresh`` list contains it; else all sources.
    """
    if only_source is not None:
        if only_source in bindings:
            return {only_source: bindings[only_source]}
        return {}
    if trigger is not None:
        return {k: b for k, b in bindings.items() if trigger in b.refresh}
    return dict(bindings)


def _parse_bindings(ripple_spec: dict) -> tuple[dict[str, SourceBinding], list[dict]]:
    """Parse ``rippleSpec.sources`` into SourceBinding objects.

    Returns ``(valid_bindings, parse_errors)``. A malformed entry becomes a
    parse error rather than aborting the whole run.
    """
    raw = (ripple_spec or {}).get("sources") or {}
    bindings: dict[str, SourceBinding] = {}
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return bindings, errors
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            errors.append({"source": key, "error": "source entry must be an object"})
            continue
        try:
            bindings[key] = SourceBinding.model_validate(entry)
        except ValidationError:
            errors.append({"source": key, "error": "source entry is malformed"})
    return bindings, errors


async def _run_one(
    *,
    client: httpx.AsyncClient,
    key: str,
    binding: SourceBinding,
    base_url: str,
    headers: dict[str, str],
) -> dict:
    """Fetch a single source. Returns a ``ran`` row; raises ``_SourceError``."""
    url = _resolve_url(base_url, binding.path)
    await _assert_host_external(urllib.parse.urlsplit(url).hostname or "")

    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        # D12 — never propagate raw exception text; log a query-stripped URL.
        logger.warning(
            "source %s: request to %s failed: %s",
            key,
            _strip_query(url),
            type(exc).__name__,
        )
        raise _SourceError("request to backend failed", code="request_failed") from exc

    # D9 — redirects are disabled on the client; treat any 3xx as an error.
    if 300 <= resp.status_code < 400:
        raise _SourceError("backend returned a redirect (not followed)", code="redirect")
    if resp.status_code >= 400:
        raise _SourceError(f"backend returned status {resp.status_code}", code="http_error")

    # D11 — reject oversized bodies; never write partial data.
    body = resp.content
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _SourceError("backend response exceeds the 512 KB limit", code="too_large")

    try:
        value = resp.json()
    except ValueError as exc:
        raise _SourceError("backend response is not valid JSON", code="bad_json") from exc

    return {"source": key, "bind": _normalize_bind(binding.bind), "value": value}


async def run_sources(
    *,
    pocket_id: str,
    ripple_spec: dict,
    base_url: str,
    auth_type: str,
    auth_header: str | None,
    token: str,
    trigger: str | None = None,
    only_source: str | None = None,
) -> dict:
    """Run the pocket's selected read-only sources and return the results.

    The result shape is::

        {"ran": [{"source", "bind", "value"}, ...],
         "errors": [{"source", "error"}, ...]}

    The executor is pure: it fetches and returns. It does NOT persist to the
    Pocket document and does NOT emit ``pocket_mutation`` — hydrated state is
    delivered in the HTTP response body of the calling route.
    """
    # D16 — per-pocket rate limit. On breach, return a source-level error
    # for every selected source without making any call.
    if _rate_limited(pocket_id):
        bindings, parse_errors = _parse_bindings(ripple_spec)
        selected = _select_sources(bindings, trigger=trigger, only_source=only_source)
        return {
            "ran": [],
            "errors": parse_errors
            + [
                {"source": key, "error": "rate limit exceeded", "code": "rate_limited"}
                for key in selected
            ],
        }

    # D6/D15 — re-validate the base URL at call time even though config-time
    # validation already ran. Defense in depth against a tampered row.
    validate_external_url_strict(base_url)

    bindings, parse_errors = _parse_bindings(ripple_spec)
    selected = _select_sources(bindings, trigger=trigger, only_source=only_source)
    headers = _auth_headers(auth_type, auth_header, token)

    ran: list[dict] = []
    errors: list[dict] = list(parse_errors)

    if not selected:
        return {"ran": ran, "errors": errors}

    # D9 — redirects disabled. D10 — tight timeouts.
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=_HTTP_TIMEOUT,
    ) as client:

        async def _guarded(key: str, binding: SourceBinding) -> dict:
            try:
                return await asyncio.wait_for(
                    _run_one(
                        client=client,
                        key=key,
                        binding=binding,
                        base_url=base_url,
                        headers=headers,
                    ),
                    timeout=_PER_SOURCE_TIMEOUT_S,
                )
            except TimeoutError:
                return {
                    "__error__": {
                        "source": key,
                        "error": "source timed out",
                        "code": "timeout",
                    }
                }
            except _SourceError as exc:
                return {"__error__": {"source": key, "error": exc.message, "code": exc.code}}
            except Exception:
                # Catch-all: never let a raw exception escape into the body.
                logger.warning("source %s: unexpected failure", key, exc_info=True)
                return {"__error__": {"source": key, "error": "source failed", "code": "error"}}

        results = await asyncio.gather(
            *(_guarded(key, binding) for key, binding in selected.items())
        )

    for result in results:
        if "__error__" in result:
            errors.append(result["__error__"])
        else:
            ran.append(result)

    return {"ran": ran, "errors": errors}


__all__ = ["run_sources", "SourceBinding"]
