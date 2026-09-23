# ee/pocketpaw_ee/sites/slug.py — the name-based address a new Paw Site gets.
# Created: 2026-09-23 (VS-2, feat/sites-first-publish-slug).
#
# On the ``workers`` deploy lane every site is its own Cloudflare Worker, and the
# Worker's script name IS its public address: ``<name>.<account>.workers.dev``. Before
# VS-2 that name was ``paw-site-<24 hex>``. A new site now gets one built from its own
# name, e.g. ``acme-bakery``, the first time it publishes.
#
# This module is PURE: the rules for what a slug may be and the order candidates are
# tried in. No I/O, no Beanie, no Cloudflare. Whether a candidate is actually free
# (another site's slug, a script already in the Cloudflare account) is the caller's
# question -- see ``service._reserve_first_publish_slug``.
#
# Two guards, and why both:
#   * ``RESERVED`` / ``_RESERVED_PATTERNS``: names a multi-tenant host keeps for itself
#     (www, api, mail2, ...). A customer site called ``admin`` reads as ours.
#   * ``has_reserved_prefix``: anything starting ``paw-``. Our own Workers live in the
#     same account (``paw-sites-dispatch``, every legacy ``paw-site-<id>``), so a user
#     name there could shadow or overwrite one of them.
#
# Cloudflare allows a workers.dev Worker name up to 63 chars of letters, digits and
# hyphens with no leading or trailing hyphen. Our 40 sits inside that and leaves room
# for the ``<account>.workers.dev`` suffix in a readable URL.
from __future__ import annotations

import re
import secrets
import string
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass

MIN_LEN = 3
MAX_LEN = 40

# How many ``base-2`` .. ``base-N`` numbered tries before random suffixes.
_NUMBERED_UPTO = 6
# How many random-suffix tries after the numbered ones (and for the fallback).
_RANDOM_TRIES = 4
_RANDOM_LEN = 4
_BASE36 = string.ascii_lowercase + string.digits

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# Seeded from the common multi-tenant reserved-subdomain lists
# (jedireza/reserved-subdomains, sandeepshetty/subdomain-blacklist), trimmed to what
# plausibly matters for a hosted-site address, plus our own product names.


def _words(text: str) -> frozenset[str]:
    """Whitespace-separated names, ignoring ``#`` group-label lines."""
    return frozenset(
        word
        for line in text.splitlines()
        if not line.strip().startswith("#")
        for word in line.split()
    )


RESERVED: frozenset[str] = _words(
    """
    # infrastructure / protocol
    www api app apps admin administrator root origin mail
    email smtp imap pop pop3 mx ns dns ftp sftp ssh
    vpn proxy gateway server servers host hosting node edge
    cdn assets static media img images image files file
    upload uploads download downloads cache git svn ldap ntp
    autoconfig autodiscover webmail wpad localhost localdomain
    broadcasthost mailer-daemon noreply no-reply
    # accounts / auth
    auth oauth sso login logout signin signout signup
    register account accounts user users profile password
    session sessions verify invite invites join
    # product surfaces
    dashboard console portal panel control manage manager
    settings config status health metrics monitor monitoring
    analytics stats logs search home index www-data
    # docs / support / company
    docs doc documentation help support faq kb wiki guide
    blog news press about contact legal terms tos privacy
    security abuse postmaster hostmaster webmaster info team
    careers jobs partners community forum forums events
    feedback developer developers
    # money
    billing pay payment payments checkout invoice invoices
    shop store pricing plans subscribe subscription wallet
    # environments
    test tests testing staging stage dev develop development
    prod production preview demo sandbox beta alpha internal
    local qa uat
    # ours
    pocketpaw paw pawsites paw-sites sites site dispatch
    workers worker cloudflare ocean soul ripple studio
    # generic
    example null undefined none true false default public
    private system official mobile m web ws wss
    """
)

# Numbered variants of the infrastructure names: www2, mail10, ns1, mx3, ...
_RESERVED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"^{stem}\d*$")
    for stem in ("www", "mail", "ns", "mx", "smtp", "ftp", "imap", "pop", "dns", "api", "cdn")
)

_RESERVED_PREFIX = "paw-"


@dataclass(frozen=True)
class SlugError:
    """Why a slug was refused. ``reason`` is ``"invalid"`` or ``"reserved"``."""

    reason: str
    message: str


def normalize(raw: str) -> str:
    """Turn a display name into a slug-shaped string (possibly empty or too short).

    Lowercase, NFKD-decompose and drop what is not ASCII (so ``Café`` → ``cafe``),
    collapse every run of other characters to one hyphen, trim hyphens, and cut to
    ``MAX_LEN`` without leaving a trailing hyphen. It does NOT judge the result:
    an emoji-only name comes back ``""`` and ``validate`` says why that is unusable.
    """
    text = unicodedata.normalize("NFKD", (raw or "").lower())
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return _cut(text, MAX_LEN)


def _cut(text: str, limit: int) -> str:
    return text[:limit].rstrip("-")


def has_reserved_prefix(slug: str) -> bool:
    """True for anything under ``paw-``: the namespace our own Workers live in."""
    return slug.startswith(_RESERVED_PREFIX)


def _is_reserved(slug: str) -> bool:
    return (
        slug in RESERVED
        or has_reserved_prefix(slug)
        or any(p.match(slug) for p in _RESERVED_PATTERNS)
    )


def validate(slug: str) -> SlugError | None:
    """None when ``slug`` may be a site address, else the reason it may not."""
    if not (MIN_LEN <= len(slug) <= MAX_LEN) or not _SLUG_RE.match(slug):
        return SlugError(
            "invalid",
            f"Use {MIN_LEN}-{MAX_LEN} lowercase letters, digits or hyphens, "
            "not starting or ending with a hyphen.",
        )
    if _is_reserved(slug):
        return SlugError("reserved", "That name is reserved.")
    return None


def _random_suffix(rand: Callable[[str], str]) -> str:
    return "".join(rand(_BASE36) for _ in range(_RANDOM_LEN))


def _with_suffix(base: str, suffix: str) -> str:
    # Leave room for ``-<suffix>`` so every candidate stays inside MAX_LEN.
    return f"{_cut(base, MAX_LEN - len(suffix) - 1)}-{suffix}"


def candidates(name: str, *, rand: Callable[[str], str] = secrets.choice) -> Iterator[str]:
    """The slugs to try, in order, for a site called ``name``.

    ``base``, ``base-2`` .. ``base-6``, then a few ``base-<4 random base36>``. When the
    name does not yield a usable base (empty, too short, reserved, ``paw-``) the
    sequence is ``site-<4 random base36>`` instead. ``rand`` picks one character from
    a string; tests inject a deterministic one.
    """
    base = normalize(name)
    if validate(base) is not None:
        for _ in range(_RANDOM_TRIES):
            yield f"site-{_random_suffix(rand)}"
        return
    yield base
    for n in range(2, _NUMBERED_UPTO + 1):
        yield _with_suffix(base, str(n))
    for _ in range(_RANDOM_TRIES):
        yield _with_suffix(base, _random_suffix(rand))


__all__ = [
    "MAX_LEN",
    "MIN_LEN",
    "RESERVED",
    "SlugError",
    "candidates",
    "has_reserved_prefix",
    "normalize",
    "validate",
]
