# ee/pocketpaw_ee/sites/ownership.py — proof that a workspace controls an origin.
#
# WHY IT EXISTS. A later slice binds a concierge to a site the customer hosts
# themselves and CRAWLS that site's pages to ground the agent. Without a proof of
# control that feature is a crawler-for-hire: anyone could name a third party's
# host and have its content fetched and compiled into their own workspace. This
# module is the gate, and it gates the CRAWL, not merely an embed.
#
# CONSUMERS ASK ``verified_origin_record``, NOT THE BOOLEAN. All three — the bind
# (``sites.service.mint_foreign_site``), the crawl (``sites.foreign_grounding``)
# and the panel read (``sites.router._foreign_concierge_response``) — apply a
# freshness policy off ``verified_at``, which the boolean cannot express.
# ``verified_origin`` has no production caller today; see its docstring.
#
# THE FLOW. ``claim_origin`` mints a secret bound to (workspace, host) and stores
# it pending. The owner publishes it at ``/.well-known/paw-verify`` or as a
# ``<meta name="paw-verify">`` in the origin's <head>. ``verify_origin`` fetches
# the claimed host and compares. A failure writes NOTHING: the row is untouched,
# no Site is minted, no crawl is scheduled. An ALREADY-VERIFIED row is RE-PROBED
# rather than handed back, so ``verified_at`` can move — it is what the freshness
# window above is measured against, and a re-verify is the click a refusal asks
# the owner for.
#
# INVARIANTS A READER MUST NOT BREAK:
#   * A CLAIM IS LOOKED UP BY (workspace, host), NEVER BY TOKEN. The comparison is
#     always against the token THIS workspace was issued, which is the whole
#     reason a token cannot be replayed across workspaces: B publishing A's token
#     is matched against B's own row and mismatches.
#   * THE COMPARISON IS CONSTANT-TIME (``hmac.compare_digest``) and there is
#     exactly one of it. A ``==`` here leaks the token a byte at a time to anyone
#     who can time a verify.
#   * FETCHING GOES THROUGH ``safe_fetch.fetch_single_url`` AND NOWHERE ELSE. It
#     owns the SSRF pipeline (DNS pinned to a validated IP, all records must pass,
#     manual revalidated redirects, streamed size cap, no cookies/auth/proxies).
#     Do not add an httpx call to this file and do not relax its caps.
#   * THE HOP CHAIN IS PINNED TO THE CLAIMED HOST (``allowed_host``). Following a
#     redirect off-host would let a parked domain that 30x-es onto a shared
#     service be "proved" by anyone who can put a file on that service. The proof
#     must be served BY the origin being claimed.
#   * A LITERAL IP IS NEVER A CLAIMABLE ORIGIN, refused in ``normalize_claim_host``
#     before any DB write or DNS lookup. A host that merely RESOLVES private is
#     refused one layer down by safe_fetch, still before a request is issued.
#   * THE META PROOF IS ACCEPTED ONLY FROM <head>. A site whose homepage renders
#     user-supplied HTML would otherwise let a commenter inject the tag and prove
#     ownership of somebody else's domain.
#
# ROBOTS.TXT IS DELIBERATELY NOT CONSULTED, and this is a decision rather than an
# omission (robots handling lives in ``url_crawler``, so nothing here inherits
# it). robots governs autonomous DISCOVERY of content nobody pointed us at. This
# is two GETs of URLs the claimant named, on their own explicit button press, to
# read a file they just placed there for us — the same class of act as a browser
# opening the page. A robots rule must not be able to make an owner's own
# verification unfixable. Do not add robots checking to ``safe_fetch``.
#
# ERROR VOCABULARY. safe_fetch raises the ``sites.import_*`` codes because the
# import endpoint's 422 body is a wire contract keyed on them. Those are NOT this
# surface's codes: they are caught at this boundary and re-raised as
# ``sites.origin_*`` (host_invalid, host_forbidden, probe_failed,
# token_missing, token_mismatch, claim_expired). Never rename them upstream.
# The one code outside that family is ``site_origin_claim.not_found``, which the
# shared ``NotFound`` builds for every 404 in this codebase.

