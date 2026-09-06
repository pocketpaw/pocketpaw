# browser.py — in-process MCP server exposing the /browser surface's agentic
# browser (navigate / snapshot / click / type / scroll / screenshot / close) to
# the claude_agent_sdk cloud chat backend.
#
# Created: 2026-09-06 (BR-1, feat/browser-surface-server). Shape is cloned from
# ``media.py``: ``create_sdk_mcp_server`` behind an SDK import-guard,
# ``SERVER_NAME`` + ``mcp__<server>__<tool>`` id constants, the
# ``_error_response`` / ``_success_response`` pair, and identity resolved from
# the per-stream ``current_workspace_id`` / ``current_user_id`` ContextVars.
#
# THREE THINGS THIS FILE IS RESPONSIBLE FOR BEYOND WIRING:
#
#   1. ONE BROWSER PER WORKSPACE. ``session_id = current_workspace_id()``, and a
#      run with no resolvable workspace is REFUSED — never a shared fallback key,
#      which would hand every anonymous caller the same live browser.
#   2. NO CREDENTIALS. ``type`` reads the target element's LIVE attributes
#      (``driver.field_info``) at type time — not at snapshot time, because the
#      DOM can change in between — and refuses password / one-time-code / card
#      fields outright. A trust boundary in code, not a line in a preamble.
#   3. AN AUDIT ROW PER ACTION, success or refusal, via the shared
#      ``_audit.record_tool_call`` (which writes BOTH the runtime SQLite
#      AuditEvent sink and the workspace Mongo audit) — the same helper every
#      other MCP server here uses.
#
# SSRF is NOT enforced here: it lives one layer down in the OSS driver's
# ``context.route`` interceptor, so it covers redirects, subresources and JS
# fetches, not just the URLs this server passes in.
#
# Every tool returns the FRESH snapshot text so the agent always reasons about
# the page as it is now rather than the page it last saw.
#
# 2026-09-06 (BR-4, feat/browser-surface-extract) — two additions:
#   * ``extract`` reads a page as MARKDOWN (html2text over ``page.content()``,
#     the same converter ``tools/builtin/url_extract.py`` already uses). A
#     snapshot is for CLICKING — it carries [ref=N] markers and the structural
#     noise that goes with them — so reading a long article through one costs
#     several times the tokens of the prose itself. Truncation at ``max_chars``
#     is MARKED, so the agent never reasons about half a page believing it saw
#     all of it.
#   * ``screenshot`` now also SAVES the PNG through the studio media storage
#     (``cloud.media.storage.save_generated``) and returns its
#     ``/api/v1/media/<name>`` URL alongside the image block, so a capture can
#     go into a Ripple image widget instead of only being looked at. The file
#     name carries a per-workspace owner token (``capture_name_prefix``) and the
#     serve route refuses a capture belonging to another tenant.

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from pocketpaw.security.safe_fetch import SafeFetchError

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_browser"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist / denylist entries must use this exact form.
NAVIGATE_TOOL_ID = f"mcp__{SERVER_NAME}__navigate"
SNAPSHOT_TOOL_ID = f"mcp__{SERVER_NAME}__snapshot"
CLICK_TOOL_ID = f"mcp__{SERVER_NAME}__click"
TYPE_TOOL_ID = f"mcp__{SERVER_NAME}__type"
SCROLL_TOOL_ID = f"mcp__{SERVER_NAME}__scroll"
SCREENSHOT_TOOL_ID = f"mcp__{SERVER_NAME}__screenshot"
EXTRACT_TOOL_ID = f"mcp__{SERVER_NAME}__extract"
CLOSE_TOOL_ID = f"mcp__{SERVER_NAME}__close"

BROWSER_TOOL_IDS = (
    NAVIGATE_TOOL_ID,
    SNAPSHOT_TOOL_ID,
    CLICK_TOOL_ID,
    TYPE_TOOL_ID,
    SCROLL_TOOL_ID,
    SCREENSHOT_TOOL_ID,
    EXTRACT_TOOL_ID,
    CLOSE_TOOL_ID,
)

