# src/pocketpaw/skills/api_skill_builder.py
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — turns an OpenAPI /
# Swagger spec for a pocket's configured backend into a loadable SKILL.md
# so the pocket-authoring agent writes correct `sources` / `actions`
# (real relative paths, real response shapes) instead of hallucinating
# endpoints. Pure OSS module — no `pocketpaw_ee` import; the SkillLoader
# in `pocketpaw.skills.loader` reads what `install_api_skill` writes.
"""Build a per-backend API skill from an OpenAPI / Swagger spec.

A pocket can be bound to one external backend (RFC 04). When the
backend exposes an OpenAPI document, ``install_api_skill`` converts it
to a SKILL.md under ``~/.pocketpaw/skills/api-<domain_slug>/`` — one of
the three roots the runtime ``SkillLoader`` scans. The pocket-specialist
runtime then loads that SKILL.md and splices an endpoint reference into
the authoring prompt so the agent uses real paths.

Two public entry points:

- ``parse_openapi_to_skill_md`` — pure transform: an OpenAPI dict in, a
  SKILL.md string out. **Content-stable** — repeated calls on the same
  spec produce byte-identical output (paths are ``sorted()``), so the
  SHA-based skip logic of any mirror installer stays a no-op.
- ``install_api_skill`` — accepts a file path / URL / parsed dict,
  validates it, writes the SKILL.md, audit-logs the install, and
  returns the domain slug.

This module never imports the enterprise package — the import-linter
``OSS core may not import from EE`` contract covers it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# A spec file larger than this is rejected — an OpenAPI document this big
# is almost certainly not what the user meant to upload, and parsing it
# would blow the authoring prompt's token budget anyway.
_MAX_SPEC_BYTES = 2 * 1024 * 1024  # 2 MB

# Hard cap on endpoints emitted into a single skill. A spec with more
# operations than this is truncated with an explicit note — the agent
# only ever needs a representative slice, and the prompt has a budget.
_MAX_ENDPOINTS = 200

# Per-operation field caps — keep each endpoint reference compact.
_SUMMARY_MAX_CHARS = 120
_MAX_RESPONSE_FIELDS = 10


# ---------------------------------------------------------------------------
# Spec-shape helpers — handle OpenAPI 3.x AND Swagger 2.x
# ---------------------------------------------------------------------------


def _slugify_domain(domain: str) -> str:
    """Turn a hostname (or any string) into a filesystem-safe slug.

    ``api.example.com`` → ``api-example-com``. Dots and any non
    ``[a-z0-9-]`` run collapse to a single hyphen so the slug is a
    clean directory name. An empty / unusable input falls back to
    ``backend``.
    """
    lowered = (domain or "").strip().lower()
    out: list[str] = []
    prev_hyphen = False
    for ch in lowered:
        if ch.isalnum():
            out.append(ch)
            prev_hyphen = False
        else:
            # Collapse any run of separators into a single hyphen.
            if not prev_hyphen:
                out.append("-")
                prev_hyphen = True
    slug = "".join(out).strip("-")
    return slug or "backend"


def _backend_domain_from_spec(spec: dict[str, Any]) -> str | None:
    """Best-effort backend hostname from an OpenAPI / Swagger spec.

    OpenAPI 3.x carries ``servers[0].url`` (a full or relative URL);
    Swagger 2.x carries ``host`` (a bare hostname, optionally with a
    port). Returns the hostname only, or ``None`` when the spec names
    no server.
    """
    # OpenAPI 3.x — servers: [{url: ...}]
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str) and url.strip():
                parsed = urlparse(url if "://" in url else f"//{url}")
                host = parsed.hostname or url.strip()
                return host or None

    # Swagger 2.x — host: "api.example.com[:port]"
    swagger_host = spec.get("host")
    if isinstance(swagger_host, str) and swagger_host.strip():
        # Strip a :port suffix if present.
        return swagger_host.strip().split(":", 1)[0] or None

    return None


def _resolve_ref(spec: dict[str, Any], node: Any, _depth: int = 0) -> Any:
    """Resolve a single local ``$ref`` against the spec.

    Only local refs (``#/components/...`` for 3.x, ``#/definitions/...``
    for 2.x) are followed, one hop, with a small depth guard so a
    self-referential schema cannot loop forever. A non-ref node is
    returned unchanged; an unresolvable ref yields ``{}``.
    """
    if _depth > 5 or not isinstance(node, dict):
        return node if isinstance(node, dict) else {}
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(target, dict) or part not in target:
            return {}
        target = target[part]
    return _resolve_ref(spec, target, _depth + 1)


def _schema_top_level_props(spec: dict[str, Any], schema: Any) -> list[str]:
    """Top-level property names of a (possibly $ref'd) JSON schema.

    When the schema describes an array, the item schema's properties are
    used instead — a list endpoint's "shape" is the shape of one row.
    Capped at ``_MAX_RESPONSE_FIELDS`` and sorted for content stability.
    """
    schema = _resolve_ref(spec, schema)
    if not isinstance(schema, dict):
        return []
    if schema.get("type") == "array" or "items" in schema:
        schema = _resolve_ref(spec, schema.get("items") or {})
        if not isinstance(schema, dict):
            return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    return sorted(props.keys())[:_MAX_RESPONSE_FIELDS]


def _response_fields(spec: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    """Key fields of an operation's 200 (or 2xx) response.

    Handles both response shapes:
    - OpenAPI 3.x — ``responses.200.content.<mime>.schema``
    - Swagger 2.x — ``responses.200.schema``
    """
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return []
    # Prefer 200; fall back to the first 2xx key present.
    resp = None
    for code in ("200", 200, "201", 201):
        if code in responses:
            resp = responses[code]
            break
    if resp is None:
        for key in sorted(str(k) for k in responses):
            if key.startswith("2"):
                resp = responses[key] if key in responses else responses.get(int(key))
                break
    resp = _resolve_ref(spec, resp)
    if not isinstance(resp, dict):
        return []

    # Swagger 2.x — schema sits directly on the response.
    if "schema" in resp:
        return _schema_top_level_props(spec, resp["schema"])

    # OpenAPI 3.x — schema is nested under content.<mime>.schema.
    content = resp.get("content")
    if isinstance(content, dict):
        for mime in sorted(content):
            entry = content[mime]
            if isinstance(entry, dict) and "schema" in entry:
                return _schema_top_level_props(spec, entry["schema"])
    return []


def _request_params(spec: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    """Key request inputs of an operation — query params + required body.

    Query params come from the ``parameters`` list (``in: query``).
    Required body fields come from the request body schema's
    ``required`` list — OpenAPI 3.x ``requestBody`` or Swagger 2.x's
    ``in: body`` parameter. Sorted for content stability.
    """
    params: list[str] = []

    parameters = operation.get("parameters")
    if isinstance(parameters, list):
        for raw in parameters:
            param = _resolve_ref(spec, raw)
            if not isinstance(param, dict):
                continue
            loc = param.get("in")
            name = param.get("name")
            if loc == "query" and isinstance(name, str):
                params.append(f"{name} (query)")
            elif loc == "body":
                # Swagger 2.x body parameter — required fields of its schema.
                body_schema = _resolve_ref(spec, param.get("schema") or {})
                if isinstance(body_schema, dict):
                    for field in body_schema.get("required") or []:
                        if isinstance(field, str):
                            params.append(f"{field} (body, required)")

    # OpenAPI 3.x requestBody — required fields of the body schema.
    request_body = _resolve_ref(spec, operation.get("requestBody") or {})
    if isinstance(request_body, dict):
        content = request_body.get("content")
        if isinstance(content, dict):
            for mime in sorted(content):
                entry = content[mime]
                if isinstance(entry, dict):
                    body_schema = _resolve_ref(spec, entry.get("schema") or {})
                    if isinstance(body_schema, dict):
                        for field in body_schema.get("required") or []:
                            if isinstance(field, str):
                                params.append(f"{field} (body, required)")
                    break  # one representative mime is enough

    # De-dup while keeping a stable order.
    return sorted(set(params))


def _operation_group(operation: dict[str, Any], path: str) -> str:
    """Group key for an operation — first tag, else first path segment.

    A tagged operation groups under its first tag; an untagged one
    groups under the first non-empty path segment (``/contacts/{id}``
    → ``contacts``). Falls back to ``general``.
    """
    tags = operation.get("tags")
    if isinstance(tags, list) and tags and isinstance(tags[0], str) and tags[0].strip():
        return tags[0].strip()
    for segment in path.split("/"):
        seg = segment.strip()
        if seg and not (seg.startswith("{") and seg.endswith("}")):
            return seg
    return "general"


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


# ---------------------------------------------------------------------------
# Public — pure transform
# ---------------------------------------------------------------------------


def parse_openapi_to_skill_md(spec: dict[str, Any], *, backend_domain: str) -> str:
    """Render an OpenAPI / Swagger spec as a SKILL.md string.

    Walks ``spec["paths"]``, groups each operation by first tag (else
    first path segment), and emits per operation: method + path,
    a trimmed summary, key request params, and key 200-response fields.

    Handles OpenAPI 3.x and Swagger 2.x response / body shapes. Caps the
    output at ``_MAX_ENDPOINTS`` operations with a truncation note.

    **Content-stable** — ``paths`` keys are ``sorted()`` and every
    derived list is sorted, so two calls on the same spec produce
    byte-identical output. A mirror installer's SHA-256 skip logic
    relies on this.

    Args:
        spec: A parsed OpenAPI / Swagger document.
        backend_domain: The backend hostname — recorded in the
            frontmatter ``metadata.backend_domain`` and used to derive
            the skill ``name``.

    Returns:
        A complete SKILL.md string (YAML frontmatter + markdown body).
    """
    slug = _slugify_domain(backend_domain)
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        paths = {}

    spec_version = spec.get("openapi") or spec.get("swagger") or "unknown"
    title = "API"
    info = spec.get("info")
    if isinstance(info, dict) and isinstance(info.get("title"), str):
        title = info["title"].strip() or "API"

    # ---- collect operations, grouped, deterministically ordered ----
    groups: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    total = 0
    truncated = False
    for path in sorted(paths.keys()):
        path_item = paths[path]
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            if total >= _MAX_ENDPOINTS:
                truncated = True
                break
            group = _operation_group(operation, path)
            groups.setdefault(group, []).append((method.upper(), path, operation))
            total += 1
        if truncated:
            break

    # ---- frontmatter ----
    frontmatter_lines = [
        "---",
        f"name: api-{slug}",
        (
            f"description: API endpoint reference for the {title} backend "
            f"({backend_domain}). Use these paths and response shapes when "
            "authoring pocket sources and actions."
        ),
        "user-invocable: false",
        "metadata:",
        f"  backend_domain: {backend_domain}",
        f"  spec_version: {spec_version}",
        f"  endpoint_count: {total}",
        "---",
    ]

    # ---- body ----
    body: list[str] = [
        f"# {title} — Backend API Reference",
        "",
        (
            f"Endpoint reference for the backend at `{backend_domain}`. "
            "Every `path` below is RELATIVE to the pocket's configured "
            "backend base URL — author `sources` / `actions` against "
            "these paths, never invent endpoints, never use an absolute "
            "URL."
        ),
        "",
    ]
    if total == 0:
        body.append("_This spec declared no operations._")
    for group in sorted(groups.keys()):
        body.append(f"## {group}")
        body.append("")
        for method, path, operation in groups[group]:
            body.append(f"### `{method} {path}`")
            summary = operation.get("summary") or operation.get("description") or ""
            if isinstance(summary, str) and summary.strip():
                trimmed = summary.strip().replace("\n", " ")
                if len(trimmed) > _SUMMARY_MAX_CHARS:
                    trimmed = trimmed[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"
                body.append(trimmed)
            req = _request_params(spec, operation)
            if req:
                body.append(f"- Request params: {', '.join(req)}")
            resp = _response_fields(spec, operation)
            if resp:
                body.append(f"- Response fields (200): {', '.join(resp)}")
            body.append("")
    if truncated:
        body.append(
            f"_Reference truncated at {_MAX_ENDPOINTS} endpoints — the "
            "backend exposes more. Ask the user for the specific endpoint "
            "if the one you need is not listed above._"
        )
        body.append("")

    return "\n".join(frontmatter_lines) + "\n\n" + "\n".join(body).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Public — install
# ---------------------------------------------------------------------------


def _parse_spec_text(text: str) -> dict[str, Any]:
    """Parse a spec document — JSON first, then YAML.

    OpenAPI documents ship as either JSON or YAML; JSON is also valid
    YAML, so a JSON-first attempt keeps the common case fast. Raises
    ``ValueError`` when neither parser yields a dict.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            import yaml  # type: ignore[import-untyped]

            parsed = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean ValueError
            raise ValueError(f"spec is neither valid JSON nor YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("spec did not parse to a JSON object")
    return parsed


def _load_spec(openapi_source: Path | str | dict[str, Any]) -> dict[str, Any]:
    """Resolve an ``openapi_source`` argument to a parsed spec dict.

    Accepts an already-parsed dict (passed through), a ``Path`` (read
    from disk, size-checked), or a ``str`` that is either an
    ``http(s)://`` URL (fetched) or a filesystem path (read).
    """
    if isinstance(openapi_source, dict):
        return openapi_source

    if isinstance(openapi_source, str) and openapi_source.lower().startswith(
        ("http://", "https://")
    ):
        import httpx

        resp = httpx.get(openapi_source, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        raw = resp.content
        if len(raw) > _MAX_SPEC_BYTES:
            raise ValueError(f"spec is {len(raw)} bytes — exceeds the {_MAX_SPEC_BYTES}-byte limit")
        return _parse_spec_text(raw.decode("utf-8", errors="replace"))

    # A filesystem path (Path object or path-like string).
    path = Path(openapi_source)
    if not path.is_file():
        raise ValueError(f"spec file not found: {path}")
    size = path.stat().st_size
    if size > _MAX_SPEC_BYTES:
        raise ValueError(f"spec file is {size} bytes — exceeds the {_MAX_SPEC_BYTES}-byte limit")
    return _parse_spec_text(path.read_text(encoding="utf-8", errors="replace"))


def install_api_skill(
    openapi_source: Path | str | dict[str, Any],
    *,
    name: str | None = None,
    dest_dir: Path | None = None,
) -> str:
    """Install a backend's API as a loadable skill and return its slug.

    Resolves ``openapi_source`` (a parsed dict, a file path, or a URL)
    to a spec, validates it carries a ``paths`` key, derives the backend
    domain, renders a SKILL.md via ``parse_openapi_to_skill_md``, and
    writes it to ``<dest_dir>/api-<domain_slug>/SKILL.md`` (default
    ``dest_dir`` is ``~/.pocketpaw/skills``). The install is audit-logged
    at INFO.

    Args:
        openapi_source: A parsed OpenAPI dict, a path to a ``.json`` /
            ``.yaml`` spec file, or an ``http(s)`` URL to fetch.
        name: Backend display name — used to derive the domain slug
            when the spec itself names no server.
        dest_dir: Override the skills root — for tests. Defaults to
            ``~/.pocketpaw/skills``.

    Returns:
        The domain slug — e.g. ``api-example-com`` — naming the skill
        directory and the SKILL.md ``name`` field.

    Raises:
        ValueError: The spec is too large, unparseable, or carries no
            ``paths`` key.
    """
    spec = _load_spec(openapi_source)

    if not isinstance(spec.get("paths"), dict):
        raise ValueError("spec has no `paths` object — not a usable OpenAPI / Swagger document")

    # Derive the backend domain: prefer the spec's own server, fall back
    # to the caller-supplied name.
    domain = _backend_domain_from_spec(spec) or (name or "").strip() or "backend"
    domain_slug = _slugify_domain(domain)

    skill_md = parse_openapi_to_skill_md(spec, backend_domain=domain)

    root = dest_dir if dest_dir is not None else (Path.home() / ".pocketpaw" / "skills")
    skill_dir = root / f"api-{domain_slug}"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    logger.info(
        "api_skill_builder: installed API skill api-%s (backend_domain=%s) at %s",
        domain_slug,
        domain,
        skill_dir,
    )

    return f"api-{domain_slug}"


__all__ = ["parse_openapi_to_skill_md", "install_api_skill"]