"""Domain-ownership claims and verification for Paw Sites origins."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import httpx
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites.safe_fetch import FetchError, FetchResult, fetch_single_url

logger = logging.getLogger(__name__)

# An honest identity for a one-file ownership probe. It is NOT the importer's UA:
# that string goes to the customer's own server, their robots groups and WAF
# rules match on it, and a verification announcing itself as a site-import
# crawler is a lie that a blocked customer cannot debug.
VERIFIER_USER_AGENT = "PawSitesVerifier/1.0 (+https://pocketpaw.dev; domain-ownership check)"

# Where the owner may publish the token.
WELL_KNOWN_PATH = "/.well-known/paw-verify"
META_NAME = "paw-verify"

TOKEN_PREFIX = "pawverify-"
# How long an UNREDEEMED token stays valid. Bounded so an abandoned claim cannot
# be redeemed by whoever controls the host months later.
CLAIM_TTL = timedelta(days=7)

# Probe budgets. Both sit well under safe_fetch's own 10MB/10s ceilings — a probe
# reads one small file or one page's <head>, and a human is waiting on a button.
# Tightening here is fine; loosening past safe_fetch's caps is not.
_WELL_KNOWN_MAX_BYTES = 64 * 1024
_HOMEPAGE_MAX_BYTES = 1024 * 1024
_PROBE_TIMEOUT_SEC = 8.0

# How much of a token file we are willing to scan. A proof is one short line; a
# file with thousands of them is someone spraying, not publishing.
_MAX_TOKEN_LINES = 20

_MAX_HOST_LENGTH = 253
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_STATUS_PENDING = "pending"
_STATUS_VERIFIED = "verified"

METHOD_WELL_KNOWN = "well-known"
METHOD_META = "meta"


# --------------------------------------------------------------------------- #
# Host normalization — the first refusal, before any write or any lookup
# --------------------------------------------------------------------------- #


def normalize_claim_host(host: str) -> str:
    """Return the canonical form of a claimable host, or raise.

    Lowercased, trailing dot stripped. Refused: anything carrying URL syntax (a
    scheme, path, port, query or credentials), a single label (``localhost`` and
    every other internal name), a label that is not a legal DNS label, and EVERY
    literal IP address.

    The literal-IP refusal is the load-bearing one and it is deliberately
    blanket. A non-public literal (loopback, RFC1918, link-local, the
    169.254.169.254 metadata address) must die here, before a row is written or a
    packet sent — that is this function's security job. A PUBLIC literal is
    refused too, because an origin claim is a claim on a NAME: a bare address has
    no registrant to prove anything about, and allowing one would leave the
    forbidden-address check as the only thing between a claim and an arbitrary
    host on the internet.
    """
    candidate = (host or "").strip().lower().rstrip(".")
    if not candidate or len(candidate) > _MAX_HOST_LENGTH:
        raise ValidationError(
            "sites.origin_host_invalid", "A domain name is required to claim an origin."
        )
    if any(ch in candidate for ch in "/\\?#@:") or any(ch.isspace() for ch in candidate):
        raise ValidationError(
            "sites.origin_host_invalid",
            "Claim a bare domain name — no scheme, port, path or credentials.",
        )
    try:
        literal = ipaddress.ip_address(candidate)
    except ValueError:
        literal = None
    if literal is not None:
        # The reason string is safe_fetch's; the refusal happens HERE, with
        # nothing written and no packet sent.
        from pocketpaw_ee.sites.safe_fetch import _forbidden_ip_reason

        reason = _forbidden_ip_reason(literal)
        if reason:
            raise ValidationError(
                "sites.origin_host_forbidden",
                f"That address is not a public origin ({reason}).",
            )
        raise ValidationError(
            "sites.origin_host_invalid", "Claim a domain name, not an IP address."
        )
    labels = candidate.split(".")
    if len(labels) < 2:
        raise ValidationError(
            "sites.origin_host_invalid",
            "Claim a full domain name (example.com), not a single-label host.",
        )
    for label in labels:
        if not _LABEL_RE.match(label):
            raise ValidationError(
                "sites.origin_host_invalid",
                "That is not a valid domain name. Use the punycode form for "
                "internationalized domains.",
            )
    return candidate


# --------------------------------------------------------------------------- #
# Issue
# --------------------------------------------------------------------------- #


async def claim_origin(*, workspace_id: str, user_id: str, host: str) -> SiteOriginClaim:
    """Mint (or re-mint) this workspace's pending token for ``host``.

    One row per (workspace, host): re-claiming REPLACES the token and restarts
    the window, so an owner who lost the value can ask again without a second
    row. Re-claiming an ALREADY VERIFIED origin is a no-op that returns the
    verified row — re-issuing there would quietly unprove a live binding.
    """
    normalized = normalize_claim_host(host)
    existing = await SiteOriginClaim.find_one(
        SiteOriginClaim.workspace == workspace_id, SiteOriginClaim.host == normalized
    )
    if existing is not None and existing.status == _STATUS_VERIFIED:
        return existing
    now = datetime.now(UTC)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    if existing is None:
        claim = SiteOriginClaim(
            workspace=workspace_id,
            host=normalized,
            token=token,
            status=_STATUS_PENDING,
            issued_at=now,
            expires_at=now + CLAIM_TTL,
            issued_by=user_id,
        )
        try:
            await claim.insert()
        except DuplicateKeyError:
            # Two clicks raced the read above. The unique index is the thing
            # that keeps one row per (workspace, host); this just turns the
            # loser into a re-read instead of a 500. NOT covered by the suite —
            # mongomock does not enforce unique indexes, so there is no way to
            # provoke it deterministically here.
            existing = await SiteOriginClaim.find_one(
                SiteOriginClaim.workspace == workspace_id,
                SiteOriginClaim.host == normalized,
            )
            if existing is None:  # pragma: no cover - the index said otherwise
                raise
        else:
            return claim
    existing.token = token
    existing.status = _STATUS_PENDING
    existing.issued_at = now
    existing.expires_at = now + CLAIM_TTL
    existing.issued_by = user_id
    existing.method = ""
    existing.verified_at = None
    await existing.save()
    return existing


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #


class _HeadMetaScanner(HTMLParser):
    """Collect ``<meta name="paw-verify" content=...>`` values found in <head>.

    IT STOPS AT THE END OF <head> ON PURPOSE. A site that renders user-supplied
    HTML in its body would otherwise let a commenter publish the tag and prove
    ownership of a domain they do not control; injecting into <head> is a much
    rarer bug. A document with no explicit <head> is scanned until <body>.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        if tag == "body":
            self._done = True
            return
        if tag != "meta":
            return
        pairs = {key.lower(): (value or "") for key, value in attrs}
        if pairs.get("name", "").strip().lower() == META_NAME:
            self.values.append(pairs.get("content", "").strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._done = True


def _token_lines(body: bytes) -> list[str]:
    """Candidate tokens from a plain-text proof file, bounded."""
    text = body.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines()[:_MAX_TOKEN_LINES]]
    return [line for line in lines if line]


