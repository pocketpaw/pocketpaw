"""Social identity provider contract.

Created 2026-07-29 (AM-2/AM-3). One shape for "who is this person, and does
their provider actually vouch for the email they claim".

The whole design turns on ``email_verified``. Linking an incoming provider
identity to an existing account by matching email addresses is safe ONLY when
the provider asserts the address is verified. GitHub, for instance, lets any
user attach an arbitrary address to their account; if we matched on that, an
attacker adds victim@corp.com to their own GitHub, signs in, and lands inside
the victim's account. The same class of bug is nOAuth (Entra's mutable,
unverified ``email`` claim) and nhost GHSA-6g38-8j4p-j3pr.

So the rule this module exists to enforce: **every adapter must compute
``email_verified`` from the provider's authoritative source, and must never
infer it from the mere presence of an address.** Where a provider gives no
verified address, the adapter reports ``email=None`` rather than guessing —
the service turns that into a refusal, not a link.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SocialIdentity:
    """A person as a provider describes them, after verification checks."""

    #: Provider key — "google" | "github". Half of the linking identity.
    provider: str
    #: The provider's own stable id for this account. NEVER the email: emails
    #: change hands, provider ids do not. This is the primary match key.
    account_id: str
    #: A VERIFIED address, or None. None is meaningful — it means the provider
    #: would not vouch for any address, so nothing may be matched on.
    email: str | None
    #: Display name, best effort. Cosmetic only, never used for matching.
    full_name: str = ""
    #: Avatar URL, best effort. Cosmetic only.
    avatar: str = ""

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("SocialIdentity.provider is required")
        if not self.account_id:
            raise ValueError("SocialIdentity.account_id is required")

    @property
    def has_verified_email(self) -> bool:
        """True when the provider vouched for an address.

        A bare truthiness check on ``email`` is the same thing by construction:
        adapters only ever populate the field with a verified address. This
        property exists so call sites read as the security decision they are.
        """
        return bool(self.email)


@runtime_checkable
class SocialProvider(Protocol):
    """What every provider adapter must offer the social service."""

    #: Stable key, stored on the linked account. Never rename one in place.
    name: str

    def is_configured(self) -> bool:
        """Whether credentials are present.

        Unconfigured providers are hidden from the UI rather than offered and
        then failing at the consent screen.
        """
        ...

    def authorize_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Where to send the browser to begin consent."""
        ...

    async def exchange(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> SocialIdentity:
        """Redeem the authorization code and return the verified identity."""
        ...
