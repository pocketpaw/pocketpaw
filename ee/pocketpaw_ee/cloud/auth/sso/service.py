"""OIDC SSO orchestration — upsert/get/delete config, login flow, JIT.

State storage uses the shared Redis client (one-shot consume: GET then
DEL) with a 10-minute TTL. The state key payload pins ``workspace_id``
+ optional PKCE ``code_verifier`` so the callback rebinds to the right
workspace + verifier without trusting any query-string value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.auth.sso import crypto, oidc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership as _Membership
from pocketpaw_ee.cloud.models.workspace import SsoConfig
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "sso_state:"


def _state_key(state: str) -> str:
    return f"{_STATE_KEY_PREFIX}{state}"


def _resolve_issuer(provider: str, issuer: str | None) -> str:
    preset = oidc.PROVIDER_PRESETS.get(provider) or {}
    fixed = preset.get("issuer")
    if fixed:
        return fixed
    if not issuer:
        raise ValidationError("sso.missing_issuer", "issuer is required for this provider")
    return issuer


def _redirect_uri() -> str:
    explicit = os.environ.get("POCKETPAW_SSO_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("POCKETPAW_PUBLIC_BASE_URL", "http://localhost:8888").rstrip("/")
    return f"{base}/api/v1/auth/sso/callback"


def _frontend_root() -> str:
    return os.environ.get("POCKETPAW_FRONTEND_BASE_URL", "/").rstrip("/") or "/"


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------


async def upsert_sso_config(
    workspace_id: str,
    *,
    provider: str,
    issuer: str,
    client_id: str,
    client_secret_plain: str,
    allowed_domains: list[str],
    enforced: bool = False,
) -> SsoConfig:
    try:
        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    except Exception as exc:
        raise NotFound("workspace", workspace_id) from exc
    if doc is None or doc.deleted_at is not None:
        raise NotFound("workspace", workspace_id)

    resolved_issuer = _resolve_issuer(provider, issuer)
    cfg = SsoConfig(
        provider=provider,
        issuer=resolved_issuer,
        client_id=client_id,
        client_secret_encrypted=crypto.encrypt(client_secret_plain),
        allowed_domains=[d.lower().lstrip("@") for d in allowed_domains],
        enforced=enforced,
    )
    doc.sso_config = cfg
    await doc.save()

    await audit_service.record(
        workspace_id,
        "system",
        "sso.config_upsert",
        target_type="workspace",
        target_id=workspace_id,
        metadata={"provider": provider, "issuer": resolved_issuer},
    )
    return cfg


async def get_sso_config(workspace_id: str) -> SsoConfig | None:
    try:
        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    except Exception:
        return None
    if doc is None or doc.deleted_at is not None:
        return None
    return doc.sso_config


async def delete_sso_config(workspace_id: str) -> None:
    try:
        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    except Exception as exc:
        raise NotFound("workspace", workspace_id) from exc
    if doc is None or doc.deleted_at is not None:
        raise NotFound("workspace", workspace_id)
    doc.sso_config = None
    await doc.save()
    await audit_service.record(
        workspace_id,
        "system",
        "sso.config_delete",
        target_type="workspace",
        target_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


async def _find_workspace_by_slug(slug: str) -> _WorkspaceDoc | None:
    return await _WorkspaceDoc.find_one(
        _WorkspaceDoc.slug == slug,
        _WorkspaceDoc.deleted_at == None,  # noqa: E711
    )


async def begin_login(workspace_slug: str) -> str:
    """Build the authorize URL + persist state in Redis. Returns the URL."""
    workspace = await _find_workspace_by_slug(workspace_slug)
    if workspace is None:
        raise NotFound("workspace", workspace_slug)
    cfg = workspace.sso_config
    if cfg is None:
        raise NotFound("sso_config", workspace_slug)

    discovery = await oidc.discover(cfg.issuer, cfg.provider)
    authorize_endpoint = discovery.get("authorization_endpoint")
    if not authorize_endpoint:
        raise ValidationError(
            "sso.discovery_missing_endpoint",
            "provider discovery doc missing authorization_endpoint",
        )

    scopes = oidc.PROVIDER_PRESETS.get(cfg.provider, {}).get(
        "scopes", ["openid", "email", "profile"]
    )

    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    nonce = secrets.token_urlsafe(32)

    payload = json.dumps(
        {"workspace_id": str(workspace.id), "code_verifier": verifier, "nonce": nonce}
    )
    redis = redis_client.get_redis()
    await redis.setex(_state_key(state), _STATE_TTL_SECONDS, payload)

    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": _redirect_uri(),
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorize_endpoint}?{urlencode(params)}"


async def _consume_state(state: str) -> dict[str, Any]:
    redis = redis_client.get_redis()
    key = _state_key(state)
    raw = await redis.get(key)
    if raw is None:
        raise Forbidden("sso.invalid_state", "SSO state is missing or expired")
    await redis.delete(key)
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise Forbidden("sso.invalid_state", "SSO state payload is malformed") from exc


def _email_domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


async def _ensure_membership(user: _UserDoc, workspace_id: str) -> None:
    if any(m.workspace == workspace_id for m in user.workspaces):
        return
    user.workspaces.append(
        _Membership(workspace=workspace_id, role="member", joined_at=datetime.now(UTC))
    )
    if user.active_workspace is None:
        user.active_workspace = workspace_id
    await user.save()


async def complete_login(code: str, state: str) -> _UserDoc:
    state_payload = await _consume_state(state)
    workspace_id = state_payload.get("workspace_id")
    if not workspace_id:
        raise Forbidden("sso.invalid_state", "SSO state missing workspace binding")

    try:
        workspace = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    except Exception as exc:
        raise NotFound("workspace", workspace_id) from exc
    if workspace is None or workspace.deleted_at is not None:
        raise NotFound("workspace", workspace_id)
    cfg = workspace.sso_config
    if cfg is None:
        raise NotFound("sso_config", workspace_id)

    discovery = await oidc.discover(cfg.issuer, cfg.provider)
    token_endpoint = discovery.get("token_endpoint")
    userinfo_endpoint = discovery.get("userinfo_endpoint")
    jwks_uri = discovery.get("jwks_uri")
    if not (token_endpoint and userinfo_endpoint and jwks_uri):
        raise ValidationError(
            "sso.discovery_incomplete",
            "provider discovery doc missing token/userinfo/jwks",
        )

    client_secret = crypto.decrypt(cfg.client_secret_encrypted)
    token_resp = await oidc.exchange_code(
        token_endpoint,
        code,
        cfg.client_id,
        client_secret,
        _redirect_uri(),
        code_verifier=state_payload.get("code_verifier"),
    )
    id_token = token_resp.get("id_token")
    access_token = token_resp.get("access_token")
    if not id_token or not access_token:
        raise ValidationError("sso.token_response_missing_tokens", "provider returned no tokens")

    claims = await oidc.parse_id_token(
        id_token,
        jwks_uri,
        audience=cfg.client_id,
        issuer=cfg.issuer,
        nonce=state_payload.get("nonce"),
    )
    userinfo = await oidc.fetch_userinfo(userinfo_endpoint, access_token)

    email = (userinfo.get("email") or claims.get("email") or "").lower()
    if not email:
        raise Forbidden("sso.email_missing", "provider returned no email")

    existing = await _UserDoc.find_one(_UserDoc.email == email)
    domain = _email_domain(email)
    domain_allowed = domain in (cfg.allowed_domains or [])
    jit = False
    if existing is None:
        if not domain_allowed:
            raise Forbidden(
                "sso.domain_not_allowed",
                f"email domain '{domain}' not in workspace allowlist",
            )
        full_name = userinfo.get("name") or claims.get("name") or ""
        # Sentinel rather than "" so the password verifier deterministically
        # fails — an empty hash could land in code paths that treat it as
        # "no password set, allow anything." 64 random urlsafe bytes is
        # unguessable and is never returned to a client.
        sentinel_password = "!sso-only-" + secrets.token_urlsafe(48)
        user = _UserDoc(
            email=email,
            hashed_password=sentinel_password,
            full_name=full_name,
            is_active=True,
            is_verified=True,
        )
        await user.insert()
        jit = True
    else:
        user = existing
        # Existing user is allowed if they're already a member of THIS
        # workspace (re-login) OR their domain is in the allowlist (cross-
        # workspace auto-join). Otherwise refuse — without this gate a user
        # from any other workspace could be silently auto-joined here just
        # by hitting our /callback with their IdP-signed identity.
        is_member = any(m.workspace == str(workspace.id) for m in user.workspaces)
        if not (is_member or domain_allowed):
            raise Forbidden(
                "sso.domain_not_allowed",
                f"email domain '{domain}' not in workspace allowlist",
            )

    await _ensure_membership(user, str(workspace.id))

    if jit:
        await audit_service.record(
            str(workspace.id),
            str(user.id),
            "sso.jit_provision",
            target_type="user",
            target_id=str(user.id),
            metadata={"email": email, "provider": cfg.provider},
        )
    await audit_service.record(
        str(workspace.id),
        str(user.id),
        "sso.login",
        target_type="user",
        target_id=str(user.id),
        metadata={"provider": cfg.provider, "jit": jit},
    )
    return user


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


async def test_connection(workspace_id: str) -> dict[str, Any]:
    cfg = await get_sso_config(workspace_id)
    if cfg is None:
        return {"ok": False, "error": "no_sso_config"}
    try:
        discovery = await oidc.discover(cfg.issuer, cfg.provider)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"discovery_failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 — surface any failure as a structured error
        return {"ok": False, "error": str(exc)}
    keys = ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri")
    endpoints = {k: discovery.get(k, "") for k in keys}
    missing = [k for k, v in endpoints.items() if not v]
    if missing:
        return {"ok": False, "error": f"discovery_missing: {','.join(missing)}"}
    return {"ok": True, "issuer": cfg.issuer, "endpoints": endpoints}
