# ee/pocketpaw_ee/integrations/providers/supabase.py: the ``supabase`` database provider.
#
# Kind ``connect`` (Supabase OAuth app, authorization code + PKCE S256), category
# ``database``, plan ``site``. Configured by ``POCKETPAW_INTEGRATIONS_SUPABASE_
# CLIENT_ID`` / ``_SECRET``; without both it is ``unconfigured``. The token endpoint
# takes the client credentials as HTTP Basic. Refresh tokens rotate (the vault keeps
# the newest); ``invalid_grant`` raises InvalidGrant. A grant is scoped to one
# organization: ``account_ref`` = its id, ``account_label`` = its name
# (``GET /v1/organizations``).
#
# Settings: ``project_ref`` (remote select over ``GET /v1/projects``, label
# "name (region)", required), ``admin_key`` (off | on, off when unset), and the
# readonly ``project_url`` link and ``kept_secrets`` note.
#
# No actions: the site talks to Supabase itself through the paw-sites ``supabase``
# recipe. ``site_setup`` (idempotent) reads the project's keys with
# ``GET /v1/projects/{ref}/api-keys?reveal=true`` and writes them into the SITE's
# secret store, tagged ``managed_by="integration:supabase"``: ``SUPABASE_URL``,
# ``SUPABASE_ANON_KEY`` (the publishable key, else the legacy ``anon``) and, only
# with ``admin_key`` on, ``SUPABASE_SERVICE_ROLE_KEY`` (a secret key, else the legacy
# ``service_role``). A secret the owner set is never replaced; its name lands in
# ``kept_secrets``. A project change overwrites managed rows and drops any the new
# setup doesn't want; no project drops them all; a setup that finds its binding
# unlinked or relinked when it finishes undoes its writes. ``site_teardown`` deletes
# only managed rows. A 403 on the keys is ``setup_required``
# (``supabase_keys_forbidden``), a 404 ``supabase_project_not_found``; a 401
# refreshes once. Key values never reach settings, a view, a log or an error.

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers.base import (
    ActionRefused,
    Availability,
    BaseProvider,
    ProviderError,
    SettingOption,
    SettingsField,
    SetupContext,
    TokenGrant,
)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_SUPABASE"
API = "https://api.supabase.com"
AUTHORIZE_URL = f"{API}/v1/oauth/authorize"
TOKEN_URL = f"{API}/v1/oauth/token"
MANAGED_BY = "integration:supabase"
PROJECT_REF = "project_ref"
PROJECT_URL = "project_url"
ADMIN_KEY = "admin_key"
KEPT_SECRETS = "kept_secrets"
URL_SECRET = "SUPABASE_URL"
ANON_SECRET = "SUPABASE_ANON_KEY"
SERVICE_SECRET = "SUPABASE_SERVICE_ROLE_KEY"
# Supabase refs are 20 lowercase letters; this only has to keep a host label safe.
REF_RE = re.compile(r"[a-z0-9]{1,63}")

_DESCRIPTIONS = {
    URL_SECRET: "Your Supabase project URL. Set by the Supabase integration.",
    ANON_SECRET: "Your Supabase publishable key (row level security applies). "
    "Set by the Supabase integration.",
    SERVICE_SECRET: "Your Supabase secret key (bypasses row level security; server "
    "code only). Set by the Supabase integration.",
}


def _credentials() -> tuple[str, str]:
    client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
    if not client_id or not secret:
        raise ProviderError("supabase: not configured")
    return client_id, secret


def _basic(client_id: str, secret: str) -> dict[str, str]:
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    return {"authorization": f"Basic {raw}"}


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def project_url(ref: str) -> str:
    return f"https://{ref}.supabase.co"


def _list(resp: Any) -> list[dict[str, Any]]:
    try:
        body = resp.json()
    except ValueError:
        return []
    return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []


