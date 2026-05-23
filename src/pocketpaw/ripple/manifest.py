# backend/src/pocketpaw/ripple/manifest.py
"""Fetch and cache the Ripple widget manifest from a CDN.

The manifest is generated at ripple build time and published as
`@ripple-ui/svelte/dist/manifest.json`. This module fetches it,
caches the parse result in-process for a configurable TTL, and
formats it as a markdown block suitable for system-prompt injection.

On any failure (network, timeout, parse, schema), get_manifest()
returns None — the caller is expected to fall back to a different
source (today: kb scope search).

Changes:
  - 2026-05-22 (Increment 5): added ``validate_against_catalog`` — a
    catalog-as-allowlist ingest gate that flags any node whose ``type``
    is not in the widget manifest (sibling to ``validate_against_manifest``,
    which only checks prop drift). Plus ``check_embed_node`` /
    ``check_embed_nodes_in_spec`` — an https-only, host-allowlisted URL
    policy for the sanctioned ``embed`` escape-hatch widget, with
    loopback / private / link-local hosts hard-blocked unconditionally.
"""

from __future__ import annotations

import difflib
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# Control-flow node types that are always allowed alongside catalog
# widgets — they carry no widget ``type`` from the renderer's closed
# registry, they're spec grammar (``each`` loops, ``if`` gates).
_CONTROL_FLOW_TYPES: frozenset[str] = frozenset({"if", "each"})

# Curated default host allow-list for the ``embed`` widget's ``url`` mode.
# These providers ship sandbox-friendly iframe-embeddable pages. The
# operator can widen this via ``POCKETPAW_RIPPLE_EMBED_ALLOWED_HOSTS``.
DEFAULT_EMBED_ALLOWED_HOSTS: tuple[str, ...] = (
    "youtube-nocookie.com",
    "player.vimeo.com",
    "codepen.io",
    "codesandbox.io",
    "observablehq.com",
    "www.figma.com",
)

_FETCH_TIMEOUT_SECONDS = 5.0
_SCHEMA = "ripple.manifest/v1"

# Module-global cache: url -> (expires_at_monotonic, parsed_manifest)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def get_manifest(url: str, ttl_seconds: int) -> dict[str, Any] | None:
    """Fetch the manifest from `url`, with in-process TTL caching.

    Returns the parsed manifest dict on success, or None on any failure.
    """
    now = time.monotonic()
    cached = _cache.get(url)
    if cached is not None and cached[0] > now:
        return cached[1]

    parsed = await _fetch_and_validate(url)
    if parsed is None:
        return None

    _cache[url] = (now + ttl_seconds, parsed)
    return parsed