# `extract` defaults. The floor stops a caller asking for a useless sliver; the
# ceiling stops one asking for a whole context window.
DEFAULT_EXTRACT_CHARS = 12_000
MIN_EXTRACT_CHARS = 500
MAX_EXTRACT_CHARS = 200_000

# --- Credential refusal -------------------------------------------------------
# The agent may fill a search box or a shipping address. It may NOT fill a
# password, a 2FA code, or a card number — those are the user's to enter, and a
# prompt-injected page that talks the agent into typing one is the whole reason
# this check is in code rather than in a preamble.
_CREDENTIAL_AUTOCOMPLETE = frozenset(
    {
        "current-password",
        "new-password",
        "one-time-code",
        "cc-number",
        "cc-csc",
        "cc-exp",
    }
)
_CREDENTIAL_NAME_RE = re.compile(
    r"(pass(wd|word)?|passcode|otp|mfa|2fa|totp|cvv|cvc|card[-_]?number|secret|api[-_]?key|token)",
    re.IGNORECASE,
)
CREDENTIAL_REFUSAL = (
    "Refused: that field takes a credential (password / one-time code / card "
    "detail). I never type credentials into a page. Ask the user to enter it "
    "themselves, or to import their saved logins from settings."
)


def _is_credential_field(info: dict[str, str]) -> bool:
    """True when the element is a credential input the agent must not fill."""
    if (info.get("type") or "").lower() == "password":
        return True
    if (info.get("autocomplete") or "").strip().lower() in _CREDENTIAL_AUTOCOMPLETE:
        return True
    return bool(_CREDENTIAL_NAME_RE.search(f"{info.get('name', '')} {info.get('id', '')}"))


# --- MCP response helpers (same shape as media.py) -----------------------------


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


def _safe_url(url: str | None) -> str | None:
    """Drop the query string and fragment before a URL reaches the audit log.

    ``record_tool_call``'s contract is "never PII/tokens", and a magic-link or
    session URL carries exactly that in ``?token=…``. Path and host are what an
    auditor needs; the query is what leaks.
    """
    if not url:
        return url
    from urllib.parse import urlsplit

    return urlsplit(url)._replace(query="", fragment="").geturl()


def _audit(
    workspace_id: str,
    user_id: str | None,
    tool_name: str,
    *,
    ok: bool,
    **metadata: Any,
) -> None:
    if "url" in metadata:
        metadata["url"] = _safe_url(metadata["url"])
    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server=SERVER_NAME,
        tool_name=tool_name,
        status="ok" if ok else "error",
        ok=ok,
        metadata={k: v for k, v in metadata.items() if v is not None},
    )


async def _driver(workspace_id: str):  # type: ignore[no-untyped-def]
    """The workspace's browser driver — one per workspace, created on demand."""
    from pocketpaw.browser import get_browser_session_manager

    session = await get_browser_session_manager().get_or_create(workspace_id)
    return session.driver


# --- Tool handlers -------------------------------------------------------------