def _key(keys: list[dict[str, Any]], *, kind: str, legacy: str) -> str:
    """The value of the first ``kind`` key (``default`` first), else the legacy one."""
    typed = [k for k in keys if k.get("type") == kind and k.get("api_key")]
    typed.sort(key=lambda k: k.get("name") != "default")
    if typed:
        return str(typed[0]["api_key"])
    for k in keys:
        if k.get("type") in (None, "legacy") and k.get("name") == legacy and k.get("api_key"):
            return str(k["api_key"])
    return ""


class SupabaseProvider(BaseProvider):
    id = "supabase"
    name = "Supabase"
    kind = "connect"
    category = "database"
    plan = "site"
    supports_pkce = True
    actions = {}
    settings_fields = (
        SettingsField(
            key=PROJECT_REF,
            label="Project",
            type="select",
            required=True,
            options_source="remote",
            max_length=63,
            help="The Supabase project this site uses as its database.",
        ),
        SettingsField(
            key=PROJECT_URL,
            label="Project URL",
            type="link",
            readonly=True,
            help="Its keys are added to the site's secrets when you pick a project.",
        ),
        SettingsField(
            key=ADMIN_KEY,
            label="Admin access",
            type="select",
            options=(
                SettingOption("off", "Off (recommended)"),
                SettingOption("on", "On — server admin access, bypasses row level security"),
            ),
            help="Adds the project's secret key as SUPABASE_SERVICE_ROLE_KEY, for "
            "server-side admin jobs only. Off unless set.",
        ),
        SettingsField(
            key=KEPT_SECRETS,
            label="Kept your own values",
            type="text",
            readonly=True,
            help="Site secrets you set yourself. Supabase leaves them as they are.",
        ),
    )

    def availability(self) -> Availability:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        return "available" if client_id and secret else "unconfigured"

    # ------------------------------------------------------------------ OAuth

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str:
        client_id, _ = _credentials()
        if not code_challenge:
            raise ProviderError("supabase: PKCE challenge missing")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant:
        client_id, secret = _credentials()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if code_verifier:
            data["code_verifier"] = code_verifier
        body = await oauth_http.token_request(
            TOKEN_URL, data, provider=self.id, headers=_basic(client_id, secret)
        )
        access = str(body["access_token"])
        org = await self._organization(access)
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            account_label=str(org.get("name") or org["id"]),
            account_ref=str(org["id"]),
        )

    async def _organization(self, access_token: str) -> dict[str, Any]:
        try:
            async with oauth_http.client() as http:
                resp = await http.get(
                    f"{API}/v1/organizations",
                    headers={"authorization": f"Bearer {access_token}"},
                )
        except Exception as exc:  # noqa: BLE001 - one safe error for any transport failure
            raise ProviderError(
                f"supabase: organizations unreachable ({type(exc).__name__})"
            ) from None
        if resp.status_code >= 400:
            raise ProviderError(f"supabase: organizations answered {resp.status_code}")
        orgs = [o for o in _list(resp) if o.get("id") or o.get("slug")]
        if not orgs:
            raise ProviderError("supabase: the grant has no organization")
        org = orgs[0]
        return {"id": str(org.get("id") or org.get("slug")), "name": str(org.get("name") or "")}

    async def refresh(
        self,
        refresh_token: str,
        meta: Mapping[str, Any] | None = None,  # noqa: ARG002
    ) -> TokenGrant:
        client_id, secret = _credentials()
        body = await oauth_http.token_request(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            provider=self.id,
            headers=_basic(client_id, secret),
        )
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
        )

    # ------------------------------------------------------- settings + setup

    async def field_options(
        self,
        binding: Any,  # noqa: ARG002
        ctx: SetupContext,
        key: str,
    ) -> list[SettingOption]:
        if key != PROJECT_REF:
            return []
        resp = await oauth_http.authed(ctx.get_token, "GET", f"{API}/v1/projects", provider=self.id)
        if resp.status_code >= 400:
            raise ProviderError(f"supabase: projects answered {resp.status_code}")
        options: list[SettingOption] = []
        for project in _list(resp):
            ref = str(project.get("ref") or project.get("id") or "")
            if not REF_RE.fullmatch(ref):
                continue
            name = str(project.get("name") or ref)
            region = str(project.get("region") or "")
            options.append(SettingOption(value=ref, label=f"{name} ({region})" if region else name))
        return options

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:
        """Write the chosen project's URL and keys into the site's secrets."""
        from pocketpaw_ee.sites import site_secrets

        settings = dict(binding.settings or {})
        ref = str(settings.get(PROJECT_REF) or "")
        if not ref:
            # No project (never picked, or dropped on a relink): nothing of ours may
            # linger. The required select reads setup_required until one is picked.
            await self._drop(ctx)
            return {KEPT_SECRETS: ""}
        if not REF_RE.fullmatch(ref):
            raise ActionRefused(
                "setup_required", "Pick the project again.", setup_error="supabase_bad_project"
            )
        resp = await oauth_http.authed(
            ctx.get_token,
            "GET",
            f"{API}/v1/projects/{ref}/api-keys",
            provider=self.id,
            params={"reveal": "true"},
        )
        if resp.status_code == 403:
            raise ActionRefused(
                "setup_required",
                "Supabase didn't allow reading this project's API keys.",
                setup_error="supabase_keys_forbidden",
            )
        if resp.status_code == 404:
            raise ActionRefused(
                "setup_required",
                "That Supabase project wasn't found.",
                setup_error="supabase_project_not_found",
            )
        if resp.status_code >= 400:
            raise ProviderError(f"supabase: api-keys answered {resp.status_code}")
        keys = _list(resp)
        values = {
            URL_SECRET: project_url(ref),
            ANON_SECRET: _key(keys, kind="publishable", legacy="anon"),
        }
        if settings.get(ADMIN_KEY) == "on":
            values[SERVICE_SECRET] = _key(keys, kind="secret", legacy="service_role")
        if not values[ANON_SECRET]:
            raise ActionRefused(
                "setup_required",
                "This Supabase project has no publishable key.",
                setup_error="supabase_no_publishable_key",
            )
        wanted = {name: value for name, value in values.items() if value}
        kept: list[str] = []
        for name, value in wanted.items():
            if not await site_secrets.set_managed_secret(
                workspace_id=ctx.workspace_id,
                pocket_id=ctx.pocket_id,
                name=name,
                value=value,
                managed_by=MANAGED_BY,
                description=_DESCRIPTIONS[name],
            ):
                kept.append(name)
        await site_secrets.delete_managed_secrets(
            workspace_id=ctx.workspace_id,
            pocket_id=ctx.pocket_id,
            managed_by=MANAGED_BY,
            keep=frozenset(wanted),
        )
        if not await _still_linked(binding):
            # Unlinked (or relinked) while this ran: undo what it just wrote.
            await self._drop(ctx)
            return {}
        return {PROJECT_URL: project_url(ref), KEPT_SECRETS: ", ".join(kept)}

    async def _drop(self, ctx: SetupContext) -> None:
        from pocketpaw_ee.sites import site_secrets

        await site_secrets.delete_managed_secrets(
            workspace_id=ctx.workspace_id, pocket_id=ctx.pocket_id, managed_by=MANAGED_BY
        )

    async def site_teardown(self, binding: Any) -> None:
        """Unlinked or deleted: drop the secrets this provider wrote, nothing else."""
        from pocketpaw_ee.sites import site_secrets

        await site_secrets.delete_managed_secrets(
            workspace_id=str(binding.workspace),
            pocket_id=str(binding.pocket_id),
            managed_by=MANAGED_BY,
        )


async def _still_linked(binding: Any) -> bool:
    """The binding row still exists and still points at the same connection."""
    from pocketpaw_ee.cloud.models.integration import SiteIntegration as Row

    row = await Row.find_one(
        Row.workspace == str(binding.workspace),
        Row.pocket_id == str(binding.pocket_id),
        Row.provider == "supabase",
    )
    return row is not None and row.connection_id == getattr(binding, "connection_id", None)