def _matches(candidates: list[str], token: str) -> bool:
    """Does any candidate equal ``token``? Constant-time per comparison.

    THIS IS THE COMPARISON. There is exactly one of it in this module and it uses
    ``hmac.compare_digest``; a ``==`` would leak the token to a timing attacker
    one byte at a time.

    Compared as BYTES, not as str. ``compare_digest`` raises TypeError on a str
    holding anything outside ASCII, and the candidate is a hostile party's file
    contents — so the str form turns a page of emoji into a 500 on a security
    endpoint.
    """
    expected = token.encode("utf-8")
    return any(hmac.compare_digest(candidate.encode("utf-8"), expected) for candidate in candidates)


async def _probe(
    url: str,
    *,
    host: str,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None,
    resolver: Callable[[str], Awaitable[list[str]]] | None,
) -> FetchResult:
    """One GET through the ONE SSRF-hardened fetch, in this module's vocabulary.

    Every rejection safe_fetch can raise is re-raised as ``sites.origin_*`` here.
    The upstream codes stay as they are: they are the import endpoint's wire
    contract, not ours.

    ``FetchBudgetExceeded`` gets no handler of its own. It cannot arrive through
    ``fetch_single_url`` — that passes ``total_byte_cap == per_fetch_cap`` and the
    per-fetch check runs first — so catching it separately would be a branch
    pretending to cover a path that does not exist. It is a ``FetchError``
    subclass and the clause below would catch it regardless.
    """
    try:
        return await fetch_single_url(
            url,
            max_bytes=max_bytes,
            timeout_sec=_PROBE_TIMEOUT_SEC,
            allowed_host=host,
            user_agent=VERIFIER_USER_AGENT,
            transport=transport,
            resolver=resolver,
        )
    except ValidationError as exc:
        # The target was REFUSED — bad shape, or it resolves somewhere non-public.
        # No request was issued.
        if exc.code == "sites.import_url_forbidden":
            raise ValidationError(
                "sites.origin_host_forbidden",
                "That domain does not resolve to a public address, so it cannot be verified.",
            ) from exc
        raise ValidationError(
            "sites.origin_host_invalid", "That domain cannot be fetched for verification."
        ) from exc
    except FetchError as exc:
        # DNS failure, an oversized body, too many hops, or a hop that left the
        # claimed host. ONE code out: the owner's next action is the same for all
        # of them, and upstream detail on this surface is a probing oracle.
        logger.info("sites.verify: probe failed for %s (%s)", host, exc.code)
        raise ValidationError(
            "sites.origin_probe_failed",
            "Could not read the verification file from that domain.",
        ) from exc
    except httpx.HTTPError as exc:
        logger.info("sites.verify: transport failure for %s", host)
        raise ValidationError(
            "sites.origin_probe_failed",
            "Could not reach that domain to verify it.",
        ) from exc


