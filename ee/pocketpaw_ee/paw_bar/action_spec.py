# ee/pocketpaw_ee/paw_bar/action_spec.py — validate the one page action a v2 concierge may suggest.
#
# A site with ``Site.concierge_page_actions`` on lets the v2 model end a reply
# with ONE ```pawbar-action fence: {"do": ..., "to"?: ..., "target"?: ...,
# "name"?: ..., "args"?: ..., "label": ...}. The model still has no tools; the
# fence is only a suggestion. ``FenceFilter`` (concierge_runtime.py) hands each
# such fence to ``render_action``, which returns the action the runner streams as
# the SSE ``action`` frame, or None to drop it (logged, never shown):
#
#   * ``navigate`` needs ``to``: a site path or absolute http(s) url that resolves
#     to ``site_origin`` AND names a known page (any page in the crawl index or
#     a catalog item's url; the runner adds the visitor's page), compared on
#     origin + path with a trailing slash ignored. The emitted ``to`` is always
#     absolute, query dropped, an ``#id`` fragment kept.
#   * ``scroll_to`` / ``highlight`` need ``target``: ``#id`` (``TARGET_ID_RE``) or
#     heading text, one line, at most ``TARGET_MAX`` chars, no ``<`` or ``>``.
#   * ``tool`` needs ``name``, one of this turn's declared tools, and ``args``
#     (absent means {}) matching that tool's schema exactly (``_check_args``).
#   * ``label`` is required everywhere: plain one-line text, at most ``LABEL_MAX``.
#
# Declared tools come from the host page (``window.pawbarTools``, sent by the
# frame as ``page.tools``) and are untrusted. ``valid_tools`` keeps the first
# ``TOOLS_MAX`` entries that pass the contract (name regex, short description, a
# flat object schema of string / number / integer / boolean properties, at most
# ``TOOL_SCHEMA_MAX`` chars of JSON), each bad one dropped alone, never raising.
# The frame (paw-bar ``page-tools.ts``) applies the same rules, so tool text is
# measured as JavaScript does it: UTF-16 units, ``\s`` whitespace, the schema's
# size as ``JSON.stringify`` writes it (``_js_len``, ``_js_json_len``).
#
# The host script (paw-bar ``actions/``) checks again on the page; the server is
# the authority on which pages exist. ``site_pages`` lists the pages the prompt
# offers (crawled pages shallowest first, then catalog products) and
# ``known_urls`` is what ``render_action`` accepts: the WHOLE crawl index, never
# capped (it costs no prompt tokens), plus the turn's catalog items, so a page
# the prompt names only as a knowledge item's path still validates. Verdicts
# shared with paw-bar live in
# tests/fixtures/action_parity/ (cases.json + expected.json, the same files as
# paw-bar's app/tests/fixtures/action_parity); server-only ones in
# server_cases.json beside them.

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

logger = logging.getLogger(__name__)

VERBS: tuple[str, ...] = ("navigate", "scroll_to", "highlight")
LABEL_MAX = 80
TARGET_MAX = 120
# The longest fence body and ``to`` read at all.
BODY_MAX = 2_000
URL_MAX = 2_048
TARGET_ID_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_-]{0,63}")
# Pages the prompt lists, at most: crawled pages, then catalog items with a url.
PROMPT_PAGES = 40
PROMPT_PRODUCT_PAGES = 20
# Declared tools: the verb, and the contract's limits.
TOOL_VERB = "tool"
TOOLS_MAX = 12
TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
TOOL_DESCRIPTION_MAX = 200
# Chars of the schema as compact JSON (what JSON.stringify gives on the page).
TOOL_SCHEMA_MAX = 2_048
# The longest string argument, whatever a schema's maxLength says.
ARG_STRING_MAX = 200
ARG_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
ENUM_MAX = 50
# A string enum value: 1 to ENUM_STRING_MAX chars (JS length), none of ``< > « »``,
# so the prompt's quote() never cuts or swaps a character of it, and it can't
# open a block or close its own quote.
ENUM_STRING_MAX = 80
_ENUM_STRING_BAD = frozenset("<>«»")
ARG_TYPES: tuple[str, ...] = ("string", "number", "integer", "boolean")
_PROPERTY_KEYS = frozenset({"type", "description", "enum", "minimum", "maximum", "maxLength"})
_SCHEMA_KEYS = frozenset({"type", "properties", "required"})

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# JavaScript's ``\s``, what the frame collapses and trims. ``str.split`` differs:
# it also splits on \x1c-\x1f and \x85, and not on \ufeff.
_JS_SPACE_RE = re.compile(
    r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)
