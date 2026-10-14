# ee/pocketpaw_ee/integrations/providers/notion.py: the ``notion`` CMS provider.
#
# Kind ``connect`` (Notion public integration OAuth), category ``cms``, plan
# ``site``. The app is ``POCKETPAW_INTEGRATIONS_NOTION_CLIENT_ID`` / ``_SECRET``;
# without both it is ``unconfigured``. Every call sends ``Notion-Version:
# 2026-03-11``. The token endpoint takes a JSON body with HTTP Basic client auth.
# ``account_ref`` = workspace_id, ``account_label`` = workspace_name, and
# ``meta`` keeps ``workspace_id`` + ``bot_id`` (non-secret). Notion sends no
# ``expires_in``: a 401 refreshes once under the vault lease
# (``get_token(rejected=...)``), and every refresh rotates the refresh token.
#
# Setting ``database_id`` is a remote select of the data sources shared with the
# integration (``POST /v1/search``, Notion's 2025-09 "data source" model); the
# stored value is a data source id. Convention: every checkbox in the data source
# SCHEMA named "Published" or "Public" (any case) gates publishing, and a row is
# public only when all of them are checked (both actions). With none, every row is
# public; ``site_setup`` writes the readonly ``publishing`` note saying so. Two
# public READ actions (no Turnstile, gateway rate limits apply):
#   * ``list_entries {limit, cursor?}`` queries the data source, newest first (a
#     publish-date property when there is one, then created_time), and projects
#     each property to a plain string (url only http(s), email / phone only when
#     they look like one; no rich-text href is ever emitted).
#   * ``get_entry {id}`` (tighter caps) reads one page, only when its parent
#     data_source_id is the configured one, and flattens its blocks to a fixed JSON
#     shape (text only, never HTML), at most ``MAX_BLOCKS`` blocks and one level
#     of children. Notion-hosted file URLs expire after about an hour; they come
#     back with ``expires_at``.
# Results, the schema and get_entry refusals are cached in process for
# ``CACHE_TTL`` seconds per (workspace, pocket, account_ref, data source, action,
# args), single-flight, bounded.
# Errors: 404 / 403 on the data source -> ``setup_required`` (marks the binding);
# a missing, foreign or unpublished page -> ``invalid_args``; 429 -> ``rate_limited``.

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from pydantic import Field

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers.base import (
    ActionArgs,
    ActionContext,
    ActionRefused,
    ActionSpec,
    Availability,
    BaseProvider,
    InvalidGrant,
    ProviderError,
    SettingOption,
    SettingsField,
    SetupContext,
    TokenGrant,
)
from pocketpaw_ee.integrations.providers.newsletter import _EMAIL_RE

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_NOTION"
NOTION_VERSION = "2026-03-11"
API = "https://api.notion.com/v1"
AUTHORIZE_URL = f"{API}/oauth/authorize"
TOKEN_URL = f"{API}/oauth/token"

CACHE_TTL = 60.0
MAX_CACHE = 1024
MAX_BLOCKS = 200
MAX_CHILD_FETCHES = 20
MAX_TEXT = 5000
MAX_VALUE = 2000
MAX_PROPERTIES = 50
EXCERPT_CHARS = 200
SEARCH_PAGES = 5

PUBLISHED_FLAGS = {"published", "public"}
DATE_NAMES = {"published at", "published on", "publish date", "published date", "date"}
EXCERPT_NAMES = {"excerpt", "summary", "description"}
_DEAD_GRANT = {"invalid_grant"}
_PHONE_RE = re.compile(r"^\+?[0-9()\- ]{3,40}$")
NOTE_ALL_PUBLIC = "No Published checkbox found: every row in this database is public."
_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)
_LIST_TYPES = {"bulleted_list_item": "bulleted_list", "numbered_list_item": "numbered_list"}
_TEXT_TYPES = {"paragraph", "quote", "toggle", "callout", "to_do"}
_UNAVAILABLE = "That entry isn't available."
# The cached answer for a page that is missing, foreign, trashed or unpublished.
_MISSING: dict[str, Any] = {}


