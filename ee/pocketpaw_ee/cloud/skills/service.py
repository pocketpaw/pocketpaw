# service.py — Cloud Skills entity business logic.
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — validates an
# uploaded OpenAPI / Swagger document and installs it as a per-backend
# API skill by delegating to the OSS pocketpaw.skills.api_skill_builder.
# No Beanie writes — the skill is a SKILL.md on the local skills dir;
# the install is audit-logged with workspace_id + user_id (Rule 9 has a
# no-event note: skills are filesystem-local, not a DB row).
# Updated: 2026-05-23 — add ``install_api_doc_from_url`` for the
# JSON-body URL-fetch surface. Lower friction than upload because most
# production APIs publish their OpenAPI at a stable URL. The fetch
# runs through the same SSRF guard the read-source executor uses
# (https-only, hostname resolves to a public IP, size + timeout caps).
from __future__ import annotations

import json
import logging
import urllib.parse

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.skills.domain import ApiDocFromUrlInstall, ApiDocInstall
from pocketpaw_ee.cloud.skills.dto import InstallApiDocResponse

logger = logging.getLogger(__name__)

# Accepted spec file extensions and the hard size cap. The cap mirrors
# ``api_skill_builder._MAX_SPEC_BYTES`` — checked here too so an
# oversized upload is rejected before the builder touches it.
_ALLOWED_EXTENSIONS = (".json", ".yaml", ".yml")
_MAX_SPEC_BYTES = 2 * 1024 * 1024  # 2 MB

# Outbound fetch tuning for ``install_api_doc_from_url``. The connect
# + read timeout matches the read-source executor's posture; if the
# upstream takes more than 15s to return an OpenAPI, the user gets a
# 422 with ``skills.api_doc.fetch_timeout`` so they can retry instead
# of staring at a stalled request.
_URL_FETCH_TIMEOUT_SECONDS = 15.0


def _audit_api_skill_install(
    *,
    workspace_id: str,
    user_id: str,
    slug: str,
    filename: str,
    source: str = "upload",
) -> None:
    """Write an audit-log entry for an API-skill install.

    Logs the workspace, the actor, the resulting slug, the upload
    filename (or the URL for the URL-fetch variant, query-stripped),
    and a ``source`` discriminator (``upload`` | ``url``). No spec
    contents, no credentials. Audit failures must never break the
    install, so the call is wrapped.
    """
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor=user_id,
                action="skills.api_doc.install",
                target=slug,
                status="success",
                category="skills_config",
                workspace_id=workspace_id,
                slug=slug,
                filename=filename,
                source=source,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the install
        logger.warning("api-skill install audit-log write failed", exc_info=True)


