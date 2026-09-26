# nginx-combined HTTP access-log middleware for ``pocketpaw serve``.
#
# What this does:
#   A raw ASGI middleware (matching the AuthMiddleware pattern in
#   dashboard_auth.py — no BaseHTTPMiddleware, so WebSocket scopes pass through
#   untouched) that, for every HTTP request, appends ONE nginx-combined-format
#   line to ``Settings.access_log_path``. shield's CrowdSec parses that file to
#   detect probing/scanning.
#
#   The backend sits behind Cloudflare → Traefik, so the socket peer is a proxy.
#   The log records the REAL client IP resolved by precedence:
#     CF-Connecting-IP header → leftmost hop of X-Forwarded-For → peer host.
#   Recording the proxy IP would make CrowdSec ban the proxy (self-DoS).
#
#   Robustness: the parent dir is created on first use; any write failure logs a
#   warning ONCE and is swallowed — a logging failure must NEVER 500 a request.
#   The middleware is only added by create_api_app() when access_log_path is set,
#   so default deploys are byte-for-byte unchanged.

import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# nginx "combined" log format:
#   $remote_addr - $remote_user [$time_local] "$request" $status
#   $body_bytes_sent "$http_referer" "$http_user_agent"
# We emit remote_user as "-" (no HTTP basic auth) and use a fixed +0000
# strftime so CrowdSec's nginx parser reads it cleanly.
_LOG_FORMAT = '{ip} - - [{ts}] "{method} {path} HTTP/{ver}" {status} {size} "{referer}" "{ua}"\n'
_TS_FORMAT = "%d/%b/%Y:%H:%M:%S +0000"


def _first_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    """Return the first value for ``name`` (lowercased) from raw ASGI headers."""
    for k, v in headers:
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


def _resolve_client_ip(headers: list[tuple[bytes, bytes]], peer: str) -> str:
    """Resolve the real client IP by proxy-aware precedence.

    Order: ``CF-Connecting-IP`` → leftmost hop of ``X-Forwarded-For`` → peer.
    The deploy sits behind Cloudflare + Traefik, so the forwarded headers are
    trusted here (they'd be strippable/spoofable on a direct-to-internet box).
    """
    cf_ip = _first_header(headers, b"cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()

    xff = _first_header(headers, b"x-forwarded-for")
    if xff and xff.strip():
        # Leftmost entry is the originating client; the rest are proxy hops.
        first_hop = xff.split(",")[0].strip()
        if first_hop:
            return first_hop

    return peer


class AccessLogMiddleware:
    """Pure ASGI middleware that appends nginx-combined access-log lines.

    Only HTTP scopes are logged; WebSocket and lifespan scopes pass straight
    through. Wired into create_api_app() ONLY when ``access_log_path`` is set.
    """

    def __init__(self, app, log_path: str):
        self.app = app
        self.log_path = Path(log_path)
        self._dir_ready = False
        self._warned = False

    def _ensure_dir(self) -> None:
        if self._dir_ready:
            return
        parent = self.log_path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._dir_ready = True

    def _write(self, line: str) -> None:
        """Append one line; swallow (and warn-once on) any I/O failure."""
        try:
            self._ensure_dir()
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "access-log write to %s failed; access logging disabled for "
                    "this process until it recovers",
                    self.log_path,
                    exc_info=True,
                )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # WebSocket / lifespan — never logged.
            await self.app(scope, receive, send)
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        peer = "-"
        client = scope.get("client")
        if client:
            peer = client[0] or "-"

        status_holder = {"status": 0, "size": 0}

        async def send_wrapper(message):
            mtype = message.get("type")
            if mtype == "http.response.start":
                status_holder["status"] = message.get("status", 0)
            elif mtype == "http.response.body":
                body = message.get("body", b"")
                if body:
                    status_holder["size"] += len(body)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                ip = _resolve_client_ip(headers, peer)
                method = scope.get("method", "-")
                raw_path = scope.get("path", "-") or "-"
                qs = scope.get("query_string", b"")
                if qs:
                    raw_path = f"{raw_path}?{qs.decode('latin-1', 'replace')}"
                http_version = scope.get("http_version", "1.1")
                referer = _first_header(headers, b"referer") or "-"
                ua = _first_header(headers, b"user-agent") or "-"
                ts = datetime.now(UTC).strftime(_TS_FORMAT)
                line = _LOG_FORMAT.format(
                    ip=ip,
                    ts=ts,
                    method=method,
                    path=raw_path,
                    ver=http_version,
                    status=status_holder["status"],
                    size=status_holder["size"],
                    referer=referer,
                    ua=ua,
                )
                self._write(line)
            except Exception:
                # Building/writing the log line must never break the request.
                if not self._warned:
                    self._warned = True
                    logger.warning("access-log line build failed", exc_info=True)
