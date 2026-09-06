# profile.py — per-workspace browser profile directory + imported storage state.
# Created: 2026-09-06 (BR-5, feat/browser-surface-profile).
#
# The agent is forbidden from typing passwords (enforced in the EE browser MCP
# server's ``type`` tool), and there is no live browser view for the user to log
# in through. So logged-in portals are reached the only safe way left: the user
# exports their OWN already-authenticated session and imports it once. The agent
# never sees a password — only a session that is already authenticated.
#
# WHAT LIVES HERE, AND WHY IT IS IN OSS RATHER THAN EE
#   * the profile PATH (``~/.pocketpaw/browser-profiles/<workspace>``, 0700) —
#     ``session.py`` needs it to launch a persistent Chromium context, and the EE
#     import route needs it to write into. One implementation, no drift.
#   * VALIDATION of the imported blob. It runs before anything is written, so a
#     hostile or malformed file never reaches disk.
#
# THE FILE IS A CREDENTIAL. ``read_state`` is the only reader and it exists for
# ``session.py`` alone. ``summarize`` is what any HTTP surface is allowed to
# return: counts, domains, a timestamp — never a cookie value. Nothing in this
# module logs the state, and validation errors name the field and index but
# never the value.
"""Per-workspace browser profile directory and imported storage state."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# A storage state is a handful of cookies plus a little localStorage. Anything
# larger is either a mistake or an attempt to fill the disk.
MAX_STATE_BYTES = 512 * 1024
MAX_COOKIES = 500
MAX_ORIGINS = 100
MAX_LOCAL_STORAGE_ITEMS = 500

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Playwright's cookie fields. Everything else in an export is dropped rather
# than passed through to ``context.add_cookies``.
_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")

# Browser extensions (Cookie-Editor, EditThisCookie) spell sameSite their own
# way. ``unspecified`` has no Playwright equivalent, so it is dropped.
_SAME_SITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
}


class InvalidStorageState(ValueError):
    """The imported blob is not a usable storage state. Message names the field."""


def safe_workspace_id(workspace_id: str) -> str:
    """Return ``workspace_id`` if it is safe as a directory name, else raise.

    A workspace id becomes a path segment, so this is a traversal guard, not a
    formatting preference.
    """
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.match(workspace_id):
        raise InvalidStorageState("workspace id is not a valid profile name")
    if workspace_id in {".", ".."}:
        raise InvalidStorageState("workspace id is not a valid profile name")
    return workspace_id


def profiles_root() -> Path:
    """The directory holding every workspace's browser profile (0700)."""
    from pocketpaw.config import get_config_dir

    root = get_config_dir() / "browser-profiles"
    root.mkdir(mode=0o700, exist_ok=True)
    _chmod(root, 0o700)
    return root


def profile_dir(workspace_id: str) -> Path:
    """This workspace's persistent Chromium profile directory (0700)."""
    path = profiles_root() / safe_workspace_id(workspace_id)
    path.mkdir(mode=0o700, exist_ok=True)
    _chmod(path, 0o700)
    return path


def state_path(workspace_id: str) -> Path:
    """Where this workspace's imported storage state is written."""
    return profile_dir(workspace_id) / "storage_state.json"


def _chmod(path: Path, mode: int) -> None:
    # mkdir's ``mode`` is masked by umask, so set it explicitly afterwards too.
    try:
        path.chmod(mode)
    except OSError:
        pass  # Windows / exotic filesystems — the path still works.


# --- Validation ---------------------------------------------------------------


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidStorageState(f"{field} must be a non-empty string")
    if len(value) > 8192:
        raise InvalidStorageState(f"{field} is too long")
    return value


def _clean_domain(raw: Any, field: str) -> str:
    """Reject a cookie domain that is not registrable.

    A cookie with ``domain=".com"`` would be offered to every ``.com`` site the
    browser visits. Requiring at least two labels stops the bare-TLD case.
    There is no Public Suffix List here (no new dependency), so a multi-label
    public suffix such as ``.co.uk`` still passes — named as a known gap.
    """
    domain = _require_str(raw, field).lstrip(".").lower()
    if len(domain.split(".")) < 2 or domain.startswith(".") or domain.endswith("."):
        raise InvalidStorageState(f"{field} is not a registrable domain")
    if any(c in domain for c in " /\\?#@:"):
        raise InvalidStorageState(f"{field} is not a valid domain")
    return domain


def _clean_cookie(raw: Any, index: int) -> dict[str, Any]:
    field = f"cookies[{index}]"
    if not isinstance(raw, dict):
        raise InvalidStorageState(f"{field} must be an object")

    cookie: dict[str, Any] = {
        "name": _require_str(raw.get("name"), f"{field}.name"),
        "value": raw.get("value") if isinstance(raw.get("value"), str) else None,
    }
    if cookie["value"] is None:
        raise InvalidStorageState(f"{field}.value must be a string")

    # Playwright accepts either url, or domain+path. Extensions emit the latter.
    url = raw.get("url")
    if isinstance(url, str) and url:
        if not url.startswith(("http://", "https://")):
            raise InvalidStorageState(f"{field}.url must be http(s)")
        cookie["url"] = url
    else:
        cookie["domain"] = _clean_domain(raw.get("domain"), f"{field}.domain")
        path = raw.get("path") if isinstance(raw.get("path"), str) else "/"
        cookie["path"] = path if path.startswith("/") else "/"

    # ``expirationDate`` is the extension spelling of Playwright's ``expires``.
    expires = raw.get("expires", raw.get("expirationDate"))
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        cookie["expires"] = float(expires)

    for flag in ("httpOnly", "secure"):
        if isinstance(raw.get(flag), bool):
            cookie[flag] = raw[flag]

    same_site = raw.get("sameSite")
    if isinstance(same_site, str):
        mapped = _SAME_SITE.get(same_site.strip().lower())
        if mapped is not None:
            cookie["sameSite"] = mapped

    return {k: v for k, v in cookie.items() if k in (*_COOKIE_FIELDS, "url")}


