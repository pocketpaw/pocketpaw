# service.py — Cloud Skills entity business logic.
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — validates an
# uploaded OpenAPI / Swagger document and installs it as a per-backend
# API skill by delegating to the OSS pocketpaw.skills.api_skill_builder.
# No Beanie writes — the skill is a SKILL.md on the local skills dir;
# the install is audit-logged with workspace_id + user_id (Rule 9 has a
# no-event note: skills are filesystem-local, not a DB row).
from __future__ import annotations

import json
import logging

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.skills.domain import ApiDocInstall
from pocketpaw_ee.cloud.skills.dto import InstallApiDocResponse

logger = logging.getLogger(__name__)

# Accepted spec file extensions and the hard size cap. The cap mirrors
# ``api_skill_builder._MAX_SPEC_BYTES`` — checked here too so an
# oversized upload is rejected before the builder touches it.
_ALLOWED_EXTENSIONS = (".json", ".yaml", ".yml")
_MAX_SPEC_BYTES = 2 * 1024 * 1024  # 2 MB


def _audit_api_skill_install(*, workspace_id: str, user_id: str, slug: str, filename: str) -> None:
    """Write an audit-log entry for an API-skill install.

    Logs the workspace, the actor, the resulting slug, and the upload
    filename — no spec contents, no credentials. Audit failures must
    never break the install, so the call is wrapped.
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
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the install
        logger.warning("api-skill install audit-log write failed", exc_info=True)


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

    text = body.spec_bytes.decode("utf-8", errors="replace")
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

    from pocketpaw.skills.api_skill_builder import install_api_skill

    try:
        slug = install_api_skill(parsed, name=body.name)
    except ValueError as exc:
        # api_skill_builder rejects a spec with no `paths` / one too
        # large — map it to a 422 the dashboard surfaces inline.
        raise ValidationError("skills.api_doc.invalid_spec", str(exc)) from exc

    _audit_api_skill_install(
        workspace_id=workspace_id,
        user_id=user_id,
        slug=slug,
        filename=filename,
    )
    logger.info(
        "skills.api_doc: installed %s for workspace=%s actor=%s",
        slug,
        workspace_id,
        user_id,
    )
    # no-event: an API skill is a filesystem-local SKILL.md, not a DB
    # row — there is no downstream search index / soul handler to keep
    # in sync, so no bus event is emitted.
    return InstallApiDocResponse(ok=True, slug=slug)


__all__ = ["install_api_doc"]