async def verify_origin(
    *,
    workspace_id: str,
    host: str,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
) -> SiteOriginClaim:
    """Prove this workspace controls ``host``, or raise and leave nothing behind.

    At most two GETs: the well-known file, then the homepage's <head>. A failure
    persists NOTHING — the claim keeps the status it had, no Site row is minted,
    and no crawl is scheduled from here (this module has a path to neither).

    RUNS ON AN ALREADY-VERIFIED CLAIM TOO, and that is the point: the proof is
    re-fetched and ``verified_at`` moves, which is the only way an owner can
    answer a consumer that has refused their 31-day-old proof.

    ``transport`` / ``resolver`` are safe_fetch's test seams, threaded so the
    suite never opens a socket.
    """
    normalized = normalize_claim_host(host)
    claim = await SiteOriginClaim.find_one(
        SiteOriginClaim.workspace == workspace_id, SiteOriginClaim.host == normalized
    )
    if claim is None:
        # Looked up by (workspace, host). A token issued to ANOTHER workspace is
        # not reachable from here — that is the replay guard, and it is
        # structural rather than a check somebody could forget to write.
        # ``NotFound(resource, id)`` builds the code as ``<resource>.not_found``
        # — the codebase's 404 convention, so this is the one code on this
        # surface outside the ``sites.origin_*`` family.
        raise NotFound("site_origin_claim", normalized)
    # A VERIFIED CLAIM IS RE-PROBED, NOT HANDED BACK. ``_record_verified`` is the
    # only writer of ``verified_at``, so returning early here froze that date at the
    # first proof for the life of the row — while three consumers (the mint, the
    # grounding crawl, the concierge panel) refuse a proof older than 30 days and
    # tell the owner to verify the domain again. That click landed here and did
    # nothing, so a paid concierge could reach a state where its knowledge can never
    # refresh while the renewal sweep keeps charging for it.
    #
    # UNCONDITIONALLY, NOT ONLY WHEN STALE. Which proofs are too old is not a
    # question this module answers: the window lives with the feature acting on the
    # proof (``foreign_grounding.VERIFICATION_MAX_AGE``), and reaching for a
    # consumer's number here to decide whether to fetch would put one policy in two
    # files. Two GETs on the owner's own button press is the cheaper side of that
    # trade.
    #
    # A FAILED RE-PROBE UN-PROVES NOTHING. Every raise below persists nothing, so an
    # origin that has stopped serving its token keeps its row and the binding that
    # row backs — the owner is told what is wrong instead of being cut off. Only a
    # successful fetch writes, which is what keeps the re-stamp a proof rather than
    # a touch.
    if claim.status != _STATUS_VERIFIED and _expires_at(claim) <= datetime.now(UTC):
        # THE TTL BOUNDS AN UNREDEEMED INVITATION, NOT A PUBLISHED PROOF. A token
        # nobody ever used expires after ``CLAIM_TTL`` so a leaked one cannot be
        # redeemed months later. One the owner DID publish, and which their origin
        # still serves, is re-checked on every call above. Applying the TTL to a
        # verified row would be worse than the bug it replaces: CLAIM_TTL (7 days)
        # is shorter than the freshness window (30), so every re-verify would demand
        # a fresh token and a freshly published file — not the one click per invoice
        # the window was argued on.
        raise ValidationError(
            "sites.origin_claim_expired",
            "That verification token has expired. Claim the domain again for a new one.",
        )

    seen_a_candidate = False

    # THE FIRST PROBE'S FAILURE IS NOT FATAL. Plenty of real hosts answer an
    # unknown path with a redirect to a CDN error page on another domain, which
    # the host pin refuses — and that must not make the meta method, which would
    # have worked, unreachable. If the homepage probe fails too its error is the
    # one that surfaces; both map into the same small ``sites.origin_*`` set.
    try:
        well_known: FetchResult | None = await _probe(
            f"https://{normalized}{WELL_KNOWN_PATH}",
            host=normalized,
            max_bytes=_WELL_KNOWN_MAX_BYTES,
            transport=transport,
            resolver=resolver,
        )
    except ValidationError:
        well_known = None
    if well_known is not None and 200 <= well_known.status < 300:
        candidates = _token_lines(well_known.body)
        seen_a_candidate = seen_a_candidate or bool(candidates)
        if _matches(candidates, claim.token):
            return await _record_verified(claim, METHOD_WELL_KNOWN)

    homepage = await _probe(
        f"https://{normalized}/",
        host=normalized,
        max_bytes=_HOMEPAGE_MAX_BYTES,
        transport=transport,
        resolver=resolver,
    )
    if 200 <= homepage.status < 300:
        scanner = _HeadMetaScanner()
        scanner.feed(homepage.body.decode("utf-8", errors="replace"))
        candidates = [value for value in scanner.values if value]
        seen_a_candidate = seen_a_candidate or bool(candidates)
        if _matches(candidates, claim.token):
            return await _record_verified(claim, METHOD_META)

    if seen_a_candidate:
        raise ValidationError(
            "sites.origin_token_mismatch",
            "The token published on that domain does not match the one issued for this workspace.",
        )
    raise ValidationError(
        "sites.origin_token_missing",
        "No verification token was found on that domain.",
    )