async def _navigate_handler(args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")

    url = (args.get("url") or "").strip()
    if not url:
        _audit(workspace_id, user_id, "navigate", ok=False, reason="missing_url")
        return _error_response("`url` is required.")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        driver = await _driver(workspace_id)
        result = await driver.navigate(url)
    except SafeFetchError:
        # The driver translates its own SSRF abort into this; a raw Chromium
        # net::ERR_FAILED means something else went wrong.
        reason = (
            "blocked: that address is not publicly reachable (private, loopback, "
            "link-local, or cloud-metadata)."
        )
        _audit(workspace_id, user_id, "navigate", ok=False, url=url, reason=reason)
        return _error_response(reason)
    except Exception as exc:  # noqa: BLE001
        reason = f"navigation failed: {exc}"
        _audit(workspace_id, user_id, "navigate", ok=False, url=url, reason=reason[:200])
        return _error_response(reason)

    _audit(workspace_id, user_id, "navigate", ok=True, url=driver.current_url)
    return _success_response({"ok": True, "url": driver.current_url, "snapshot": result.snapshot})


async def _snapshot_handler(_args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")
    try:
        driver = await _driver(workspace_id)
        result = await driver.snapshot()
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "snapshot", ok=False, reason=str(exc)[:200])
        return _error_response(f"snapshot failed: {exc}")

    _audit(workspace_id, user_id, "snapshot", ok=True, url=driver.current_url)
    return _success_response({"ok": True, "url": driver.current_url, "snapshot": result.snapshot})


async def _click_handler(args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")
    ref = args.get("ref")
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        _audit(workspace_id, user_id, "click", ok=False, reason="bad_ref")
        return _error_response("`ref` must be an integer from the latest snapshot.")

    try:
        driver = await _driver(workspace_id)
        result = await driver.click(ref)
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "click", ok=False, ref=ref, reason=str(exc)[:200])
        return _error_response(f"click failed: {exc}")

    _audit(workspace_id, user_id, "click", ok=True, ref=ref, url=driver.current_url)
    return _success_response({"ok": True, "url": driver.current_url, "snapshot": result.snapshot})


async def _type_handler(args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")
    ref = args.get("ref")
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        _audit(workspace_id, user_id, "type", ok=False, reason="bad_ref")
        return _error_response("`ref` must be an integer from the latest snapshot.")
    text = args.get("text")
    if not isinstance(text, str):
        _audit(workspace_id, user_id, "type", ok=False, ref=ref, reason="missing_text")
        return _error_response("`text` is required.")

    try:
        driver = await _driver(workspace_id)
        info = await driver.field_info(ref)
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "type", ok=False, ref=ref, reason=str(exc)[:200])
        return _error_response(f"type failed: {exc}")

    # HARD BOUNDARY — checked against the LIVE element, and before any keystroke.
    if _is_credential_field(info):
        _audit(workspace_id, user_id, "type", ok=False, ref=ref, reason="credential_field_refused")
        return _error_response(CREDENTIAL_REFUSAL)

    try:
        await driver.type_text(ref, text)
        result = await driver.snapshot()
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "type", ok=False, ref=ref, reason=str(exc)[:200])
        return _error_response(f"type failed: {exc}")

    # The typed VALUE is never audited — it is user content, not metadata.
    _audit(workspace_id, user_id, "type", ok=True, ref=ref, url=driver.current_url)
    return _success_response({"ok": True, "url": driver.current_url, "snapshot": result.snapshot})


async def _scroll_handler(args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")
    direction = (args.get("direction") or "down").strip().lower()
    if direction not in ("up", "down"):
        _audit(workspace_id, user_id, "scroll", ok=False, reason="bad_direction")
        return _error_response("`direction` must be 'up' or 'down'.")

    try:
        driver = await _driver(workspace_id)
        result = await driver.scroll(direction)
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "scroll", ok=False, reason=str(exc)[:200])
        return _error_response(f"scroll failed: {exc}")

    _audit(workspace_id, user_id, "scroll", ok=True, direction=direction, url=driver.current_url)
    return _success_response({"ok": True, "url": driver.current_url, "snapshot": result.snapshot})


def _to_markdown(html: str) -> str:
    """HTML → markdown, tuned so the result is CHEAPER than a snapshot.

    Same converter ``tools/builtin/url_extract.py`` uses, with two deliberate
    differences: links lose their URLs (the link TEXT survives, and a page of
    nav chrome is mostly URLs by weight) and images are dropped entirely.
    ``body_width = 0`` stops html2text hard-wrapping prose at 78 columns, which
    would spend tokens on newlines the agent has no use for.
    """
    import html2text

    converter = html2text.HTML2Text()
    converter.ignore_links = True
    converter.ignore_images = True
    converter.body_width = 0
    return converter.handle(html).strip()


