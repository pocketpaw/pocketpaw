"""Social sign-in wire types.

Created 2026-07-29 (AM-2). Thin by design: the browser-facing endpoints are
redirects, not JSON, so the only response body here is the provider list the
auth dialog reads to decide which buttons exist.

Updated 2026-08-01 (AM-6) with the connected-accounts shapes. These ARE JSON:
Settings calls them with fetch, where a 302 is opaque and useless. Note what
``LinkedIdentity`` does NOT carry — no ``access_token``, no ``account_id``, no
provider payload. The response type is the enforcement: it can only serialize
the three fields the panel renders, so a future field added to the stored
``OAuthAccount`` cannot leak through this endpoint by default.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SocialProvidersResponse(BaseModel):
    """Which providers this deployment can actually complete a flow with.

    The dialog renders one button per entry. A provider missing its
    credentials is absent rather than present-and-broken — a button that opens
    a dead consent screen is worse than no button.
    """

    providers: list[str] = Field(default_factory=list)


class SocialExchangeRequest(BaseModel):
    """Trade a one-time code from the desktop callback for a bearer token."""

    xc: str = Field(min_length=1, max_length=512)


class LinkedIdentity(BaseModel):
    """One provider identity attached to the signed-in user.

    ``account_email`` is what the PROVIDER had for them at link time, shown so
    the user can tell two GitHub accounts apart. It is display-only and is
    never a lookup key — see ``domain.decide_link``.
    """

    provider: str
    account_email: str = ""
    #: None for identities linked before the field existed — see the note in
    #: ``models/user.py``. The panel shows "Connected" with no date.
    linked_at: datetime | None = None


class SocialIdentitiesResponse(BaseModel):
    """Everything the connected-accounts panel needs to render."""

    identities: list[LinkedIdentity] = Field(default_factory=list)


class SocialLinkCompleteRequest(BaseModel):
    """Redeem a desktop link code.

    The desktop callback parks the consented identity instead of attaching it,
    because a Tauri webview carries no cookie our backend can authenticate.
    This request finishes the job over the bearer the app does hold.
    """

    code: str = Field(min_length=1, max_length=512)


class SocialLinkCompleteResponse(BaseModel):
    """The outcome of a desktop link, plus the panel's new state.

    Returns the full identity list so the app renders the result without a
    second round trip — the window that started this has already closed, and a
    refetch that fails leaves the panel stale with no way to explain itself.
    """

    provider: str
    identities: list[LinkedIdentity] = Field(default_factory=list)


class SocialLinkStartResponse(BaseModel):
    """Where to send the browser to attach a provider identity.

    A URL rather than a 302, because Settings starts this with fetch and a
    redirect there is followed opaquely by the browser instead of navigating
    the page. The client assigns it to ``window.location``.
    """

    authorize_url: str