def _clean_origin(raw: Any, index: int) -> dict[str, Any]:
    field = f"origins[{index}]"
    if not isinstance(raw, dict):
        raise InvalidStorageState(f"{field} must be an object")
    origin = _require_str(raw.get("origin"), f"{field}.origin")
    if not origin.startswith(("http://", "https://")):
        raise InvalidStorageState(f"{field}.origin must be http(s)")
    items_raw = raw.get("localStorage") or []
    if not isinstance(items_raw, list):
        raise InvalidStorageState(f"{field}.localStorage must be an array")
    if len(items_raw) > MAX_LOCAL_STORAGE_ITEMS:
        raise InvalidStorageState(f"{field}.localStorage has too many entries")
    items = []
    for i, item in enumerate(items_raw):
        if not isinstance(item, dict):
            raise InvalidStorageState(f"{field}.localStorage[{i}] must be an object")
        items.append(
            {
                "name": _require_str(item.get("name"), f"{field}.localStorage[{i}].name"),
                "value": item.get("value") if isinstance(item.get("value"), str) else "",
            }
        )
    return {"origin": origin, "localStorage": items}


def validate_storage_state(raw: Any) -> dict[str, Any]:
    """Normalize an imported blob into a Playwright storage state, or raise.

    Accepts a Playwright ``storage_state`` object (``cookies`` + optional
    ``origins``) or a bare cookie-export array. Unknown keys are dropped rather
    than trusted.
    """
    if isinstance(raw, list):
        raw = {"cookies": raw}
    if not isinstance(raw, dict):
        raise InvalidStorageState("expected a storage state object or a cookie array")

    cookies_raw = raw.get("cookies")
    if not isinstance(cookies_raw, list):
        raise InvalidStorageState("cookies must be an array")
    if not cookies_raw:
        raise InvalidStorageState("cookies is empty — nothing to import")
    if len(cookies_raw) > MAX_COOKIES:
        raise InvalidStorageState(f"too many cookies (max {MAX_COOKIES})")

    origins_raw = raw.get("origins") or []
    if not isinstance(origins_raw, list):
        raise InvalidStorageState("origins must be an array")
    if len(origins_raw) > MAX_ORIGINS:
        raise InvalidStorageState(f"too many origins (max {MAX_ORIGINS})")

    return {
        "cookies": [_clean_cookie(c, i) for i, c in enumerate(cookies_raw)],
        "origins": [_clean_origin(o, i) for i, o in enumerate(origins_raw)],
    }


# --- Read / write / delete ----------------------------------------------------


def write_state(workspace_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Write a validated storage state 0600 and return its public summary.

    Written to a temp file opened at 0600 and then renamed, so the state is
    never briefly readable at the process umask.
    """
    path = state_path(workspace_id)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(state).encode()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _chmod(path, 0o600)
    return summarize(workspace_id) or {}


def read_state(workspace_id: str) -> dict[str, Any] | None:
    """The imported storage state, or None. ONLY ``session.py`` may call this.

    The return value is a credential. It must never be returned by an HTTP
    route, logged, audited, or handed to the agent.
    """
    path = state_path(workspace_id)
    if not path.exists():
        return None
    try:
        return validate_storage_state(json.loads(path.read_text()))
    except (OSError, ValueError):
        # A corrupt file must not brick the browser — launch without it.
        return None


def summarize(workspace_id: str) -> dict[str, Any] | None:
    """Counts, domains and an import timestamp — NEVER a cookie value.

    This is the ONLY shape any HTTP surface returns for imported state.
    """
    path = state_path(workspace_id)
    if not path.exists():
        return None
    try:
        state = validate_storage_state(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None
    domains = sorted(
        {
            (c["domain"].lstrip(".") if c.get("domain") else urlparse(c["url"]).netloc)
            for c in state["cookies"]
            if c.get("domain") or c.get("url")
        }
    )
    return {
        "cookie_count": len(state["cookies"]),
        "domains": domains,
        "origin_count": len(state["origins"]),
        "imported_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
    }


def delete_profile(workspace_id: str) -> bool:
    """Remove the imported state AND the persisted profile. True if anything went.

    Deleting only ``storage_state.json`` would leave the cookies Chromium has
    already persisted inside the profile directory, so the whole directory goes. Callers
    must close the live session FIRST — a running browser holds the profile's
    SQLite files open.
    """
    path = profiles_root() / safe_workspace_id(workspace_id)
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


__all__ = [
    "InvalidStorageState",
    "MAX_STATE_BYTES",
    "delete_profile",
    "profile_dir",
    "read_state",
    "safe_workspace_id",
    "state_path",
    "summarize",
    "validate_storage_state",
    "write_state",
]