async def _extract_handler(args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")

    try:
        max_chars = int(args.get("max_chars") or DEFAULT_EXTRACT_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_EXTRACT_CHARS
    max_chars = max(MIN_EXTRACT_CHARS, min(max_chars, MAX_EXTRACT_CHARS))

    try:
        driver = await _driver(workspace_id)
        html = await driver.content_html()
        text = _to_markdown(html)
    except ImportError:
        reason = "extract needs html2text (install pocketpaw[extract])."
        _audit(workspace_id, user_id, "extract", ok=False, reason=reason)
        return _error_response(reason)
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "extract", ok=False, reason=str(exc)[:200])
        return _error_response(f"extract failed: {exc}")

    total = len(text)
    truncated = total > max_chars
    if truncated:
        # MARKED, not silent — an agent that reasons about half a page believing
        # it read all of it answers confidently and wrongly.
        text = (
            f"{text[:max_chars]}\n\n"
            f"[truncated: {max_chars} of {total} characters. Call extract again "
            f"with a larger `max_chars` to read the rest.]"
        )

    _audit(
        workspace_id,
        user_id,
        "extract",
        ok=True,
        url=driver.current_url,
        chars=total,
        truncated=truncated,
    )
    return _success_response(
        {
            "ok": True,
            "url": driver.current_url,
            "chars": total,
            "truncated": truncated,
            "markdown": text,
        }
    )


async def _screenshot_handler(_args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")
    try:
        driver = await _driver(workspace_id)
        png = await driver.screenshot_png()
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "screenshot", ok=False, reason=str(exc)[:200])
        return _error_response(f"screenshot failed: {exc}")

    # Saved through the SAME storage + serve route studio generations use, under
    # a name that carries this workspace's owner token — so the capture has a URL
    # a Ripple image widget can render, and another tenant's request for it 404s.
    try:
        from pocketpaw_ee.cloud.media import storage as media_storage

        media_url = await media_storage.save_generated(
            png, mime="image/png", name_prefix=media_storage.capture_name_prefix(workspace_id)
        )
    except Exception:  # noqa: BLE001 — the image itself is still useful
        logger.warning("browser: saving the screenshot failed", exc_info=True)
        media_url = None

    _audit(
        workspace_id,
        user_id,
        "screenshot",
        ok=True,
        url=driver.current_url,
        bytes=len(png),
        saved=media_url is not None,
    )
    # The bytes go back for the agent to LOOK at; the URL is what it can hand to
    # an image widget. Never a file in the server's cwd on a shared box.
    note = (
        f"Screenshot of {driver.current_url} — image URL: {media_url}"
        if media_url
        else f"Screenshot of {driver.current_url} (could not be saved, so it has no URL)"
    )
    return {
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(png).decode("ascii"),
                "mimeType": "image/png",
            },
            {"type": "text", "text": note},
        ]
    }


