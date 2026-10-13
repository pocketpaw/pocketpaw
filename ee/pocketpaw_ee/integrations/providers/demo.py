# ee/pocketpaw_ee/integrations/providers/demo.py: the fake ``demo`` connect provider.
#
# Registered only when ``POCKETPAW_INTEGRATIONS_DEMO=1`` (see providers/__init__.py).
# It exercises every seam a real provider uses, with no network:
#   * authorize_url points straight at our own callback with a fake ``demo_`` code
#     that embeds the PKCE challenge; complete checks the verifier against it
#   * complete / refresh mint fake tokens; refresh rotates the refresh token every
#     time, and a refresh token ending in ``revoked`` raises InvalidGrant
#   * actions: ``echo`` (public read) returns its args, ``record`` (public write)
#     stores them in ``IntegrationDemoRecord``
#   * webhooks: body signed with HMAC-SHA256 in ``X-Demo-Signature`` using
#     ``POCKETPAW_INTEGRATIONS_DEMO_WEBHOOK_SECRET``. No default: without the demo
#     switch AND an explicit secret every webhook is refused.
# Not for production; nothing here is a credential.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from pydantic import Field

from pocketpaw_ee.integrations.providers.base import (
    ActionArgs,
    ActionContext,
    ActionSpec,
    BaseProvider,
    InvalidGrant,
    NormalizedEvent,
    ProviderError,
    TokenGrant,
)

ENABLE_ENV = "POCKETPAW_INTEGRATIONS_DEMO"
WEBHOOK_SECRET_ENV = "POCKETPAW_INTEGRATIONS_DEMO_WEBHOOK_SECRET"
SIGNATURE_HEADER = "x-demo-signature"


def demo_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip() == "1"


def webhook_secret() -> str:
    """The configured demo webhook secret, or "" (then nothing verifies)."""
    return os.environ.get(WEBHOOK_SECRET_ENV, "").strip()


def sign(body: bytes) -> str:
    """The signature a demo webhook sender puts in ``X-Demo-Signature``."""
    secret = webhook_secret()
    if not secret:
        raise ValueError(f"{WEBHOOK_SECRET_ENV} is not set")
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class EchoArgs(ActionArgs):
    message: str = Field(default="", max_length=256)


class RecordArgs(ActionArgs):
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(default="", max_length=1000)


async def _echo(ctx: ActionContext, args: EchoArgs) -> dict[str, Any]:
    # Proves the handler can reach a live token without ever returning it.
    if ctx.get_token is not None:
        await ctx.get_token()
    return {"echo": args.message}


async def _record(ctx: ActionContext, args: RecordArgs) -> dict[str, Any]:
    from pocketpaw_ee.cloud.models.integration import IntegrationDemoRecord

    if ctx.get_token is not None:
        await ctx.get_token()
    row = IntegrationDemoRecord(
        workspace=ctx.workspace_id, pocket_id=ctx.pocket_id, args=args.model_dump()
    )
    await row.insert()
    return {"recorded": True, "id": str(row.id)}


def _grant() -> TokenGrant:
    return TokenGrant(
        access_token=f"demo-access-{secrets.token_urlsafe(12)}",
        refresh_token=f"demo-refresh-{secrets.token_urlsafe(12)}",
        expires_in=3600,
        scopes=["demo.read", "demo.write"],
        account_label="Demo account",
        account_ref="demo-account",
    )


class DemoProvider(BaseProvider):
    id = "demo"
    name = "Demo"
    kind = "connect"
    category = "developer"
    plan = "free"
    supports_pkce = True
    actions = {
        "echo": ActionSpec(
            args=EchoArgs,
            handler=_echo,
            description="Echo a message back (fake provider, for testing).",
            public=True,
            write=False,
        ),
        "record": ActionSpec(
            args=RecordArgs,
            handler=_record,
            description="Store a label and value (fake provider, for testing).",
            public=True,
            write=True,
        ),
    }

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str:
        # The fake "provider" remembers the challenge by carrying it in the code.
        code = f"demo_{secrets.token_urlsafe(12)}.{code_challenge or ''}"
        return f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"

    async def complete(
        self,
        code: str,
        redirect_uri: str,  # noqa: ARG002
        code_verifier: str | None = None,
    ) -> TokenGrant:
        if not code.startswith("demo_") or "." not in code:
            raise ProviderError("demo: unknown authorization code")
        challenge = code.rsplit(".", 1)[1]
        if (
            not challenge
            or not code_verifier
            or not hmac.compare_digest(challenge, _s256(code_verifier))
        ):
            raise ProviderError("demo: PKCE verification failed")
        return _grant()

    async def refresh(self, refresh_token: str) -> TokenGrant:
        if not refresh_token or refresh_token.endswith("revoked"):
            raise InvalidGrant("demo: invalid_grant")
        return _grant()

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None:
        if not demo_enabled() or not webhook_secret():
            return None
        given = headers.get(SIGNATURE_HEADER, "")
        if not given or not hmac.compare_digest(given, sign(body)):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload.get("id") or not payload.get("type"):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return NormalizedEvent(
            id=str(payload["id"]),
            type=str(payload["type"]),
            provider=self.id,
            data=data,
            site_id=str(payload.get("site_id") or ""),
            account_ref=str(payload.get("account_ref") or ""),
        )
