"""Verified domains for workspaces (Wave 3 Task 12).

Admin claims a domain, gets a TXT verification token, drops it on the
domain's DNS, then calls ``verify_domain`` which does an async DNS TXT
lookup. Once verified + auto_join, new registrants with that email
domain are auto-joined by ``UserManager.on_after_register``.

Sole writer of the ``Workspace.verified_domains`` embedded list.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime

import dns.asyncresolver
import dns.exception
from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.models.workspace import VerifiedDomain as _VerifiedDomainDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.workspace.domain import VerifiedDomain

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")


def normalize_domain(raw: str) -> str:
    """Lowercase, strip whitespace, strip leading ``@``/``http(s)://``, validate shape."""
    if not raw:
        raise ValidationError("domain.invalid", "domain is required")
    s = raw.strip().lower()
    # Drop scheme + path bits if a user pastes a URL.
    s = re.sub(r"^https?://", "", s)
    s = s.lstrip("@")
    s = s.split("/", 1)[0]
    s = s.split(":", 1)[0]  # drop port
    if " " in s or "\t" in s or "@" in s or not _DOMAIN_RE.match(s):
        raise ValidationError("domain.invalid", f"'{raw}' is not a valid domain")
    return s


def mint_verification_token() -> str:
    return f"paw-verify={secrets.token_hex(16)}"


async def _fetch_workspace(workspace_id: str) -> _WorkspaceDoc:
    try:
        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    except Exception as exc:
        raise NotFound("workspace", workspace_id) from exc
    if doc is None or doc.deleted_at is not None:
        raise NotFound("workspace", workspace_id)
    return doc


def _find_entry(doc: _WorkspaceDoc, domain: str) -> _VerifiedDomainDoc | None:
    for entry in doc.verified_domains:
        if entry.domain == domain:
            return entry
    return None


def _to_domain(doc: _VerifiedDomainDoc) -> VerifiedDomain:
    return VerifiedDomain(
        domain=doc.domain,
        verification_token=doc.verification_token,
        verified=doc.verified,
        verified_at=doc.verified_at,
        auto_join=doc.auto_join,
        created_at=doc.created_at,
    )


async def add_domain(workspace_id: str, domain: str) -> VerifiedDomain:
    """Admin path. Idempotent: if the domain is already present, returns
    the existing entry (with its original token) rather than minting a new one.
    """
    normalized = normalize_domain(domain)
    doc = await _fetch_workspace(workspace_id)
    existing = _find_entry(doc, normalized)
    if existing is not None:
        return _to_domain(existing)

    entry = _VerifiedDomainDoc(
        domain=normalized,
        verification_token=mint_verification_token(),
    )
    doc.verified_domains.append(entry)
    await doc.save()

    await audit_service.record(
        workspace_id,
        "system",
        "domain.add",
        target_type="workspace",
        target_id=workspace_id,
        metadata={"domain": normalized},
    )
    return _to_domain(entry)


async def remove_domain(workspace_id: str, domain: str) -> None:
    normalized = normalize_domain(domain)
    doc = await _fetch_workspace(workspace_id)
    before = len(doc.verified_domains)
    doc.verified_domains = [d for d in doc.verified_domains if d.domain != normalized]
    if len(doc.verified_domains) == before:
        raise NotFound("domain", normalized)
    await doc.save()

    await audit_service.record(
        workspace_id,
        "system",
        "domain.remove",
        target_type="workspace",
        target_id=workspace_id,
        metadata={"domain": normalized},
    )


async def list_domains(workspace_id: str) -> list[VerifiedDomain]:
    doc = await _fetch_workspace(workspace_id)
    return [_to_domain(e) for e in doc.verified_domains]


async def verify_domain(workspace_id: str, domain: str) -> VerifiedDomain:
    """Perform async DNS TXT lookup; flip ``verified`` if the token matches."""
    normalized = normalize_domain(domain)
    doc = await _fetch_workspace(workspace_id)
    entry = _find_entry(doc, normalized)
    if entry is None:
        raise NotFound("domain", normalized)

    expected = entry.verification_token
    matched = False
    try:
        answer = await dns.asyncresolver.resolve(normalized, "TXT")
    except (dns.exception.DNSException, Exception):  # noqa: BLE001
        # Network / NXDOMAIN / NoAnswer all collapse to "not found" — the
        # caller-facing distinction is "matched yes/no", not "why not".
        answer = []

    # dnspython returns rrsets where each record has `.strings = [b"..."]`
    # (one record can also span multiple chunks that must be concatenated).
    for rr in answer:
        strings = getattr(rr, "strings", None) or []
        joined = b"".join(strings).decode("utf-8", errors="ignore").strip().strip('"')
        if joined == expected:
            matched = True
            break

    if not matched:
        raise Forbidden(
            "domain.txt_not_found",
            f"TXT record matching '{expected}' not found on {normalized}",
        )

    entry.verified = True
    entry.verified_at = datetime.now(UTC)
    await doc.save()

    await audit_service.record(
        workspace_id,
        "system",
        "domain.verify",
        target_type="workspace",
        target_id=workspace_id,
        metadata={"domain": normalized},
    )
    return _to_domain(entry)


async def set_auto_join(workspace_id: str, domain: str, auto_join: bool) -> VerifiedDomain:
    normalized = normalize_domain(domain)
    doc = await _fetch_workspace(workspace_id)
    entry = _find_entry(doc, normalized)
    if entry is None:
        raise NotFound("domain", normalized)
    entry.auto_join = auto_join
    await doc.save()

    await audit_service.record(
        workspace_id,
        "system",
        "domain.update",
        target_type="workspace",
        target_id=workspace_id,
        metadata={"domain": normalized, "auto_join": auto_join},
    )
    return _to_domain(entry)


async def find_workspace_by_verified_domain(email_domain: str) -> _WorkspaceDoc | None:
    """Used by the post-register hook. Returns one matching workspace or None."""
    if not email_domain:
        return None
    return await _WorkspaceDoc.find_one(
        {
            "deleted_at": None,
            "verified_domains": {
                "$elemMatch": {
                    "domain": email_domain.lower(),
                    "verified": True,
                    "auto_join": True,
                }
            },
        }
    )


__all__ = [
    "add_domain",
    "find_workspace_by_verified_domain",
    "list_domains",
    "mint_verification_token",
    "normalize_domain",
    "remove_domain",
    "set_auto_join",
    "verify_domain",
]