async def _close_handler(_args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id:
        return _error_response("No workspace context; the browser is unavailable on this run.")
    try:
        from pocketpaw.browser import get_browser_session_manager

        await get_browser_session_manager().close_session(workspace_id)
    except Exception as exc:  # noqa: BLE001
        _audit(workspace_id, user_id, "close", ok=False, reason=str(exc)[:200])
        return _error_response(f"close failed: {exc}")

    _audit(workspace_id, user_id, "close", ok=True)
    return _success_response({"ok": True, "closed": True})


def build_browser_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the /browser surface, or return
    ``None`` if the Claude Agent SDK isn't installed.

    Matches the ``(name, server) | None`` shape of ``build_media_server`` so the
    backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_browser MCP disabled")
        return None

    @tool(
        "navigate",
        (
            "Open a URL in the workspace's browser and return the page snapshot. "
            "Use this on the BROWSER surface to start or redirect a browsing task. "
            "Args: `url` (required). Returns {ok, url, snapshot} where `snapshot` "
            "lists the visible page with [ref=N] markers on every clickable / "
            "typeable element — pass those refs to `click` and `type`. Addresses "
            "that resolve to private, loopback, link-local or cloud-metadata IPs "
            "are blocked and return an error; relay it, do not retry."
        ),
        {
            "type": "object",
            "properties": {"url": {"type": "string", "minLength": 1}},
            "required": ["url"],
            "additionalProperties": False,
        },
    )
    async def navigate(args):  # type: ignore[no-untyped-def]
        return await _navigate_handler(args)

    @tool(
        "snapshot",
        (
            "Re-read the current page and return a fresh snapshot with [ref=N] "
            "markers. Use it after the page changes on its own (a JS update, a "
            "late-loading list). Refs are re-numbered every snapshot — always act "
            "on refs from the LATEST one. Returns {ok, url, snapshot}."
        ),
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def snapshot(args):  # type: ignore[no-untyped-def]
        return await _snapshot_handler(args)

    @tool(
        "click",
        (
            "Click an element by its [ref=N] from the latest snapshot, then return "
            "the resulting page. Args: `ref` (required, integer). Returns {ok, url, "
            "snapshot}."
        ),
        {
            "type": "object",
            "properties": {"ref": {"type": "integer"}},
            "required": ["ref"],
            "additionalProperties": False,
        },
    )
    async def click(args):  # type: ignore[no-untyped-def]
        return await _click_handler(args)

    @tool(
        "type",
        (
            "Type text into an element by its [ref=N] from the latest snapshot "
            "(replaces any existing content), then return the resulting page. Args: "
            "`ref` (required, integer), `text` (required). CREDENTIAL FIELDS ARE "
            "REFUSED — passwords, one-time codes and card details come back as an "
            "error; when that happens, tell the user to enter it themselves or to "
            "import their saved logins in settings. Returns {ok, url, snapshot}."
        ),
        {
            "type": "object",
            "properties": {"ref": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["ref", "text"],
            "additionalProperties": False,
        },
    )
    async def type_(args):  # type: ignore[no-untyped-def]
        return await _type_handler(args)

    @tool(
        "scroll",
        (
            "Scroll the page one viewport-ish step and return the resulting "
            "snapshot. Args: `direction` ('up' or 'down', default 'down'). Use it "
            "when the content you need is below the fold. Returns {ok, url, snapshot}."
        ),
        {
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
            "additionalProperties": False,
        },
    )
    async def scroll(args):  # type: ignore[no-untyped-def]
        return await _scroll_handler(args)

    @tool(
        "extract",
        (
            "Read the current page as markdown. Use this whenever you need the "
            "page's CONTENT — an article, docs, a long answer — because it is far "
            "cheaper than a snapshot, which exists for CLICKING and carries "
            "[ref=N] markers, nav chrome and link URLs you do not need to read. "
            "Args: `max_chars` (default 12000). Returns {ok, url, chars, "
            "truncated, markdown}; when `truncated` is true the text ends with a "
            "marker — call again with a larger `max_chars` for the rest, and "
            "never assume you saw the whole page without checking that flag."
        ),
        {
            "type": "object",
            "properties": {"max_chars": {"type": "integer", "minimum": 500}},
            "additionalProperties": False,
        },
    )
    async def extract(args):  # type: ignore[no-untyped-def]
        return await _extract_handler(args)

    @tool(
        "screenshot",
        (
            "Capture the current page as a PNG image. Use it when the LOOK of the "
            "page matters (a chart, a layout, a visual check) — to READ a page use "
            "`extract`, to find controls use `snapshot`. The image comes back for "
            "you to look at AND is saved, so the text block carries an image URL "
            "you may put in a Ripple image widget. Use that URL verbatim; never "
            "invent one."
        ),
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def screenshot(args):  # type: ignore[no-untyped-def]
        return await _screenshot_handler(args)

    @tool(
        "close",
        (
            "Close the workspace's browser and release it. Call this when the "
            "browsing task is finished. A later `navigate` opens a fresh one."
        ),
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def close(args):  # type: ignore[no-untyped-def]
        return await _close_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[navigate, snapshot, extract, click, type_, scroll, screenshot, close],
    )
    return SERVER_NAME, server


__all__ = [
    "BROWSER_TOOL_IDS",
    "CLICK_TOOL_ID",
    "CLOSE_TOOL_ID",
    "DEFAULT_EXTRACT_CHARS",
    "EXTRACT_TOOL_ID",
    "CREDENTIAL_REFUSAL",
    "NAVIGATE_TOOL_ID",
    "SCREENSHOT_TOOL_ID",
    "SCROLL_TOOL_ID",
    "SERVER_NAME",
    "SNAPSHOT_TOOL_ID",
    "TYPE_TOOL_ID",
    "build_browser_server",
]
