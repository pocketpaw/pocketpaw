# ee/pocketpaw_ee/integrations/providers/base.py: the provider contract.
#
# One module per provider under ``providers/`` implements ``IntegrationProvider``
# and registers it in ``providers/__init__.py``. A provider declares: identity (id,
# name, kind builtin | connect | embed, category, plan); availability() (available |
# coming_soon | unconfigured); ``settings_fields`` (owner-editable and server-set
# readonly ``SettingsField``s, PUT is validated against them); for connect kinds
# authorize_url (may be async) / complete / refresh and ``supports_pkce`` (S256
# challenge to authorize_url, verifier to complete, kept in the server-side state);
# ``actions`` (an allowlist of ``ActionSpec``); verify_webhook() -> NormalizedEvent.
#
# Optional hooks (``BaseProvider`` has defaults; callers use getattr):
#   * site_setup(binding, ctx) -> dict   on link/enable/reconnect; idempotent;
#     returns readonly settings; raising leaves the site ``setup_required``
#   * site_status(binding, connection) -> str | None   override the computed status
#   * connection_attention(connection) -> str | None   ConnectionView hint (meta)
#   * resume_url(connection, state, redirect_uri) -> str | None   (async) finishes an
#     unfinished setup; sends the browser back to ``redirect_uri`` with ``state``
#   * callback_params: tuple[str, ...]   callback query keys ``complete`` also gets
#     as ``callback_params={key: value}``: untrusted, the provider validates them
#   * refresh_with_meta: bool   ``refresh`` also gets ``meta=connection.meta`` (a
#     token endpoint that depends on the account: Zoho's region, Shopify's shop)
#   * connect_params: tuple[ConnectParam, ...]   typed by the owner before Connect;
#     checked by ``check_connect_params``, kept in the state (never read from the
#     callback), passed as ``params=`` to authorize_url / complete; a grant should
#     keep them in ``meta["connect_params"]`` for resume and refresh
#   * verify_callback(query, params) -> bool   checks the full (signed) callback query
#   * handle_event(event) -> bool   (async) account-level webhooks (uninstall, privacy
#     requests) taken before site routing; True acks and records the event id
#   * field_options(binding, ctx, key) -> list[SettingOption]   (async) live choices
#     of a ``select`` with ``options_source="remote"``; ``binding`` may be None;
#     raise ``ProviderError`` on failure (capped and cached in field_options.py)
#
# Handlers receive an ``ActionContext`` and return a JSON-safe dict that goes to a
# public caller as ``result``. They must never return a token or a secret.
# ``ActionContext.connection_meta`` is the bound connection's non-secret ``meta``.
# ``ProviderError`` is the failure a handler raises; the gateway maps it to 502.
# ``ActionRefused(code)`` is a handler refusal with a gateway code instead
# (``invalid_args``, ``rate_limited``, ``setup_required``); ``setup_required`` also
# marks the site's binding. ``InvalidGrant`` from ``refresh`` means the grant is
# dead (owner must reconnect). ``get_token(rejected=token)`` forces one refresh
# when the provider refused ``token``; without it the cached token comes back.

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

Kind = Literal["builtin", "connect", "embed"]
FieldType = Literal["url", "text", "select", "link"]
OptionsSource = Literal["static", "remote"]
Plan = Literal["free", "site"]
Availability = Literal["available", "coming_soon", "unconfigured"]


class ProviderError(Exception):
    """The provider failed or answered something we can't use. Message is safe to log."""


class InvalidGrant(ProviderError):
    """The refresh token is dead (``invalid_grant`` / ``BAD_REFRESH_TOKEN``)."""


class ActionRefused(ProviderError):
    """A handler refusal the gateway passes through as ``code``. ``message`` is
    caller-facing (no secrets); ``setup_error`` is stored on the binding when the
    code is ``setup_required``."""

    CODES = frozenset({"invalid_args", "rate_limited", "setup_required"})

    def __init__(self, code: str, message: str = "", *, setup_error: str = "") -> None:
        if code not in self.CODES:
            raise ValueError(f"unknown refusal code {code!r}")
        self.code = code
        self.message = message
        self.setup_error = setup_error or "setup_failed"
        super().__init__(f"{code}: {message}" if message else code)