_PATH_SAFE = "/-._~!$&'()*+,;=:@"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _drop(reason: str) -> None:
    logger.info("concierge: dropped a pawbar-action (%s)", reason)


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of an absolute http(s) url, lower-cased, default
    port dropped; "" for anything else."""
    split = _split(url)
    return split[0] if split else ""


def _split(url: str) -> tuple[str, str, str] | None:
    """(origin, path without trailing slash, fragment) of an absolute http(s)
    url, or None. The path is re-quoted so two spellings of one path compare
    equal; the homepage's path is ""."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = quote(unquote(parts.path or "/"), safe=_PATH_SAFE).rstrip("/")
    return f"{scheme}://{netloc}", path, parts.fragment


def _resolve(ref: str, site_origin: str) -> tuple[str, str, str] | None:
    """``ref`` (a site path or an absolute url) resolved against ``site_origin``,
    split; None when it is not on that origin."""
    if not site_origin:
        return None
    split = _split(urljoin(site_origin + "/", (ref or "").strip()))
    if split is None or split[0] != site_origin:
        return None
    return split


def _catalog_paths(catalog: Sequence[Any], site_origin: str) -> list[tuple[str, str]]:
    """``(path, name)`` for each catalog item whose url is on ``site_origin``."""
    out: list[tuple[str, str]] = []
    for item in catalog or ():
        raw = str(getattr(item, "url", "") or "").strip()
        split = _resolve(raw, site_origin) if raw else None
        if split is not None:
            out.append((split[1] or "/", str(getattr(item, "name", "") or "")))
    return out


def known_urls(site: Any, catalog: Sequence[Any], site_origin: str) -> list[str]:
    """Every page a ``navigate`` may name: each crawled page (``kb_page_index``)
    and each catalog item url on ``site_origin``, as absolute urls."""
    if not site_origin:
        return []
    crawled = [f"{site_origin}/{key}" for key in (getattr(site, "kb_page_index", None) or {})]
    return crawled + [site_origin + path for path, _ in _catalog_paths(catalog, site_origin)]


def site_pages(site: Any, catalog: Sequence[Any], site_origin: str) -> list[tuple[str, str]]:
    """``(path, title)`` for the pages the prompt offers, at most ``PROMPT_PAGES``:
    crawled pages first, shallowest path first then by key, filling what the
    products leave; then up to ``PROMPT_PRODUCT_PAGES`` of the turn's catalog items
    with a url on ``site_origin`` (titled by name). A crawled page that is one of
    those products is listed once, as the product, so a store's crawled product
    pages never push its own pages (size guide, shipping) off the list."""
    if not site_origin:
        return []
    products: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, name in _catalog_paths(catalog, site_origin):
        if len(products) >= PROMPT_PRODUCT_PAGES:
            break
        if path.rstrip("/") not in seen:
            seen.add(path.rstrip("/"))
            products.append((path, name))
    index = getattr(site, "kb_page_index", None) or {}
    crawled = sorted(
        (
            (f"/{key}", str(entry.get("title") or "") if isinstance(entry, dict) else "")
            for key, entry in index.items()
            if f"/{key}".rstrip("/") not in seen
        ),
        key=lambda page: (page[0].count("/"), page[0]),
    )
    return crawled[: PROMPT_PAGES - len(products)] + products


def _number(value: Any) -> bool:
    """A finite JSON number (a bool is not one)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
    )


def _js_len(text: str) -> int:
    """``text.length`` in JavaScript: UTF-16 code units, so an emoji counts 2."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _js_number(value: int | float) -> str:
    """A finite number as ``JSON.stringify`` writes it: 1.0 is "1", 1e-05 is
    "0.00001", 1e-08 is "1e-8", 1e+21 is "1e+21"."""
    if abs(value) < 1e21 and float(value).is_integer():
        return str(int(value))
    value = float(value)
    text = repr(value)
    if 1e-7 <= abs(value) < 1e21:
        return format(Decimal(text), "f")
    mantissa, _, exponent = text.partition("e")
    power = int(exponent)
    return f"{mantissa}e{'+' if power > 0 else '-'}{abs(power)}"