async def _record_verified(claim: SiteOriginClaim, method: str) -> SiteOriginClaim:
    """The ONLY write on the success path. Nothing else persists a verification."""
    claim.status = _STATUS_VERIFIED
    claim.method = method
    claim.verified_at = datetime.now(UTC)
    await claim.save()
    logger.info(
        "sites.verify: %s verified for workspace %s via %s", claim.host, claim.workspace, method
    )
    return claim


def _expires_at(claim: SiteOriginClaim) -> datetime:
    """``expires_at`` as an aware UTC stamp — Mongo hands naive datetimes back."""
    stamp = claim.expires_at
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The predicate the mint and crawl slices consume
# --------------------------------------------------------------------------- #


async def verified_origin(workspace_id: str, host: str) -> bool:
    """Has ``workspace_id`` proved it controls ``host``?

    THE GATE FAILS CLOSED. A host this module would not even accept as a claim
    (a literal IP, a single label, URL syntax) answers False rather than raising,
    because a caller using this to decide whether to fetch someone's pages must
    never be handed an exception it might treat as "not my problem".

    NO PRODUCTION CALLER TODAY. The bind and the crawl both need ``verified_at``
    to apply their 30-day freshness rule, so both ask
    ``verified_origin_record``. This stays as the boolean form of the same
    question and is exercised by tests/ee/sites/test_domain_ownership.py; a new
    consumer that genuinely does not care how old a proof is may use it, but it
    should say why in a comment, because "verified once" is rarely the question.
    """
    return (await verified_origin_record(workspace_id, host)) is not None


async def verified_origin_record(workspace_id: str, host: str) -> SiteOriginClaim | None:
    """The verified claim row for (workspace, host), or None.

    Returned beside the boolean so a consumer can apply its own freshness policy
    off ``verified_at``. This module still owns no TTL — how often control must be
    re-proved is a decision for the feature that acts on it, and inventing one here
    would silently break a live binding on its anniversary. What the module DOES
    own is the way back: ``verify_origin`` re-probes a verified row and re-stamps
    ``verified_at``, so a consumer that refuses an old proof is refusing something
    the owner can fix rather than a dead end.
    """
    try:
        normalized = normalize_claim_host(host)
    except ValidationError:
        return None
    claim = await SiteOriginClaim.find_one(
        SiteOriginClaim.workspace == workspace_id, SiteOriginClaim.host == normalized
    )
    if claim is None or claim.status != _STATUS_VERIFIED:
        return None
    return claim