def _norm(notion_id: str) -> str:
    return notion_id.replace("-", "").lower()


class ListEntriesArgs(ActionArgs):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9-]*$")


class GetEntryArgs(ActionArgs):
    id: str = Field(max_length=36, pattern=_ID_RE.pattern)


# --------------------------------------------------------------- HTTP


def _headers() -> dict[str, str]:
    return {"notion-version": NOTION_VERSION, "accept": "application/json"}


async def _api(
    get_token: Callable[..., Awaitable[str]] | None, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    return await oauth_http.authed(
        get_token, method, f"{API}{path}", provider="notion", headers=_headers(), **kwargs
    )


def _error_code(resp: httpx.Response) -> str:
    return str(oauth_http.body_of(resp).get("code") or "")


def _check(resp: httpx.Response, what: str, *, missing: ActionRefused) -> dict[str, Any]:
    """The JSON body of a good answer, or the refusal Notion's status maps to."""
    status = resp.status_code
    if status < 400:
        return oauth_http.body_of(resp)
    if status == 429:
        raise ActionRefused("rate_limited")
    if status in (403, 404) or _error_code(resp) in {"object_not_found", "restricted_resource"}:
        raise missing
    raise ProviderError(f"notion: {what} answered {status}")


def _database_gone() -> ActionRefused:
    return ActionRefused(
        "setup_required",
        "The site owner needs to share the Notion database again.",
        setup_error="notion_database_unavailable",
    )


def _entry_missing() -> ActionRefused:
    return ActionRefused("invalid_args", _UNAVAILABLE)


# ------------------------------------------------------------ projection


def _clean(text: str, cap: int) -> str:
    text = "".join(c for c in text if c in "\n\t" or (ord(c) >= 0x20 and ord(c) != 0x7F))
    return text[:cap]


def _plain(rich: Any, cap: int = MAX_TEXT) -> str:
    if not isinstance(rich, list):
        return ""
    return _clean("".join(str(r.get("plain_text") or "") for r in rich if isinstance(r, dict)), cap)


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return f"{value:g}" if isinstance(value, float) else str(value)
    return str(value)


def _safe_link(kind: str, value: Any) -> str:
    """A url / email / phone value only when it is what it claims to be, else ""."""
    text = _clean(str(value or ""), MAX_VALUE).strip()
    if kind == "url":
        try:
            parts = urlsplit(text)
        except ValueError:
            return ""
        return text if parts.scheme.lower() in ("http", "https") and parts.netloc else ""
    if kind == "email":
        return text if _EMAIL_RE.match(text) else ""
    return text if _PHONE_RE.match(text) else ""


def _property_text(prop: Mapping[str, Any]) -> str | None:
    """A property as a plain string, or None for a type we don't project."""
    kind = prop.get("type")
    value = prop.get(kind) if isinstance(kind, str) else None
    if kind in ("title", "rich_text"):
        return _plain(value, MAX_VALUE)
    if kind in ("select", "status"):
        return _clean(str((value or {}).get("name") or ""), MAX_VALUE)
    if kind == "multi_select":
        names = [str(o.get("name") or "") for o in value or [] if isinstance(o, dict)]
        return _clean(", ".join(n for n in names if n), MAX_VALUE)
    if kind == "date":
        return str((value or {}).get("start") or "")
    if kind in ("number", "checkbox"):
        return _clean(_scalar(value), MAX_VALUE)
    if kind in ("url", "email", "phone_number"):
        return _safe_link(kind, value)
    if kind in ("created_time", "last_edited_time"):
        return str(value or "")
    if kind == "formula" and isinstance(value, dict):
        inner = value.get(value.get("type") or "")
        if isinstance(inner, dict):  # a date formula
            return str(inner.get("start") or "")
        return _clean(_scalar(inner), MAX_VALUE)
    return None


def _published_flags(schema: Mapping[str, Any]) -> list[str]:
    """Every checkbox property of the data source SCHEMA that gates publishing.
    A row is public only when all of them are checked."""
    return [
        name
        for name, prop in schema.items()
        if isinstance(prop, dict)
        and prop.get("type") == "checkbox"
        and name.strip().lower() in PUBLISHED_FLAGS
    ]


def _publishing_note(flags: list[str]) -> str:
    if not flags:
        return NOTE_ALL_PUBLIC
    names = " and ".join(f'"{name}"' for name in flags)
    return f"Only rows with {names} checked are public."


def _date_property(schema: Mapping[str, Any]) -> str:
    for name, prop in schema.items():
        if (
            isinstance(prop, dict)
            and prop.get("type") == "date"
            and name.strip().lower() in DATE_NAMES
        ):
            return name
    return ""


def _file_url(obj: Any) -> tuple[str, str]:
    """(https url, expiry or "") of a Notion file object; ("", "") when unusable."""
    if not isinstance(obj, dict):
        return "", ""
    kind = obj.get("type")
    inner = obj.get(kind) if kind in ("external", "file") else None
    url = str((inner or {}).get("url") or "")
    if not url.startswith("https://"):
        return "", ""
    return url, str((inner or {}).get("expiry_time") or "") if kind == "file" else ""


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= EXCERPT_CHARS:
        return text
    cut = text[:EXCERPT_CHARS].rsplit(" ", 1)[0] or text[:EXCERPT_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def _entry(page: Mapping[str, Any]) -> dict[str, Any]:
    """One page as a list entry: plain strings only."""
    raw = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    properties: dict[str, str] = {}
    title = ""
    slug = published_at = excerpt = ""
    first_text = ""
    for name, prop in raw.items():
        if not isinstance(prop, dict) or len(properties) >= MAX_PROPERTIES:
            continue
        text = _property_text(prop)
        if text is None:
            continue
        key = _clean(str(name), 100)
        properties[key] = text
        lowered = key.strip().lower()
        kind = prop.get("type")
        if kind == "title":
            title = text
        elif lowered == "slug":
            slug = text
        elif kind == "date" and lowered in DATE_NAMES and not published_at:
            published_at = text
        elif kind == "rich_text" and lowered in EXCERPT_NAMES and not excerpt:
            excerpt = text
        elif kind == "rich_text" and not first_text:
            first_text = text
    out: dict[str, Any] = {
        "id": str(page.get("id") or ""),
        "title": title,
        "excerpt": _excerpt(excerpt or first_text),
        "properties": properties,
    }
    if slug:
        out["slug"] = slug
    if published_at:
        out["published_at"] = published_at
    cover, cover_expires = _file_url(page.get("cover"))
    if cover:
        out["cover_url"] = cover
        if cover_expires:
            out["cover_expires_at"] = cover_expires
    return out


def _block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    """One Notion block in our safe shape, or None for a type we drop."""
    kind = block.get("type")
    data = block.get(kind) if isinstance(kind, str) else None
    data = data if isinstance(data, dict) else {}
    if kind in ("heading_1", "heading_2", "heading_3"):
        return {"type": "heading", "level": int(kind[-1]), "text": _plain(data.get("rich_text"))}
    if kind in _TEXT_TYPES:
        out_type = "quote" if kind == "quote" else "paragraph"
        return {"type": out_type, "text": _plain(data.get("rich_text"))}
    if kind in _LIST_TYPES:
        return {"type": _LIST_TYPES[kind], "items": [_plain(data.get("rich_text"))]}
    if kind == "code":
        return {"type": "code", "text": _plain(data.get("rich_text"))}
    if kind == "image":
        url, expires = _file_url(data)
        if not url:
            return None
        out: dict[str, Any] = {"type": "image", "url": url}
        caption = _plain(data.get("caption"), MAX_VALUE)
        if caption:
            out["text"] = caption
        if expires:
            out["expires_at"] = expires
        return out
    if kind == "divider":
        return {"type": "divider"}
    return None


def _group(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive list items of one kind become one list with ``items``."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        last = out[-1] if out else None
        if "items" in block and last is not None and last["type"] == block["type"]:
            last["items"].extend(block["items"])
        else:
            out.append({**block, "items": list(block["items"])} if "items" in block else block)
    return out


# --------------------------------------------------------------- cache

_cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
_inflight: dict[tuple[str, ...], asyncio.Task[dict[str, Any]]] = {}


def clear_cache() -> None:
    _cache.clear()
    _inflight.clear()


def _store(key: tuple[str, ...], value: dict[str, Any]) -> None:
    now = time.monotonic()
    if len(_cache) >= MAX_CACHE:
        for stale in [k for k, (until, _) in _cache.items() if until <= now]:
            _cache.pop(stale, None)
        while len(_cache) >= MAX_CACHE:
            _cache.pop(next(iter(_cache)))
    _cache[key] = (now + CACHE_TTL, value)


async def _cached(
    key: tuple[str, ...], fetch: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    hit = _cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    task = _inflight.get(key)
    if task is None:

        async def _run() -> dict[str, Any]:
            value = await fetch()
            _store(key, value)
            return value

        task = asyncio.ensure_future(_run())
        _inflight[key] = task
        task.add_done_callback(
            lambda done: _inflight.pop(key, None) if _inflight.get(key) is done else None
        )
    return await asyncio.shield(task)


def _cache_key(ctx: ActionContext, source: str, *parts: str) -> tuple[str, ...]:
    account = str(ctx.connection_meta.get("workspace_id") or "")
    return (ctx.workspace_id, ctx.pocket_id, account, _norm(source), ctx.action, *parts)


# -------------------------------------------------------------- actions


def _source(ctx: ActionContext) -> str:
    source = str(ctx.settings.get("database_id") or "").strip()
    if not _ID_RE.match(source):
        raise ActionRefused(
            "setup_required",
            "The site owner needs to pick a Notion database.",
            setup_error="notion_database_missing",
        )
    return source


async def _fetch_schema(
    get_token: Callable[..., Awaitable[str]] | None, source: str
) -> dict[str, Any]:
    resp = await _api(get_token, "GET", f"/data_sources/{quote(source, safe='-')}")
    body = _check(resp, "data source", missing=_database_gone())
    props = body.get("properties")
    return props if isinstance(props, dict) else {}


async def _schema(ctx: ActionContext, source: str) -> dict[str, Any]:
    """The data source's property schema, cached like the actions' results."""
    account = str(ctx.connection_meta.get("workspace_id") or "")
    key = (ctx.workspace_id, ctx.pocket_id, account, _norm(source), "schema")
    return await _cached(key, lambda: _fetch_schema(ctx.get_token, source))


async def _clear_setup_error(ctx: ActionContext) -> None:
    from pocketpaw_ee.integrations import bindings

    await bindings.clear_setup_error(ctx.workspace_id, ctx.pocket_id, ctx.provider)


async def _list_entries(ctx: ActionContext, args: ListEntriesArgs) -> dict[str, Any]:
    source = _source(ctx)

    async def fetch() -> dict[str, Any]:
        schema = await _schema(ctx, source)
        flags, date = _published_flags(schema), _date_property(schema)
        sorts: list[dict[str, str]] = []
        if date:
            sorts.append({"property": date, "direction": "descending"})
        sorts.append({"timestamp": "created_time", "direction": "descending"})
        query: dict[str, Any] = {"page_size": args.limit, "sorts": sorts}
        checks = [{"property": f, "checkbox": {"equals": True}} for f in flags]
        if len(checks) == 1:
            query["filter"] = checks[0]
        elif checks:
            query["filter"] = {"and": checks}
        if args.cursor:
            query["start_cursor"] = args.cursor
        resp = await _api(
            ctx.get_token, "POST", f"/data_sources/{quote(source, safe='-')}/query", json=query
        )
        if resp.status_code == 400 and args.cursor:
            raise ActionRefused("invalid_args", "That page cursor isn't valid.")
        body = _check(resp, "query", missing=_database_gone())
        pages = [p for p in body.get("results") or [] if isinstance(p, dict)]
        entries = [_entry(p) for p in pages if p.get("object") == "page"]
        cursor = body.get("next_cursor") if body.get("has_more") else None
        await _clear_setup_error(ctx)
        return {"entries": entries[: args.limit], "next_cursor": cursor or None}

    return await _cached(_cache_key(ctx, source, str(args.limit), args.cursor), fetch)


async def _children(
    ctx: ActionContext, block_id: str, budget: int
) -> tuple[list[dict[str, Any]], bool]:
    """Up to ``budget`` raw children of ``block_id``; (blocks, more_left)."""
    out: list[dict[str, Any]] = []
    cursor = ""
    while len(out) < budget:
        params = {"page_size": str(min(100, budget - len(out)))}
        if cursor:
            params["start_cursor"] = cursor
        resp = await _api(
            ctx.get_token, "GET", f"/blocks/{quote(block_id, safe='-')}/children", params=params
        )
        body = _check(resp, "blocks", missing=_entry_missing())
        out.extend(b for b in body.get("results") or [] if isinstance(b, dict))
        cursor = str(body.get("next_cursor") or "") if body.get("has_more") else ""
        if not cursor:
            return out[:budget], False
    return out[:budget], bool(cursor)


async def _blocks(ctx: ActionContext, page_id: str) -> tuple[list[dict[str, Any]], bool]:
    top, truncated = await _children(ctx, page_id, MAX_BLOCKS)
    flat: list[dict[str, Any]] = []
    used, fetches = len(top), 0
    for block in top:
        shaped = _block(block)
        if shaped is not None:
            flat.append(shaped)
        if not block.get("has_children") or block.get("type") == "child_page":
            continue
        if used >= MAX_BLOCKS or fetches >= MAX_CHILD_FETCHES:
            truncated = True
            continue
        fetches += 1
        kids, more = await _children(ctx, str(block.get("id") or ""), MAX_BLOCKS - used)
        used += len(kids)
        truncated = truncated or more
        flat.extend(s for s in (_block(k) for k in kids) if s is not None)
    return _group(flat), truncated


async def _get_entry(ctx: ActionContext, args: GetEntryArgs) -> dict[str, Any]:
    source = _source(ctx)

    async def fetch() -> dict[str, Any]:
        flags = _published_flags(await _schema(ctx, source))
        resp = await _api(ctx.get_token, "GET", f"/pages/{quote(args.id, safe='-')}")
        if resp.status_code in (403, 404):
            return _MISSING  # cached too: a probe for a bad id costs Notion one call a minute
        page = _check(resp, "page", missing=_entry_missing())
        parent = page.get("parent") if isinstance(page.get("parent"), dict) else {}
        if (
            page.get("object") != "page"
            or _norm(str(parent.get("data_source_id") or "")) != _norm(source)
            or page.get("in_trash")
            or page.get("archived")
        ):
            return _MISSING
        props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
        for flag in flags:
            prop = props.get(flag)
            if not isinstance(prop, dict) or prop.get("checkbox") is not True:
                return _MISSING
        entry = _entry(page)
        blocks, truncated = await _blocks(ctx, str(page.get("id") or args.id))
        entry.pop("excerpt", None)
        entry["blocks"] = blocks
        if truncated:
            entry["truncated"] = True
        return entry

    result = await _cached(_cache_key(ctx, source, _norm(args.id)), fetch)
    if result is _MISSING:
        raise _entry_missing()
    return result


# ------------------------------------------------------------- provider


async def _token_request(body: dict[str, str], client_id: str, secret: str) -> dict[str, Any]:
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    headers = {**_headers(), "authorization": f"Basic {basic}"}
    try:
        with oauth_http.untraced():
            async with oauth_http.client() as http:
                resp = await http.post(TOKEN_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"notion: token endpoint unreachable ({type(exc).__name__})") from None
    data = oauth_http.body_of(resp)
    if resp.status_code >= 400 or not data.get("access_token"):
        reason = str(data.get("error") or data.get("code") or "").lower()
        if reason in _DEAD_GRANT:
            raise InvalidGrant(f"notion: {reason}")
        raise ProviderError(f"notion: token endpoint answered {resp.status_code}")
    return data


class NotionProvider(BaseProvider):
    id = "notion"
    name = "Notion"
    kind = "connect"
    category = "cms"
    plan = "site"
    supports_pkce = False
    settings_fields = (
        SettingsField(
            key="database_id",
            label="Database",
            type="select",
            help="The Notion database the site reads. Share it with the integration first.",
            required=True,
            options_source="remote",
            max_length=36,
        ),
        SettingsField(
            key="publishing",
            label="Who sees rows",
            type="text",
            readonly=True,
            help="Set from the database: a Published or Public checkbox hides unchecked rows.",
        ),
    )
    actions = {
        "list_entries": ActionSpec(
            args=ListEntriesArgs,
            handler=_list_entries,
            description="List the newest entries of the site's Notion database (a blog, news).",
            public=True,
            ip_per_min=60,
            site_per_min=300,
        ),
        "get_entry": ActionSpec(
            args=GetEntryArgs,
            handler=_get_entry,
            description="Read one entry of the site's Notion database with its content.",
            public=True,
            ip_per_min=20,
            site_per_min=60,
        ),
    }

    def availability(self) -> Availability:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        return "available" if client_id and secret else "unconfigured"

    def _credentials(self) -> tuple[str, str]:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id or not secret:
            raise ProviderError("notion: not configured")
        return client_id, secret

    def authorize_url(
        self,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,  # noqa: ARG002 - no PKCE
    ) -> str:
        client_id, _ = self._credentials()
        params = {
            "client_id": client_id,
            "response_type": "code",
            "owner": "user",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,  # noqa: ARG002 - no PKCE
    ) -> TokenGrant:
        client_id, secret = self._credentials()
        body = await _token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            client_id,
            secret,
        )
        workspace_id = str(body.get("workspace_id") or "")
        if not workspace_id:
            raise ProviderError("notion: no workspace id in the grant")
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            account_label=str(body.get("workspace_name") or "Notion workspace"),
            account_ref=workspace_id,
            meta={"workspace_id": workspace_id, "bot_id": str(body.get("bot_id") or "")},
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        client_id, secret = self._credentials()
        body = await _token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}, client_id, secret
        )
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
        )

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:
        """Tell the owner which rows are public (readonly ``publishing``)."""
        source = str((getattr(binding, "settings", None) or {}).get("database_id") or "")
        if not _ID_RE.match(source):
            return {"publishing": ""}
        schema = await _fetch_schema(ctx.get_token, source)
        return {"publishing": _publishing_note(_published_flags(schema))}

    async def field_options(
        self,
        binding: Any,  # noqa: ARG002
        ctx: SetupContext,
        key: str,
    ) -> list[SettingOption]:
        if key != "database_id":
            return []
        options: list[SettingOption] = []
        cursor = ""
        for _ in range(SEARCH_PAGES):
            query: dict[str, Any] = {
                "filter": {"property": "object", "value": "data_source"},
                "page_size": 100,
            }
            if cursor:
                query["start_cursor"] = cursor
            resp = await _api(ctx.get_token, "POST", "/search", json=query)
            if resp.status_code >= 400:
                raise ProviderError(f"notion: search answered {resp.status_code}")
            body = oauth_http.body_of(resp)
            for item in body.get("results") or []:
                if not isinstance(item, dict) or item.get("in_trash"):
                    continue
                source_id = str(item.get("id") or "")
                if _ID_RE.match(source_id):
                    label = _plain(item.get("title"), 200).strip() or "Untitled database"
                    options.append(SettingOption(value=source_id, label=label))
            cursor = str(body.get("next_cursor") or "") if body.get("has_more") else ""
            if not cursor:
                break
        return options