def _js_json_len(value: Any) -> int:
    """``JSON.stringify(value).length`` for a parsed JSON value."""
    if isinstance(value, dict):
        sizes = [_js_json_len(str(k)) + 1 + _js_json_len(v) for k, v in value.items()]
        return 2 + sum(sizes) + max(len(sizes) - 1, 0)
    if isinstance(value, list):
        return 2 + sum(_js_json_len(v) for v in value) + max(len(value) - 1, 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return len(_js_number(value))
    return _js_len(json.dumps(value, ensure_ascii=False))


def _plain(value: Any, cap: int) -> str | None:
    """``value`` as non-empty one-line text of at most ``cap`` chars, or None."""
    if not isinstance(value, str):
        return None
    text = _JS_SPACE_RE.sub(" ", value).strip(" ")
    if not text or _js_len(text) > cap or _CONTROL_RE.search(text):
        return None
    return text


def _fits(kind: str, value: Any) -> bool:
    """``value`` is a JSON value of the schema type ``kind``."""
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return _number(value) and float(value).is_integer()
    return _number(value)


def _valid_enum_string(value: str) -> bool:
    return 1 <= _js_len(value) <= ENUM_STRING_MAX and not _ENUM_STRING_BAD & set(value)


def _valid_property(prop: Any) -> bool:
    if not isinstance(prop, dict) or not set(prop) <= _PROPERTY_KEYS:
        return False
    kind = prop.get("type")
    if kind not in ARG_TYPES:
        return False
    if "description" in prop and _plain(prop["description"], TOOL_DESCRIPTION_MAX) is None:
        return False
    if "enum" in prop:
        enum = prop["enum"]
        if not isinstance(enum, list) or not enum or len(enum) > ENUM_MAX:
            return False
        if not all(_fits(kind, v) for v in enum):
            return False
        if kind == "string" and not all(_valid_enum_string(v) for v in enum):
            return False
    bounds = [b for b in ("minimum", "maximum") if b in prop]
    if bounds and (kind not in ("number", "integer") or not all(_number(prop[b]) for b in bounds)):
        return False
    if len(bounds) == 2 and prop["minimum"] > prop["maximum"]:
        return False
    if "maxLength" in prop:
        cap = prop["maxLength"]
        if kind != "string" or not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            return False
    return True


def _valid_schema(schema: Any) -> bool:
    """A flat object schema the contract allows, at most ``TOOL_SCHEMA_MAX``."""
    if not isinstance(schema, dict) or not set(schema) <= _SCHEMA_KEYS:
        return False
    if schema.get("type") != "object":
        return False
    # Absent or null properties / required are empty, as on the frame.
    props = schema.get("properties")
    props = {} if props is None else props
    if not isinstance(props, dict):
        return False
    if not all(isinstance(k, str) and ARG_NAME_RE.fullmatch(k) for k in props):
        return False
    if not all(_valid_property(p) for p in props.values()):
        return False
    required = schema.get("required")
    required = [] if required is None else required
    if not isinstance(required, list):
        return False
    if not all(isinstance(r, str) and r in props for r in required):
        return False
    if len(set(required)) != len(required):
        return False
    return _js_json_len(schema) <= TOOL_SCHEMA_MAX


def _valid_tool(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
        return None
    description = _plain(raw.get("description"), TOOL_DESCRIPTION_MAX)
    schema = raw.get("input_schema")
    if description is None or not _valid_schema(schema):
        return None
    # A deep copy through JSON: the caller's dict is never shared.
    return {
        "name": name,
        "description": description,
        "input_schema": json.loads(json.dumps(schema)),
    }


def valid_tools(raw: Any) -> list[dict[str, Any]]:
    """The page's declared tools that pass the contract, as ``{name, description,
    input_schema}``: only the first ``TOOLS_MAX`` entries are read, a bad one is
    dropped on its own, a repeated name keeps the first. Never raises; anything
    that is not a list is no tools."""
    if not isinstance(raw, list):
        return []
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for entry in raw[:TOOLS_MAX]:
        try:
            tool = _valid_tool(entry)
        except (TypeError, ValueError, RecursionError):
            tool = None
        if tool is None:
            logger.info("concierge: dropped a declared tool")
        elif tool["name"] not in names:
            names.add(tool["name"])
            tools.append(tool)
    return tools


def _check_args(args: Any, schema: dict[str, Any]) -> dict[str, Any] | None:
    """``args`` when they match ``schema`` exactly (types, required keys, no other
    keys, enum, minimum / maximum, strings within ``maxLength`` and
    ``ARG_STRING_MAX``), whole-number floats read as integers; else None. Absent
    args are {}."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    props: dict[str, Any] = schema.get("properties") or {}
    if not set(args) <= set(props) or not set(schema.get("required") or ()) <= set(args):
        return None
    out: dict[str, Any] = {}
    for key, value in args.items():
        prop = props[key]
        kind = prop["type"]
        if not _fits(kind, value):
            return None
        if kind == "integer":
            value = int(value)
        cap = min(ARG_STRING_MAX, prop.get("maxLength", ARG_STRING_MAX))
        if kind == "string" and _js_len(value) > cap:
            return None
        if "enum" in prop and value not in prop["enum"]:
            return None
        if "minimum" in prop and value < prop["minimum"]:
            return None
        if "maximum" in prop and value > prop["maximum"]:
            return None
        out[key] = value
    return out


def _tool_call(data: dict[str, Any], tools: Iterable[dict[str, Any]]) -> dict | None:
    """``{name, args}`` for a ``tool`` action naming one of ``tools`` with args
    its schema accepts, or None."""
    name = data.get("name")
    tool = next((t for t in tools if t.get("name") == name), None)
    if tool is None:
        _drop(f"unknown tool {str(name)[:40]!r}")
        return None
    args = _check_args(data.get("args"), tool["input_schema"])
    if args is None:
        _drop(f"args do not match tool {tool['name']!r}")
        return None
    return {"name": tool["name"], "args": args}


def _label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > LABEL_MAX or _CONTROL_RE.search(text) or "<" in text or ">" in text:
        return None
    return text


def _target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if text.startswith("#"):
        return text if TARGET_ID_RE.fullmatch(text) else None
    if not text or len(text) > TARGET_MAX or _CONTROL_RE.search(text) or "<" in text or ">" in text:
        return None
    return text


def _navigate_to(value: Any, site_origin: str, known: Iterable[str]) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > URL_MAX or "\\" in raw or _CONTROL_RE.search(raw):
        return None
    if any(ch.isspace() for ch in raw):
        return None
    split = _resolve(raw, site_origin)
    if split is None:
        return None
    pages = {s[:2] for k in known if (s := _resolve(k, site_origin)) is not None}
    if split[:2] not in pages:
        return None
    origin, path, fragment = split
    url = f"{origin}{path or '/'}"
    if fragment and TARGET_ID_RE.fullmatch(f"#{fragment}"):
        url += f"#{fragment}"
    return url


def render_action(
    body: str,
    *,
    site_origin: str,
    known_urls: Iterable[str],
    tools: Sequence[dict[str, Any]] = (),
) -> dict | None:
    """The action a ```pawbar-action fence body asks for, validated, or None.

    ``site_origin`` is the origin the visitor's page is on (``origin_of``); ""
    refuses every ``navigate``. ``known_urls`` are the pages a ``navigate`` may
    name, absolute or as site paths. ``tools`` are this turn's declared tools,
    already through ``valid_tools``; none refuses every ``tool``. The result has
    ``do`` and ``label``, plus ``to`` (absolute) for navigate, ``target`` for
    scroll_to / highlight, or ``name`` and ``args`` for tool; any other key the
    model wrote is dropped."""
    if not isinstance(body, str) or len(body) > BODY_MAX:
        _drop("body too long")
        return None
    try:
        data = json.loads(body)
    except ValueError:
        _drop("not json")
        return None
    if not isinstance(data, dict):
        _drop("not an object")
        return None
    verb = data.get("do")
    if verb not in VERBS and verb != TOOL_VERB:
        _drop(f"unknown verb {str(verb)[:40]!r}")
        return None
    label = _label(data.get("label"))
    if label is None:
        _drop("bad label")
        return None
    if verb == TOOL_VERB:
        call = _tool_call(data, tools or ())
        return None if call is None else {"do": verb, **call, "label": label}
    if verb == "navigate":
        to = _navigate_to(data.get("to"), (site_origin or "").lower(), known_urls)
        if to is None:
            _drop("navigate to an unknown or foreign page")
            return None
        return {"do": verb, "to": to, "label": label}
    target = _target(data.get("target"))
    if target is None:
        _drop("bad target")
        return None
    return {"do": verb, "target": target, "label": label}


__all__ = [
    "ARG_STRING_MAX",
    "ENUM_STRING_MAX",
    "LABEL_MAX",
    "TARGET_ID_RE",
    "TARGET_MAX",
    "TOOLS_MAX",
    "TOOL_DESCRIPTION_MAX",
    "TOOL_NAME_RE",
    "TOOL_SCHEMA_MAX",
    "TOOL_VERB",
    "VERBS",
    "known_urls",
    "origin_of",
    "render_action",
    "site_pages",
    "valid_tools",
]
