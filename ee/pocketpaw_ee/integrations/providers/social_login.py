# ee/pocketpaw_ee/integrations/providers/social_login.py: the built-in ``social_login``.
#
# Kind ``builtin``, category ``auth``, plan ``site``. It lets visitors of a PROJECT
# site that uses the better-auth recipe sign in with Google or GitHub through the
# platform OAuth broker (``integrations/social_broker.py``): one platform OAuth
# client per provider, one fixed callback per provider on the platform domain, and
# the site never holds a client secret. The owner picks which providers are on
# (``providers``, a required multi select); there are no gateway actions.
#
# Platform clients come from env (``POCKETPAW_SOCIAL_GOOGLE_CLIENT_ID`` /
# ``_SECRET``, ``POCKETPAW_SOCIAL_GITHUB_CLIENT_ID`` / ``_SECRET``), deliberately
# separate from the app's own sign-in clients, plus ``POCKETPAW_SOCIAL_PAIRWISE_SECRET``
# (32+ chars, never rotated) that keys the per-site pairwise account ids. A provider
# counts as configured only with its client AND that secret; with neither provider
# configured this reads ``unconfigured``. ``site_setup`` makes sure the site's ``PAW_GATEWAY_TOKEN``
# exists: the site Worker presents it to redeem a sign-in ticket.

from __future__ import annotations

import os
from typing import Any

from pocketpaw_ee.integrations.providers.base import (
    Availability,
    BaseProvider,
    SettingOption,
    SettingsField,
    SetupContext,
)

SOCIAL_PROVIDERS = ("google", "github")
_ENV = "POCKETPAW_SOCIAL_{}_CLIENT_{}"
PAIRWISE_ENV = "POCKETPAW_SOCIAL_PAIRWISE_SECRET"
MIN_PAIRWISE_SECRET = 32


def client_id(provider: str) -> str:
    return os.environ.get(_ENV.format(provider.upper(), "ID"), "").strip()


def client_secret(provider: str) -> str:
    return os.environ.get(_ENV.format(provider.upper(), "SECRET"), "").strip()


def pairwise_secret() -> str:
    """Keys the per-site pairwise account ids. Rotating it changes every visitor's id
    on every site (they would get new accounts), so set it once."""
    return os.environ.get(PAIRWISE_ENV, "").strip()


def configured(provider: str) -> bool:
    return (
        provider in SOCIAL_PROVIDERS
        and bool(client_id(provider) and client_secret(provider))
        and len(pairwise_secret()) >= MIN_PAIRWISE_SECRET
    )


def enabled_providers(settings: dict[str, Any] | None) -> list[str]:
    """The providers an owner turned on for a site (``providers`` = "google,github")."""
    raw = (settings or {}).get("providers")
    if not isinstance(raw, str):
        return []
    return [p for p in (v.strip() for v in raw.split(",")) if p in SOCIAL_PROVIDERS]


class SocialLoginProvider(BaseProvider):
    id = "social_login"
    name = "Social sign-in"
    kind = "builtin"
    category = "auth"
    plan = "site"
    settings_fields = (
        SettingsField(
            key="providers",
            label="Sign-in providers",
            type="select",
            help="Which buttons visitors can use to sign in to this site.",
            required=True,
            multi=True,
            options=(SettingOption("google", "Google"), SettingOption("github", "GitHub")),
        ),
    )

    def availability(self) -> Availability:
        return "available" if any(configured(p) for p in SOCIAL_PROVIDERS) else "unconfigured"

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:  # noqa: ARG002
        from pocketpaw_ee.integrations.bindings import ensure_gateway_token

        await ensure_gateway_token(ctx.workspace_id, ctx.pocket_id)
        return {}