async def _fetch_and_validate(url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=_FETCH_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        logger.warning("ripple manifest fetch timed out: %s", url)
        return None
    except httpx.HTTPError as exc:
        logger.warning("ripple manifest fetch failed: %s (%s)", url, exc)
        return None

    if response.status_code >= 400:
        logger.warning("ripple manifest fetch returned %s: %s", response.status_code, url)
        return None

    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        logger.error("ripple manifest is not valid JSON: %s", url)
        return None

    if not isinstance(parsed, dict):
        logger.error("ripple manifest is not a JSON object: %s", url)
        return None
    if parsed.get("schema") != _SCHEMA:
        logger.error("ripple manifest schema mismatch (got %r): %s", parsed.get("schema"), url)
        return None
    if not isinstance(parsed.get("widgets"), list):
        logger.error("ripple manifest missing widgets array: %s", url)
        return None

    # spec envelope is optional for backward compatibility with older
    # manifests, but log if missing — agents fed a manifest without it
    # have to fall back to the system prompt for the envelope contract.
    if not isinstance(parsed.get("spec"), dict):
        logger.info("ripple manifest has no spec envelope (older publish): %s", url)

    return parsed


# Per-widget inner-item alias table for KNOWN drift in widget item arrays.
# The manifest declares item shapes as TypeScript strings (e.g.
# ``Array<{ text: string; ... }>``) which are not machine-introspectable,
# so a small hand-curated alias table covers the widgets whose item
# arrays have empirically drifted in production. Add an entry here the
# first time a new drift is observed — the comment trail below doubles
# as a record of past LLM failures.
#
# Format: ``{ widget_type: { items_prop_key: { wrong_inner_key: right_inner_key } } }``
_KNOWN_ITEM_ALIASES: dict[str, dict[str, dict[str, str]]] = {
    # `timeline` events: agents emit `description` (universal name)
    # instead of the manifest's `detail`. Clean rename.
    "timeline": {"events": {"description": "detail"}},
}


def validate_against_manifest(
    spec: dict[str, Any] | None,
    manifest: dict[str, Any],
    *,
    apply_aliases: bool = False,
) -> list[dict[str, Any]]:
    """Walk a rippleSpec tree and flag nodes whose ``props`` keys are not
    declared in the manifest for that ``type``.

    Also covers a small set of widgets whose inner item arrays have
    drifted in production (see ``_KNOWN_ITEM_ALIASES``). When
    ``apply_aliases=True``, the spec is mutated in place to rewrite the
    known wrong keys to their canonical form.

    Returns one issue dict per offending node; ``[]`` when ``spec`` is not
    a dict or no issues are found. Issue shape::

        {
          "path": "ui.children[2].children[1]",
          "type": "timeline",
          "unknown_props": ["maxItem"],          # top-level prop drift
          "allowed_props": ["events", "maxItems"],
          "item_issues": [                       # inner-item drift
            {
              "path": "...props.events[0]",
              "from": "description",
              "to": "detail",
              "applied": True,
            },
          ],
        }
    """
    if not isinstance(spec, dict):
        return []
    widgets = manifest.get("widgets") or []
    by_type: dict[str, set[str]] = {}
    for w in widgets:
        t = w.get("type")
        if isinstance(t, str):
            by_type[t] = set((w.get("props") or {}).keys())

    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    issues: list[dict[str, Any]] = []
    _walk_validate(root, "ui", by_type, apply_aliases, issues)
    return issues


def _walk_validate(
    node: Any,
    path: str,
    by_type: dict[str, set[str]],
    apply_aliases: bool,
    issues: list[dict[str, Any]],
) -> None:
    if not isinstance(node, dict):
        return

    wtype = node.get("type")
    props = node.get("props")
    if isinstance(wtype, str) and wtype in by_type and isinstance(props, dict):
        allowed = by_type[wtype]
        unknown = sorted(k for k in props.keys() if k not in allowed)
        item_issues: list[dict[str, Any]] = []

        for items_key, alias_map in _KNOWN_ITEM_ALIASES.get(wtype, {}).items():
            arr = props.get(items_key)
            if not isinstance(arr, list):
                continue
            for i, item in enumerate(arr):
                if not isinstance(item, dict):
                    continue
                for wrong_k, right_k in alias_map.items():
                    if wrong_k not in item or right_k in item:
                        continue
                    if apply_aliases:
                        item[right_k] = item.pop(wrong_k)
                    item_issues.append(
                        {
                            "path": f"{path}.props.{items_key}[{i}]",
                            "from": wrong_k,
                            "to": right_k,
                            "applied": apply_aliases,
                        }
                    )

        if unknown or item_issues:
            issues.append(
                {
                    "path": path,
                    "type": wtype,
                    "unknown_props": unknown,
                    "allowed_props": sorted(allowed),
                    "item_issues": item_issues,
                }
            )

    children = node.get("children")
    if isinstance(children, list):
        for i, child in enumerate(children):
            _walk_validate(child, f"{path}.children[{i}]", by_type, apply_aliases, issues)


# ---------------------------------------------------------------------------
# Catalog-as-allowlist ingest gate (Increment 5).
#
# Where ``validate_against_manifest`` checks per-widget *prop* drift, this
# gate checks the ``type`` itself: a node whose ``type`` is not a known
# catalog widget renders as a red "Unknown widget type" box. Catching it
# at ingest time lets the agent-generation path retry with a corrective
# prompt instead of shipping the broken box to the user.
# ---------------------------------------------------------------------------


def allowed_types_from_manifest(manifest: dict[str, Any]) -> list[str]:
    """Extract the widget ``type`` list from a parsed manifest.

    The widget-manifest's ``widgets`` array is the catalog allow-list.
    Returns a sorted list of unique string types; ``[]`` when the
    manifest has no widgets.
    """
    widgets = manifest.get("widgets") or []
    types: set[str] = set()
    for w in widgets:
        t = w.get("type")
        if isinstance(t, str) and t:
            types.add(t)
    return sorted(types)


def validate_against_catalog(
    spec: dict[str, Any] | None,
    allowed_types: list[str] | set[str],
) -> list[dict[str, Any]]:
    """Walk a rippleSpec tree and flag every node whose ``type`` is not in
    the catalog allow-list.

    ``allowed_types`` is the widget-manifest type list (the renderer's
    closed widget registry). The control-flow types ``if`` and ``each``
    are always allowed on top of it — they're spec grammar, not widgets.

    Returns one issue dict per offending node; ``[]`` when ``spec`` is not
    a dict or every node's type is known. Issue shape::

        {
          "path": "ui.children[2]",
          "type": "revenue-card",
          "suggestion": "card",      # nearest catalog match, or None
        }

    ``suggestion`` is the closest catalog widget by edit distance
    (``difflib.get_close_matches``) so a corrective prompt can point the
    agent at the right name. It is ``None`` when nothing is close.
    """
    if not isinstance(spec, dict):
        return []
    allowed: set[str] = set(allowed_types) | _CONTROL_FLOW_TYPES
    # Suggestion pool excludes the control-flow types — suggesting `if`
    # / `each` for a mistyped widget name is never the right fix.
    pool = sorted(set(allowed_types))

    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    issues: list[dict[str, Any]] = []
    _walk_catalog(root, "ui", allowed, pool, issues)
    return issues


def _walk_catalog(
    node: Any,
    path: str,
    allowed: set[str],
    pool: list[str],
    issues: list[dict[str, Any]],
) -> None:
    if not isinstance(node, dict):
        return

    wtype = node.get("type")
    if isinstance(wtype, str) and wtype and wtype not in allowed:
        matches = difflib.get_close_matches(wtype, pool, n=1, cutoff=0.6)
        issues.append(
            {
                "path": path,
                "type": wtype,
                "suggestion": matches[0] if matches else None,
            }
        )

    for key in ("children", "else_children"):
        kids = node.get(key)
        if isinstance(kids, list):
            for i, child in enumerate(kids):
                _walk_catalog(child, f"{path}.{key}[{i}]", allowed, pool, issues)


# ---------------------------------------------------------------------------
# Embed URL / host policy (Increment 5).
#
# The `embed` widget is the sanctioned escape hatch for content the widget
# catalog can't express (a CodePen, a Figma frame, a self-contained viz).
# Its `mode: "url"` form points an iframe at a third-party page, so the URL
# is an SSRF / clickjacking boundary: https-only, host must be allow-listed,
# and loopback / private / link-local / cloud-metadata hosts are blocked
# UNCONDITIONALLY — even if the allow-list is widened to ``["*"]``.
# ---------------------------------------------------------------------------


def _host_allowed(host: str, allowed_hosts: list[str] | set[str]) -> bool:
    """Return True when ``host`` is permitted by the allow-list.

    A literal ``"*"`` entry widens the allow-list to every host (the
    operator's explicit opt-in). Otherwise a host matches when it equals
    an allow-list entry exactly, or is a sub-domain of one.
    """
    host = host.lower().rstrip(".")
    for entry in allowed_hosts:
        if not isinstance(entry, str) or not entry:
            continue
        if entry == "*":
            return True
        e = entry.lower().rstrip(".")
        if host == e or host.endswith("." + e):
            return True
    return False


def check_embed_url(url: Any, allowed_hosts: list[str] | set[str]) -> str | None:
    """Validate one ``embed`` node ``url`` against the embed policy.

    Returns ``None`` when the URL passes. Returns a human-readable reason
    string when it fails. The policy, in order:

    * ``url`` must be a non-empty string.
    * scheme must be ``https`` — plain ``http`` is rejected.
    * host must be present.
    * loopback / RFC1918 / link-local / carrier-grade-NAT / cloud-metadata
      hosts are hard-blocked **unconditionally** — this survives an
      ``allowed_hosts`` widened to ``["*"]``.
    * host must be in ``allowed_hosts`` (or ``allowed_hosts`` contains
      ``"*"``).
    """
    # Imported here, not at module top, to keep the OSS-core import graph
    # of this module light — the SSRF host classifier lives in security/.
    from pocketpaw.security.url_validators import host_is_internal

    if not isinstance(url, str) or not url.strip():
        return "embed url must be a non-empty string"
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        return f"embed url scheme '{parts.scheme or '(none)'}' not allowed — must be https"
    if not parts.hostname:
        return f"embed url has no host: {url!r}"
    host = parts.hostname
    # Hard block runs BEFORE the allow-list check so a `["*"]` allow-list
    # can never re-enable an internal target.
    if host_is_internal(host):
        return (
            f"embed url host '{host}' is loopback/private/link-local — "
            f"blocked unconditionally (SSRF / metadata-endpoint protection)"
        )
    if not _host_allowed(host, allowed_hosts):
        return f"embed url host '{host}' is not in the embed allow-list"
    return None


def check_embed_node(
    node: dict[str, Any],
    allowed_hosts: list[str] | set[str],
) -> str | None:
    """Validate a single ``embed`` node's policy.

    Only ``mode: "url"`` embeds carry a URL boundary — a ``mode:
    "srcdoc"`` embed is self-contained HTML and is not checked here.
    Returns ``None`` when the node passes (or is not a URL-mode embed);
    a reason string otherwise.
    """
    if not isinstance(node, dict) or node.get("type") != "embed":
        return None
    props = node.get("props")
    if not isinstance(props, dict):
        return None
    if props.get("mode") != "url":
        return None
    return check_embed_url(props.get("url"), allowed_hosts)


def find_embed_nodes(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Walk a rippleSpec tree and return every ``embed`` node found.

    Used by the ingest pipeline to decide whether a spec needs an
    embed-policy check and an audit-log entry.
    """
    if not isinstance(spec, dict):
        return []
    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    found: list[dict[str, Any]] = []
    _walk_collect_embeds(root, found)
    return found


def _walk_collect_embeds(node: Any, found: list[dict[str, Any]]) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "embed":
        found.append(node)
    for key in ("children", "else_children"):
        kids = node.get(key)
        if isinstance(kids, list):
            for child in kids:
                _walk_collect_embeds(child, found)


def check_embed_nodes_in_spec(
    spec: dict[str, Any] | None,
    allowed_hosts: list[str] | set[str],
) -> list[dict[str, Any]]:
    """Walk a rippleSpec tree and flag every ``embed`` node that violates
    the URL/host policy.

    Returns one issue dict per offending node; ``[]`` when the spec has no
    policy-violating embeds. Issue shape::

        { "path": "ui.children[1]", "url": "http://...", "reason": "..." }
    """
    if not isinstance(spec, dict):
        return []
    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    issues: list[dict[str, Any]] = []
    _walk_embed_policy(root, "ui", allowed_hosts, issues)
    return issues


def _walk_embed_policy(
    node: Any,
    path: str,
    allowed_hosts: list[str] | set[str],
    issues: list[dict[str, Any]],
) -> None:
    if not isinstance(node, dict):
        return
    reason = check_embed_node(node, allowed_hosts)
    if reason is not None:
        props = node.get("props")
        url = props.get("url") if isinstance(props, dict) else None
        issues.append({"path": path, "url": url, "reason": reason})
    for key in ("children", "else_children"):
        kids = node.get(key)
        if isinstance(kids, list):
            for i, child in enumerate(kids):
                _walk_embed_policy(child, f"{path}.{key}[{i}]", allowed_hosts, issues)


# ---------------------------------------------------------------------------
# Action-verb allowlist + unwired-live-button detector (2026-05-23).
#
# Two related failure modes the prompt-side "label without source" rule
# (PR #1194) didn't catch:
#
#  A. Invented action verbs. The specialist authors ``on_click:
#     {action: "fetch", endpoint: "/pet/1", target: "pet_rows"}``. The
#     Ripple event dispatcher has no ``fetch`` verb — its default case
#     ``console.warn``s and returns. The button looks live but does
#     nothing.
#
#  B. "Live"-labelled buttons with no real wiring. A Refresh button
#     whose ``on_click`` is empty (or only contains the invented verb
#     above) renders, takes user clicks, and never fetches. Coupled
#     with pre-seeded mock data in ``state.<key>``, the canvas looks
#     alive but is inert.
#
# Both walks here run as siblings to the catalog gate — strict on the
# agent-generation path (raises so the chat agent can retry), logged on
# the human / import path.
# ---------------------------------------------------------------------------

# Canonical action verbs the Ripple event dispatcher understands.
# Source of truth: ``ripple/src/lib/core/event-dispatcher.ts`` — the
# switch in ``dispatch()``. Drift between the two would let an invented
# verb pass spec validation and silently no-op at runtime, which is the
# exact failure we are guarding against; mirror this list whenever the
# dispatcher gains a new case.
_KNOWN_ACTION_VERBS: frozenset[str] = frozenset(
    {
        "set",
        "toggle",
        "push",
        "remove",
        "open",
        "navigate",
        "toast",
        "emit",
        "pin",
        "unpin",
        "api",
        "run_source",
        "call_binding",
        "flow",
        "branch",
        "confirm",
        "validate",
        "delay",
        "invoke",
    }
)

# Button labels (case-insensitive substring match) that promise a live
# fetch. Used by :func:`find_unwired_live_buttons` to know which buttons
# are advertising behavior that must actually be wired.
_LIVE_BUTTON_KEYWORDS: tuple[str, ...] = (
    "refresh",
    "reload",
    "sync",
    "pull",
    "fetch",
    "update from",
    "re-fetch",
    "refetch",
)

# Event-prop names that carry one or more action handlers. Mirrors the
# Ripple node shape — any prop value whose key matches and whose value
# is a handler-shaped object (or list of them) is in scope.
_EVENT_PROP_NAMES: frozenset[str] = frozenset(
    {
        "on_click",
        "onClick",
        "on_change",
        "onChange",
        "on_submit",
        "onSubmit",
        "on_input",
        "onInput",
        "on_blur",
        "onBlur",
        "on_focus",
        "onFocus",
        "on_select",
        "onSelect",
    }
)


def _iter_handlers(value: Any) -> list[dict[str, Any]]:
    """Normalise a handler prop value into the list of handler dicts.

    A handler is either a single object ``{action: ..., ...}`` or a
    list of such objects (action chains). Returns the flat list — any
    non-dict entries are dropped (the catalog already rejected those).
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [h for h in value if isinstance(h, dict)]
    return []


def _walk_action_verbs(node: Any, path: str, issues: list[dict[str, Any]]) -> None:
    """Recursive walk that collects unknown-action-verb violations.

    Each issue: ``{path, prop, action, suggestion}``. ``suggestion`` is
    the nearest known verb (difflib) so the corrective hint can be
    specific. A handler without an ``action`` field is skipped — that
    is the catalog's concern, not the verb gate.
    """
    if isinstance(node, dict):
        # node-level prop scan
        props = node.get("props") if isinstance(node.get("props"), dict) else node
        if isinstance(props, dict):
            for prop_name, prop_value in props.items():
                if prop_name not in _EVENT_PROP_NAMES:
                    continue
                for handler in _iter_handlers(prop_value):
                    verb = handler.get("action")
                    if not isinstance(verb, str) or not verb:
                        # No action field — the renderer treats this as
                        # a no-op too. Flag it: a Refresh button bound
                        # to ``{}`` is identical to ``{action: "fetch"}``
                        # in effect.
                        issues.append(
                            {
                                "path": f"{path}.props.{prop_name}",
                                "prop": prop_name,
                                "action": "<missing>",
                                "suggestion": None,
                            }
                        )
                        continue
                    if verb not in _KNOWN_ACTION_VERBS:
                        matches = difflib.get_close_matches(
                            verb, sorted(_KNOWN_ACTION_VERBS), n=1, cutoff=0.6
                        )
                        issues.append(
                            {
                                "path": f"{path}.props.{prop_name}",
                                "prop": prop_name,
                                "action": verb,
                                "suggestion": matches[0] if matches else None,
                            }
                        )

        for key in ("children", "else_children"):
            kids = node.get(key)
            if isinstance(kids, list):
                for i, child in enumerate(kids):
                    _walk_action_verbs(child, f"{path}.{key}[{i}]", issues)


def validate_action_verbs(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Walk the spec, flag every event-handler whose ``action`` verb
    is not in :data:`_KNOWN_ACTION_VERBS`.

    Returns one issue per offending handler; ``[]`` when ``spec`` is
    not a dict or every handler's verb is recognized. Issue shape::

        {
          "path": "ui.children[2].props.on_click",
          "prop": "on_click",
          "action": "fetch",
          "suggestion": "run_source",   # nearest known verb, or None
        }

    The renderer's event dispatcher silently no-ops unknown verbs (it
    ``console.warn``s and returns), which masks broken buttons as
    "working." Catching this at spec ingest forces the agent to use
    the right verb.
    """
    if not isinstance(spec, dict):
        return []
    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    issues: list[dict[str, Any]] = []
    _walk_action_verbs(root, "ui", issues)
    return issues


def _node_label_text(node: dict[str, Any]) -> str:
    """Return the user-visible text of a node — used to detect Refresh
    / Sync / Fetch labels. Concatenates ``props.label``, ``props.text``,
    ``props.title``, and ``props.aria_label`` if present.
    """
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    parts: list[str] = []
    for key in ("label", "text", "title", "aria_label", "ariaLabel"):
        v = props.get(key) if isinstance(props, dict) else None
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def _looks_live(label: str) -> bool:
    """True when a button's label promises live fetching behaviour."""
    if not label:
        return False
    lo = label.lower()
    return any(kw in lo for kw in _LIVE_BUTTON_KEYWORDS)


def _walk_live_buttons(
    node: Any,
    path: str,
    sources: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    """Recursive walk that flags Refresh-labelled buttons whose
    ``on_click`` is empty or wired to an unrecognized verb / missing
    source.
    """
    if isinstance(node, dict):
        wtype = node.get("type")
        if wtype == "button":
            props = node.get("props") if isinstance(node.get("props"), dict) else {}
            label = _node_label_text(node)
            if _looks_live(label):
                handlers = _iter_handlers(
                    props.get("on_click") if isinstance(props, dict) else None
                )
                if not handlers:
                    issues.append(
                        {
                            "path": f"{path}.props.on_click",
                            "label": label,
                            "reason": "live-labelled button has no on_click handler",
                        }
                    )
                else:
                    # Pick the first handler that's plausibly the
                    # "fetch" — verb in {api, run_source, invoke, flow}.
                    plausible = [
                        h
                        for h in handlers
                        if h.get("action") in {"api", "run_source", "invoke", "flow"}
                    ]
                    if not plausible:
                        # Either all handlers use non-fetching verbs
                        # (set/toggle/etc.) or an unknown verb. Either
                        # way the button doesn't fetch anything.
                        verbs = sorted({str(h.get("action") or "<missing>") for h in handlers})
                        issues.append(
                            {
                                "path": f"{path}.props.on_click",
                                "label": label,
                                "reason": (
                                    "live-labelled button has no fetching action — "
                                    f"verbs present: {verbs}; expected one of "
                                    "['api', 'run_source', 'invoke', 'flow']"
                                ),
                            }
                        )
                    else:
                        for h in plausible:
                            if h.get("action") == "run_source":
                                src_key = h.get("source")
                                if not isinstance(src_key, str) or src_key not in sources:
                                    issues.append(
                                        {
                                            "path": f"{path}.props.on_click",
                                            "label": label,
                                            "reason": (
                                                f"run_source references source "
                                                f"{src_key!r} which is not declared "
                                                "in rippleSpec.sources"
                                            ),
                                        }
                                    )
                            elif h.get("action") == "api":
                                if not isinstance(h.get("url"), str) or not h.get("url"):
                                    issues.append(
                                        {
                                            "path": f"{path}.props.on_click",
                                            "label": label,
                                            "reason": "api handler has no url",
                                        }
                                    )

        for key in ("children", "else_children"):
            kids = node.get(key)
            if isinstance(kids, list):
                for i, child in enumerate(kids):
                    _walk_live_buttons(child, f"{path}.{key}[{i}]", sources, issues)


def find_unwired_live_buttons(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Walk the spec, flag every "live"-labelled button whose on_click
    is empty, references an undeclared source, or uses an action verb
    that doesn't actually fetch.

    Returns one issue per offending button; ``[]`` when ``spec`` is
    not a dict or every Refresh-class button is properly wired. Issue
    shape::

        {
          "path": "ui.children[2].props.on_click",
          "label": "Refresh",
          "reason": "...",
        }

    Pairs with :func:`validate_action_verbs` — the verb check catches
    the dispatcher-level failure (unknown verb), this catches the
    semantic-level failure (verb known, but the wiring is broken for
    the button's purpose).
    """
    if not isinstance(spec, dict):
        return []
    raw_sources = (spec.get("sources") if isinstance(spec.get("sources"), dict) else {}) or {}
    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    issues: list[dict[str, Any]] = []
    _walk_live_buttons(root, "ui", raw_sources, issues)
    return issues


def format_for_prompt(manifest: dict[str, Any]) -> str:
    """Render the manifest as a markdown block for system-prompt injection.

    Returns "" if the manifest has no widgets — caller treats this the same
    as a fetch failure (i.e. falls back to kb search).
    """
    widgets = manifest.get("widgets") or []
    if not widgets:
        return ""

    lines: list[str] = []
    lines.append("\n\n<ripple-widget-reference>")

    # The envelope contract goes FIRST. The agent's most expensive failure
    # mode is inventing the wrong top-level field name (`root`, `tree`,
    # `view`) for the renderable tree — anchoring `ui` here, before the
    # widget catalog, prevents that. Per-widget examples below show
    # node-shape only; this section shows how nodes wrap into a spec.
    spec_envelope = manifest.get("spec")
    if isinstance(spec_envelope, dict):
        ui_field = spec_envelope.get("uiField") or "ui"
        state_field = spec_envelope.get("stateField") or "state"
        envelope_version = spec_envelope.get("version") or "1.0"
        aliases = spec_envelope.get("aliasesNotAllowed") or []
        description = spec_envelope.get("description") or ""
        example = spec_envelope.get("example")

        lines.append("## Spec envelope")
        lines.append("")
        if description:
            lines.append(description)
            lines.append("")
        lines.append(
            f'Top-level shape: `{{ "version": "{envelope_version}", '
            f'"{state_field}": {{...}}, "{ui_field}": {{...}} }}`.'
        )
        if aliases:
            alias_list = ", ".join(f"`{a}`" for a in aliases)
            lines.append(
                f"Do NOT name the renderable tree {alias_list}. The field is `{ui_field}` exactly."
            )
        if example:
            lines.append("")
            lines.append("**Envelope example:**")
            lines.append("```json")
            lines.append(json.dumps(example, indent=2))
            lines.append("```")
        lines.append("")
        lines.append("## Widget catalog")
        lines.append("")

    lines.append(
        "The following Ripple widgets are available. Use these props, types, "
        "and example specs when building the UI."
    )
    lines.append("")

    for w in widgets:
        wtype = w.get("type", "?")
        category = w.get("category", "?")
        desc = w.get("description", "")
        lines.append(f"### `{wtype}` ({category})")
        lines.append(desc)
        props = w.get("props") or {}
        if props:
            lines.append("")
            lines.append("**Props:**")
            for name, spec in props.items():
                req = " *(required)*" if spec.get("required") else ""
                lines.append(
                    f"- `{name}`: `{spec.get('type', '?')}`{req} — {spec.get('description', '')}"
                )
        example = w.get("example")
        if example:
            lines.append("")
            lines.append("**Example:**")
            lines.append("```json")
            lines.append(json.dumps(example, indent=2))
            lines.append("```")
        lines.append("")

    lines.append("</ripple-widget-reference>")
    return "\n".join(lines)