class ActionArgs(BaseModel):
    """Base for an action's args model: unknown keys are refused."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass(frozen=True)
class TokenGrant:
    """What ``complete`` / ``refresh`` hand the vault. Lives only in memory."""

    access_token: str
    refresh_token: str = ""
    expires_in: int | None = None
    scopes: list[str] = field(default_factory=list)
    account_label: str = ""
    account_ref: str = ""
    token_type: str = "bearer"
    # Non-secret provider state kept on the connection (``connection.meta``), e.g.
    # whether a payments account can take charges yet. Never a credential.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectParam:
    """One value the owner gives before Connect (``params`` on the connect body)."""

    key: str
    label: str
    placeholder: str = ""
    max_length: int = 200


@dataclass(frozen=True)
class SettingOption:
    value: str
    label: str


@dataclass(frozen=True)
class SettingsField:
    """One per-site setting. ``readonly`` ones are server-set (``site_setup``) and
    ignored when a client sends them; ``hidden`` ones (readonly too) never reach a
    view. ``multi`` selects take comma-separated values. A ``url`` must be http(s)
    and, when ``allowed_hosts`` is set, have one of those hosts exactly."""

    key: str
    label: str
    type: FieldType = "text"
    help: str = ""
    required: bool = False
    options: tuple[SettingOption, ...] = ()
    readonly: bool = False
    hidden: bool = False
    multi: bool = False
    allowed_hosts: tuple[str, ...] = ()
    max_length: int = 500
    options_source: OptionsSource = "static"


@dataclass
class SetupContext:
    """What ``site_setup`` may use. ``get_token`` is None for kinds with no grant."""

    workspace_id: str
    pocket_id: str
    user_id: str
    provider: str
    connection: Any | None = None
    get_token: Callable[..., Awaitable[str]] | None = None


@dataclass
class ActionContext:
    """Everything a handler may use. ``get_token`` refreshes under the vault lease."""

    workspace_id: str
    pocket_id: str
    site_id: str
    provider: str
    action: str
    settings: Mapping[str, Any]
    caller: Literal["site_key", "gateway_token", "owner"]
    get_token: Callable[..., Awaitable[str]] | None = None
    connection_meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionSpec:
    """One allowlisted action. Only ``public`` ones are reachable from a site."""

    args: type[ActionArgs]
    handler: Callable[[ActionContext, Any], Awaitable[dict[str, Any]]]
    description: str
    public: bool = False
    write: bool = False
    # Per-minute caps the gateway applies (site-key callers; gateway-token callers
    # get ``site_per_min * 10`` and no per-IP cap).
    ip_per_min: int | None = None
    site_per_min: int | None = None

    def caps(self) -> tuple[int, int]:
        ip_default, site_default = (10, 60) if self.write else (60, 600)
        return (self.ip_per_min or ip_default, self.site_per_min or site_default)


@dataclass(frozen=True)
class NormalizedEvent:
    """A verified provider webhook in our vocabulary.

    ``type`` is ``order.paid`` / ``order.refunded`` / ``booking.created`` / ...
    The event is routed to a site by ``site_id`` when the provider carries it, else
    by ``account_ref`` through the connection's bindings."""

    id: str
    type: str
    provider: str
    data: dict[str, Any] = field(default_factory=dict)
    site_id: str = ""
    account_ref: str = ""


class IntegrationProvider(Protocol):
    id: str
    name: str
    kind: Kind
    category: str
    plan: Plan
    supports_pkce: bool
    actions: dict[str, ActionSpec]
    settings_fields: tuple[SettingsField, ...]

    def availability(self) -> Availability: ...

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str | Awaitable[str]: ...

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant: ...

    async def refresh(
        self, refresh_token: str, meta: Mapping[str, Any] | None = None
    ) -> TokenGrant: ...

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None: ...


class BaseProvider:
    """Defaults for providers that have no OAuth, no webhooks, no setup (built-ins)."""

    id: str = ""
    name: str = ""
    kind: Kind = "builtin"
    category: str = ""
    plan: Plan = "free"
    supports_pkce: bool = False
    actions: dict[str, ActionSpec] = {}
    settings_fields: tuple[SettingsField, ...] = ()
    connect_params: tuple[ConnectParam, ...] = ()

    def availability(self) -> Availability:
        return "available"

    def check_connect_params(self, params: Mapping[str, Any]) -> dict[str, str]:
        """The declared connect params, stripped. ValueError (owner-facing) for an
        unknown key, a missing value or one over its cap."""
        declared = {p.key: p for p in self.connect_params}
        unknown = [k for k in params if k not in declared]
        if unknown:
            raise ValueError(f"{self.name} doesn't take {unknown[0]!r}.")
        out: dict[str, str] = {}
        for key, spec in declared.items():
            value = str(params.get(key) or "").strip()
            if not value:
                raise ValueError(f"Enter your {spec.label}.")
            if len(value) > spec.max_length:
                raise ValueError(f"{spec.label} is too long.")
            out[key] = value
        return out

    def authorize_url(
        self,
        state: str,  # noqa: ARG002
        redirect_uri: str,  # noqa: ARG002
        code_challenge: str | None = None,  # noqa: ARG002
    ) -> str:
        raise ProviderError(f"{self.id} has no connect flow")

    async def complete(
        self,
        code: str,  # noqa: ARG002
        redirect_uri: str,  # noqa: ARG002
        code_verifier: str | None = None,  # noqa: ARG002
    ) -> TokenGrant:
        raise ProviderError(f"{self.id} has no connect flow")

    async def refresh(
        self,
        refresh_token: str,  # noqa: ARG002
        meta: Mapping[str, Any] | None = None,  # noqa: ARG002
    ) -> TokenGrant:
        raise ProviderError(f"{self.id} has no refresh flow")

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None:  # noqa: ARG002
        return None

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:  # noqa: ARG002
        return {}

    def site_status(self, binding: Any, connection: Any | None) -> str | None:  # noqa: ARG002
        return None

    def connection_attention(self, connection: Any) -> str | None:  # noqa: ARG002
        return None

    async def resume_url(
        self,
        connection: Any,  # noqa: ARG002
        state: str,  # noqa: ARG002
        redirect_uri: str,  # noqa: ARG002
    ) -> str | None:
        return None

    async def field_options(
        self,
        binding: Any,  # noqa: ARG002
        ctx: SetupContext,  # noqa: ARG002
        key: str,  # noqa: ARG002
    ) -> list[SettingOption]:
        return []
