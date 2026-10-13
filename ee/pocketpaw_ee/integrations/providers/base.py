# ee/pocketpaw_ee/integrations/providers/base.py: the provider contract.
#
# One module per provider under ``providers/`` implements ``IntegrationProvider``
# and registers it in ``providers/__init__.py``. A provider declares:
#   * identity: id, name, kind (builtin | connect | embed), category, plan
#   * availability(): available | coming_soon | unconfigured (no client id set)
#   * the OAuth hooks for connect kinds: authorize_url / complete / refresh, and
#     ``supports_pkce`` (then authorize_url gets an S256 challenge and complete the
#     verifier, which the flow keeps in its server-side state)
#   * actions: an allowlist of ``ActionSpec`` (typed args model, public, write)
#   * verify_webhook(): signature check -> ``NormalizedEvent`` or None
#
# Handlers receive an ``ActionContext`` and return a JSON-safe dict that goes to a
# public caller as ``result``. They must never return a token or a secret.
# ``ProviderError`` is the one failure a handler raises; the gateway maps it to 502.
# ``InvalidGrant`` from ``refresh`` means the grant is dead (owner must reconnect).

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

Kind = Literal["builtin", "connect", "embed"]
Plan = Literal["free", "site"]
Availability = Literal["available", "coming_soon", "unconfigured"]


class ProviderError(Exception):
    """The provider failed or answered something we can't use. Message is safe to log."""


class InvalidGrant(ProviderError):
    """The refresh token is dead (``invalid_grant`` / ``BAD_REFRESH_TOKEN``)."""


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
    get_token: Callable[[], Awaitable[str]] | None = None


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

    def availability(self) -> Availability: ...

    def authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str: ...

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant: ...

    async def refresh(self, refresh_token: str) -> TokenGrant: ...

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None: ...


class BaseProvider:
    """Defaults for providers that have no OAuth or no webhooks (built-ins)."""

    id: str = ""
    name: str = ""
    kind: Kind = "builtin"
    category: str = ""
    plan: Plan = "free"
    supports_pkce: bool = False
    actions: dict[str, ActionSpec] = {}

    def availability(self) -> Availability:
        return "available"

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

    async def refresh(self, refresh_token: str) -> TokenGrant:  # noqa: ARG002
        raise ProviderError(f"{self.id} has no refresh flow")

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None:  # noqa: ARG002
        return None