def _strip_url_query(url: str) -> str:
    """Drop query string, userinfo credentials, and fragment from a URL
    for audit logging.

    Some upstreams ship API keys in URL params OR userinfo
    (``https://user:apikey@host/...``). Logging the bare ``scheme://host[:port]/path``
    keeps the audit trail useful without persisting a token.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        safe_netloc = parts.hostname or ""
        if parts.port:
            safe_netloc = f"{safe_netloc}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, safe_netloc, parts.path, "", ""))
    except Exception:  # noqa: BLE001 — fallback for malformed input
        return url.split("?", 1)[0].split("#", 1)[0]


async def install_api_doc(
    workspace_id: str, user_id: str, body: ApiDocInstall | dict
) -> InstallApiDocResponse:
    """Install an uploaded OpenAPI / Swagger document as an API skill.

    Validates the file extension and size, parses the spec, delegates
    to ``pocketpaw.skills.api_skill_builder.install_api_skill`` to write
    the SKILL.md, audit-logs the install, and returns the skill slug.

    Args:
        workspace_id: Active workspace — tenancy context.
        user_id: Authenticated caller — actor in the audit entry.
        body: An ``ApiDocInstall`` (or wire dict) carrying the spec
            bytes, the upload filename, and the optional backend name.

    Returns:
        ``InstallApiDocResponse`` with ``ok`` and the installed ``slug``.

    Raises:
        ValidationError: The file extension is unsupported, the file
            exceeds the 2 MB cap, the spec is unparseable, or it carries
            no ``paths`` object.
    """
    # Re-validate at entry (Rule 6) — FastAPI parsed the multipart body,
    # but internal callers (jobs, tests) re-parse here.
    body = ApiDocInstall.model_validate(body)

    filename = body.filename or ""
    if not filename.lower().endswith(_ALLOWED_EXTENSIONS):
        raise ValidationError(
            "skills.api_doc.bad_extension",
            f"spec file must be one of {', '.join(_ALLOWED_EXTENSIONS)}",
        )

    if len(body.spec_bytes) > _MAX_SPEC_BYTES:
        raise ValidationError(
            "skills.api_doc.too_large",
            f"spec file is {len(body.spec_bytes)} bytes — exceeds the 2 MB limit",
        )

    parsed = _parse_spec_text(body.spec_bytes.decode("utf-8", errors="replace"))

    slug = _install_parsed_spec(parsed, name=body.name)

    _audit_api_skill_install(
        workspace_id=workspace_id,
        user_id=user_id,
        slug=slug,
        filename=filename,
        source="upload",
    )
    logger.info(
        "skills.api_doc: installed %s from upload for workspace=%s actor=%s",
        slug,
        workspace_id,
        user_id,
    )
    # no-event: an API skill is a filesystem-local SKILL.md, not a DB
    # row — there is no downstream search index / soul handler to keep
    # in sync, so no bus event is emitted.
    return InstallApiDocResponse(ok=True, slug=slug)


async def install_api_doc_from_url(
    workspace_id: str, user_id: str, body: ApiDocFromUrlInstall | dict
) -> InstallApiDocResponse:
    """Fetch an OpenAPI / Swagger document from a URL and install it.

    Cloud-safe URL fetch path: enforces https-only, runs the SSRF guard
    against the resolved hostname (rejects loopback, link-local,
    private, and reserved IPs the same way the read-source executor
    does), caps the response body at 2 MB, then hands the parsed dict
    to the same installer the upload path uses.

    Args:
        workspace_id: Active workspace — tenancy context.
        user_id: Authenticated caller — actor in the audit entry.
        body: An ``ApiDocFromUrlInstall`` (or wire dict) carrying the
            spec URL and an optional backend display name.

    Returns:
        ``InstallApiDocResponse`` with ``ok`` and the installed ``slug``.

    Raises:
        ValidationError: The URL is not https, resolves to a private
            host, exceeds the size cap, returns a non-2xx status, is
            unparseable, or carries no ``paths`` object.
    """
    # Re-validate at entry (Rule 6).
    body = ApiDocFromUrlInstall.model_validate(body)

    url = str(body.url)
    parts = urllib.parse.urlsplit(url)

    if parts.scheme.lower() != "https":
        raise ValidationError(
            "skills.api_doc.bad_scheme",
            "spec URL must be https — http and other schemes are not allowed",
        )

    hostname = parts.hostname or ""
    if not hostname:
        raise ValidationError(
            "skills.api_doc.bad_url",
            "spec URL must include a hostname",
        )

    # SSRF guard — resolve and reject internal / loopback / private IPs.
    # Reuses the same guard the read-source executor applies so the
    # posture matches across the two HTTP-fetching surfaces.
    from pocketpaw_ee.cloud.pockets._http_guard import _assert_host_external, _GuardError

    try:
        await _assert_host_external(hostname)
    except _GuardError as exc:
        raise ValidationError("skills.api_doc.bad_host", str(exc)) from exc

    # Fetch with a hard timeout + size cap. We read the response body
    # once and pass the parsed dict to the installer — never the URL
    # again — so the installer's own httpx fetch never runs (would
    # double the size budget and bypass our SSRF check on a DNS
    # rebind between calls).
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=_URL_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        # Strip the URL of query params before logging — some upstreams
        # ship credentials there even when the spec itself is public.
        logger.warning(
            "skills.api_doc.from_url: fetch %s failed: %s",
            _strip_url_query(url),
            type(exc).__name__,
        )
        raise ValidationError(
            "skills.api_doc.fetch_failed",
            "could not fetch the spec from that URL",
        ) from exc

    if 300 <= resp.status_code < 400:
        # Redirects are off so the agent that authored the URL gets a
        # signal to use the final location directly.
        raise ValidationError(
            "skills.api_doc.fetch_redirect",
            f"spec URL returned a redirect ({resp.status_code}) — pass the final URL directly",
        )
    if resp.status_code >= 400:
        raise ValidationError(
            "skills.api_doc.fetch_failed",
            f"spec URL returned status {resp.status_code}",
        )

    raw = resp.content
    if len(raw) > _MAX_SPEC_BYTES:
        raise ValidationError(
            "skills.api_doc.too_large",
            f"spec response is {len(raw)} bytes — exceeds the 2 MB limit",
        )

    parsed = _parse_spec_text(raw.decode("utf-8", errors="replace"))

    slug = _install_parsed_spec(parsed, name=body.name)

    _audit_api_skill_install(
        workspace_id=workspace_id,
        user_id=user_id,
        slug=slug,
        filename=_strip_url_query(url),
        source="url",
    )
    logger.info(
        "skills.api_doc: installed %s from url for workspace=%s actor=%s",
        slug,
        workspace_id,
        user_id,
    )
    return InstallApiDocResponse(ok=True, slug=slug)


def _parse_spec_text(text: str) -> dict:
    """Parse a JSON-or-YAML spec into a dict, or raise a ValidationError.

    Tries JSON first (most OpenAPI specs ship as JSON) then YAML.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            import yaml  # type: ignore[import-untyped]

            parsed = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 — surfaced as a ValidationError
            raise ValidationError(
                "skills.api_doc.unparseable",
                "spec is neither valid JSON nor YAML",
            ) from exc

    if not isinstance(parsed, dict):
        raise ValidationError(
            "skills.api_doc.unparseable",
            "spec did not parse to a JSON object",
        )
    return parsed


def _install_parsed_spec(parsed: dict, *, name: str | None) -> str:
    """Delegate to the OSS installer; map its ValueErrors to 422."""
    from pocketpaw.skills.api_skill_builder import install_api_skill

    try:
        return install_api_skill(parsed, name=name)
    except ValueError as exc:
        # api_skill_builder rejects a spec with no `paths` / one too
        # large — map it to a 422 the dashboard surfaces inline.
        raise ValidationError("skills.api_doc.invalid_spec", str(exc)) from exc


__all__ = ["install_api_doc", "install_api_doc_from_url"]
