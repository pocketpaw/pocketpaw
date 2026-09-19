# tests/ee/sites/test_domain_ownership.py — the guard suite for ownership.py, the
# proof that a workspace controls an origin before anything crawls it.
#
# This is a security gate, so every test here asserts a MECHANISM rather than
# "it raised":
#
#   * a non-public claim is refused with ZERO requests issued and ZERO rows
#     written — ``seen == []`` and an empty collection are what separate
#     "refused at validation" from "refused after we already fetched it";
#   * a FAILED verification leaves no Site row and reaches no crawl entry point,
#     asserted against the real collection and a patched crawler, because
#     "it returned an error" is also what a version that half-ran would do;
#   * a token issued to workspace A does not verify the same host for workspace
#     B, with the site actually serving A's token — the replay the (workspace,
#     host) lookup exists to stop;
#   * the probe announces the VERIFIER identity, read off the request the
#     transport saw, not off the constant;
#   * a meta tag in <body> does not prove anything, because a homepage that
#     renders user-supplied HTML would otherwise verify a stranger's domain.
#
# All network is mocked (httpx.MockTransport + a dict-backed resolver): nothing
# here opens a socket. Fixture IPs must be genuinely public — this Python
# classifies the RFC 5737 documentation ranges as private, so a fixture drawn
# from 203.0.113.0/24 fails for the wrong reason.
# tests/mutations/ownership_verification.json is the other half of this gate: it
# deletes the token comparison and the pre-fetch IP check on purpose and expects
# these tests to notice.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites import ownership

pytestmark = pytest.mark.asyncio

_WS_A = "ws-alpha"
_WS_B = "ws-beta"
_HOST = "customer.example"

_PUBLIC = {
    _HOST: ["93.184.216.34"],
    "other.example": ["93.184.216.35"],
}
_PRIVATE = {_HOST: ["10.0.0.7"]}


def _resolver(table: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError(f"no DNS for {host}")
        return table[host]

    return resolve


def _transport(routes: dict[str, httpx.Response], seen: list[httpx.Request]):
    """MockTransport routing on PATH. The pinned request carries the validated IP
    in the URL, so the claimed host only ever appears in the Host header."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes.get(request.url.path, httpx.Response(404, content=b"not found"))

    return httpx.MockTransport(handler)


def _well_known(body: bytes) -> dict[str, httpx.Response]:
    return {
        ownership.WELL_KNOWN_PATH: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=body
        )
    }


def _page(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())


async def _verify(host=_HOST, *, workspace=_WS_A, routes=None, table=None, seen=None):
    return await ownership.verify_origin(
        workspace_id=workspace,
        host=host,
        transport=_transport(routes or {}, seen if seen is not None else []),
        resolver=_resolver(_PUBLIC if table is None else table),
    )


# --------------------------------------------------------------------------- #
# Criterion 1 — a matching token verifies and the origin is persisted
# --------------------------------------------------------------------------- #


async def test_a_matching_well_known_token_verifies_and_persists_the_origin(beanie_test_db):
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    assert claim.status == "pending"

    verified = await _verify(routes=_well_known(claim.token.encode()))

    assert verified.status == "verified"
    assert verified.method == ownership.METHOD_WELL_KNOWN
    assert verified.verified_at is not None
    # Persisted, not merely returned — the predicate reads the DB.
    assert await ownership.verified_origin(_WS_A, _HOST) is True
    reloaded = await SiteOriginClaim.find_one(SiteOriginClaim.workspace == _WS_A)
    assert reloaded is not None and reloaded.status == "verified"


async def test_a_meta_tag_in_head_verifies(beanie_test_db):
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    routes = {
        "/": _page(
            f'<html><head><meta name="paw-verify" content="{claim.token}"></head>'
            "<body>hello</body></html>"
        )
    }

    verified = await _verify(routes=routes)

    assert verified.method == ownership.METHOD_META
    assert await ownership.verified_origin(_WS_A, _HOST) is True


async def test_the_claimed_host_is_normalized_so_the_predicate_answers_either_spelling(
    beanie_test_db,
):
    """A claim on ``Customer.Example.`` and a lookup of ``customer.example`` are
    the same origin. Without one normalization on both sides a verified host
    silently fails the gate that guards the crawl."""
    claim = await ownership.claim_origin(
        workspace_id=_WS_A, user_id="u1", host="  Customer.Example.  "
    )
    assert claim.host == _HOST

    await _verify(routes=_well_known(claim.token.encode()))

    assert await ownership.verified_origin(_WS_A, "CUSTOMER.EXAMPLE") is True


# --------------------------------------------------------------------------- #
# Criterion 2 — missing, stale and mismatched tokens refuse
# --------------------------------------------------------------------------- #


async def test_nothing_published_refuses_as_token_missing(beanie_test_db):
    await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    with pytest.raises(ValidationError) as exc:
        await _verify(routes={})

    assert exc.value.code == "sites.origin_token_missing"
    assert await ownership.verified_origin(_WS_A, _HOST) is False


async def test_a_different_token_refuses_as_mismatch_not_missing(beanie_test_db):
    """The two refusals are distinct on purpose: "we found nothing" and "we found
    the wrong value" send the owner to different fixes."""
    await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known(b"pawverify-someone-elses-value"))

    assert exc.value.code == "sites.origin_token_mismatch"
    assert await ownership.verified_origin(_WS_A, _HOST) is False


async def test_a_token_that_only_shares_a_prefix_refuses(beanie_test_db):
    """The comparison is whole-value. A truncated token must not pass, which is
    the case a ``startswith`` or a sloppy ``in`` would wave through."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known(claim.token[:-4].encode()))

    assert exc.value.code == "sites.origin_token_mismatch"


async def test_a_non_ascii_proof_file_refuses_instead_of_crashing(beanie_test_db):
    """The candidate is a hostile party's file contents. ``compare_digest`` raises
    TypeError on a str holding anything outside ASCII, so comparing as str would
    turn a page of emoji into a 500 on a security endpoint."""
    await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known("☃ α".encode()))

    assert exc.value.code == "sites.origin_token_mismatch"


async def test_an_expired_claim_refuses_without_fetching_anything(beanie_test_db):
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    claim.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await claim.save()
    seen: list[httpx.Request] = []

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known(claim.token.encode()), seen=seen)

    assert exc.value.code == "sites.origin_claim_expired"
    # The stale token is on the site and still does not verify, and we did not
    # even look: an expired claim is decided before the probe.
    assert seen == []
    assert await ownership.verified_origin(_WS_A, _HOST) is False


async def test_verifying_a_host_that_was_never_claimed_refuses(beanie_test_db):
    seen: list[httpx.Request] = []

    with pytest.raises(NotFound) as exc:
        await _verify(routes=_well_known(b"anything"), seen=seen)

    assert exc.value.code == "site_origin_claim.not_found"
    assert exc.value.status_code == 404
    assert seen == []


async def test_a_meta_tag_in_the_body_does_not_prove_ownership(beanie_test_db):
    """A homepage that renders user-supplied HTML would otherwise let a commenter
    verify somebody else's domain. Only <head> counts."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    routes = {
        "/": _page(
            "<html><head><title>hi</title></head>"
            f'<body><div class="comment"><meta name="paw-verify" '
            f'content="{claim.token}"></div></body></html>'
        )
    }

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=routes)

    assert exc.value.code == "sites.origin_token_missing"
    assert await ownership.verified_origin(_WS_A, _HOST) is False


async def test_a_meta_tag_after_head_closes_does_not_prove_ownership(beanie_test_db):
    """The </head> stop on its own. This document never opens a <body>, so the
    other half of the boundary cannot cover for it — without this stop the tag
    below the head counts and the scan is back to trusting the whole page."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    routes = {
        "/": _page(
            "<html><head><title>hi</title></head>"
            f'<meta name="paw-verify" content="{claim.token}"></html>'
        )
    }

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=routes)

    assert exc.value.code == "sites.origin_token_missing"


async def test_a_meta_tag_in_a_headless_document_body_does_not_prove_ownership(beanie_test_db):
    """The <body> stop on its own. This document has no <head> to close, which is
    what real user-generated pages served by template engines often look like."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    routes = {
        "/": _page(f'<html><body><meta name="paw-verify" content="{claim.token}"></body></html>')
    }

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=routes)

    assert exc.value.code == "sites.origin_token_missing"


# --------------------------------------------------------------------------- #
# Criterion 3 — a non-public claim refuses at validation, before any fetch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",  # the cloud metadata address
        "::1",
        "fd00::1",
        "::ffff:127.0.0.1",  # loopback wearing a v6 costume
        "localhost",
        "http://customer.example/",
        "customer.example:8080",
        "user:pw@customer.example",
    ],
)
async def test_a_non_public_or_malformed_claim_is_refused_before_any_row_or_packet(
    beanie_test_db, host
):
    """Refused during normalization: nothing written, nothing resolved, nothing
    fetched. ``claim_origin`` is the earliest point this can be stopped, and the
    empty collection is what proves it stopped there."""
    with pytest.raises(ValidationError) as exc:
        await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=host)

    assert exc.value.code in ("sites.origin_host_invalid", "sites.origin_host_forbidden")
    assert await SiteOriginClaim.find_all().count() == 0


async def test_a_public_literal_ip_is_not_a_claimable_origin(beanie_test_db):
    """A bare address has no registrant to prove anything about. Refusing every
    literal keeps the forbidden-range check from being the only thing between a
    claim and an arbitrary host."""
    with pytest.raises(ValidationError) as exc:
        await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host="93.184.216.34")

    assert exc.value.code == "sites.origin_host_invalid"


async def test_a_host_that_resolves_private_is_refused_with_no_request_issued(beanie_test_db):
    """The claim passes normalization — it is a real domain name — and dies at the
    DNS check inside safe_fetch, still before a packet. This is the rebinding
    case that a literal-IP check alone cannot catch."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    seen: list[httpx.Request] = []

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known(claim.token.encode()), table=_PRIVATE, seen=seen)

    assert exc.value.code == "sites.origin_host_forbidden"
    assert seen == []
    assert await ownership.verified_origin(_WS_A, _HOST) is False


async def test_the_probe_is_pinned_to_the_claimed_host_across_redirects(beanie_test_db):
    """A parked domain that 30x-es onto a shared service must not be provable by
    whoever can put a file on that service."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    offsite = httpx.Response(302, headers={"location": "https://other.example/token.txt"})
    routes = {
        # BOTH probes are sent off-host, and the foreign host really does serve a
        # valid token. Unpinned, the first hop alone would verify this domain.
        ownership.WELL_KNOWN_PATH: offsite,
        "/": offsite,
        "/token.txt": httpx.Response(200, content=claim.token.encode()),
    }

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=routes)

    assert exc.value.code == "sites.origin_probe_failed"
    assert await ownership.verified_origin(_WS_A, _HOST) is False


# --------------------------------------------------------------------------- #
# Criterion 4 — a failed verification leaves no Site row and performs no crawl
# --------------------------------------------------------------------------- #


async def test_a_failed_verification_mints_no_site_and_runs_no_crawl(beanie_test_db, monkeypatch):
    """The refusal is not the assertion here. The assertion is that nothing was
    left behind: no Site document, no crawl, and a claim still pending."""
    from pocketpaw_ee.sites import import_service, url_crawler

    crawls: list[object] = []

    async def _forbidden_crawl(*args, **kwargs):
        crawls.append((args, kwargs))
        raise AssertionError("verification must never reach the crawler")

    monkeypatch.setattr(url_crawler, "crawl_site", _forbidden_crawl, raising=False)
    monkeypatch.setattr(import_service, "crawl_site_from_url", _forbidden_crawl, raising=False)

    await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    with pytest.raises(ValidationError):
        await _verify(routes=_well_known(b"pawverify-wrong"))

    assert await Site.find_all().count() == 0
    assert crawls == []
    claim = await SiteOriginClaim.find_one(SiteOriginClaim.workspace == _WS_A)
    assert claim is not None
    assert claim.status == "pending"
    assert claim.verified_at is None
    assert claim.method == ""


async def test_a_refused_claim_mints_no_site(beanie_test_db):
    with pytest.raises(ValidationError):
        await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host="169.254.169.254")

    assert await Site.find_all().count() == 0
    assert await SiteOriginClaim.find_all().count() == 0


# --------------------------------------------------------------------------- #
# Criterion 5 — a token cannot be replayed across workspaces
# --------------------------------------------------------------------------- #


async def test_workspace_a_token_on_the_site_does_not_verify_the_host_for_workspace_b(
    beanie_test_db,
):
    """The site really is serving a valid token — A's. B claims the same host,
    gets its OWN token, and must not be able to ride A's proof. The lookup is by
    (workspace, host), so B's attempt is compared against B's row."""
    claim_a = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    claim_b = await ownership.claim_origin(workspace_id=_WS_B, user_id="u2", host=_HOST)
    assert claim_a.token != claim_b.token
    site_serves_a_token = _well_known(claim_a.token.encode())

    with pytest.raises(ValidationError) as exc:
        await _verify(workspace=_WS_B, routes=site_serves_a_token)

    assert exc.value.code == "sites.origin_token_mismatch"
    assert await ownership.verified_origin(_WS_B, _HOST) is False

    # And the same bytes verify for A, so the refusal above is about the
    # workspace binding and not about the token being unreadable.
    await _verify(workspace=_WS_A, routes=site_serves_a_token)
    assert await ownership.verified_origin(_WS_A, _HOST) is True


async def test_a_verified_origin_in_one_workspace_is_not_verified_in_another(beanie_test_db):
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    await _verify(routes=_well_known(claim.token.encode()))

    assert await ownership.verified_origin(_WS_A, _HOST) is True
    assert await ownership.verified_origin(_WS_B, _HOST) is False


async def test_workspace_b_cannot_verify_a_host_it_never_claimed(beanie_test_db):
    claim_a = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    seen: list[httpx.Request] = []

    with pytest.raises(NotFound):
        await _verify(workspace=_WS_B, routes=_well_known(claim_a.token.encode()), seen=seen)

    assert seen == []


# --------------------------------------------------------------------------- #
# Criterion 6 — the probe announces an honest verifier identity
# --------------------------------------------------------------------------- #


async def test_the_probe_sends_the_verifier_user_agent_not_the_importers(beanie_test_db):
    """Read off the request the transport actually saw. A verification announcing
    itself as a site-import crawler misleads the operator, and a customer whose
    robots.txt blocks that name would watch this fail with no guessable cause."""
    from pocketpaw_ee.sites.safe_fetch import USER_AGENT as IMPORTER_UA

    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    seen: list[httpx.Request] = []

    await _verify(routes=_well_known(claim.token.encode()), seen=seen)

    assert seen, "the probe issued no request"
    sent = seen[0].headers["user-agent"]
    assert sent == ownership.VERIFIER_USER_AGENT
    assert sent != IMPORTER_UA
    assert "crawler" not in sent


async def test_the_import_crawler_still_sends_its_own_user_agent(beanie_test_db):
    """The parameter defaults to the importer's identity, so the crawler's
    outbound behaviour is unchanged by this slice."""
    from pocketpaw_ee.sites.safe_fetch import USER_AGENT as IMPORTER_UA
    from pocketpaw_ee.sites.safe_fetch import fetch_single_url

    seen: list[httpx.Request] = []
    routes = {"/": httpx.Response(200, content=b"ok")}

    await fetch_single_url(
        f"https://{_HOST}/",
        transport=_transport(routes, seen),
        resolver=_resolver(_PUBLIC),
    )

    assert seen[0].headers["user-agent"] == IMPORTER_UA


# --------------------------------------------------------------------------- #
# The predicate, and what re-claiming does
# --------------------------------------------------------------------------- #


async def test_the_predicate_fails_closed_on_a_host_it_would_never_accept(beanie_test_db):
    """A caller deciding whether to fetch somebody's pages must get False, not an
    exception it might treat as somebody else's problem."""
    for host in ("127.0.0.1", "localhost", "", "not a host"):
        assert await ownership.verified_origin(_WS_A, host) is False


async def test_reclaiming_a_pending_origin_remints_the_token_and_invalidates_the_old_one(
    beanie_test_db,
):
    first = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    first_token = first.token
    second = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    assert second.token != first_token
    assert await SiteOriginClaim.find_all().count() == 1

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known(first_token.encode()))
    assert exc.value.code == "sites.origin_token_mismatch"

    await _verify(routes=_well_known(second.token.encode()))
    assert await ownership.verified_origin(_WS_A, _HOST) is True


async def test_reclaiming_a_verified_origin_does_not_unprove_it(beanie_test_db):
    """Fresh or stale, a re-claim returns the row untouched — and the SAME token.

    The fresh half is the original assertion and still holds: re-minting there
    would unprove a live binding for no reason.

    The stale half is why the fresh half is safe now that ``verified_at`` is
    load-bearing. A re-claim that re-minted a stale row's token would demand the
    owner publish a NEW file before they could re-verify, and CLAIM_TTL (7 days)
    is shorter than the freshness window (30), so that would be the price of
    every renewal. Keeping the token is what makes ``verify_origin``'s re-probe a
    single click: the file the owner published long ago is still the file we
    compare.
    """
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    token = claim.token
    await _verify(routes=_well_known(token.encode()))

    again = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    assert again.status == "verified"
    assert again.token == token
    assert await ownership.verified_origin(_WS_A, _HOST) is True

    await _age_the_proof(days=31)
    stale_again = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)

    assert stale_again.status == "verified"
    assert stale_again.token == token, (
        "a stale row must keep its token, or re-verifying costs the owner a new file"
    )


# --------------------------------------------------------------------------- #
# Re-verification — the way back from a proof a consumer has refused
#
# ``verified_at`` became load-bearing when the 30-day freshness window landed:
# the mint, the grounding crawl and the concierge panel all refuse a proof older
# than it, and all three tell the owner to verify the domain again. Before these
# tests that click was a no-op — ``verify_origin`` returned early on a verified
# row and ``_record_verified`` is the only writer of ``verified_at`` — so the
# date froze at the first proof and a paid concierge could reach a state whose
# knowledge can never refresh while the renewal sweep keeps charging for it.
# --------------------------------------------------------------------------- #


async def _age_the_proof(*, days: int) -> SiteOriginClaim:
    """Backdate a verified row's proof AND its token TTL by ``days``.

    Both, deliberately. CLAIM_TTL is 7 days and the freshness window is 30, so
    any real 31-day-old proof sits on a row whose ``expires_at`` passed weeks
    ago. A fixture that aged only ``verified_at`` would describe a state that
    cannot occur, and would pass against code that still refuses an expired row.
    """
    claim = await SiteOriginClaim.find_one(SiteOriginClaim.workspace == _WS_A)
    assert claim is not None
    now = datetime.now(UTC)
    claim.verified_at = now - timedelta(days=days)
    claim.issued_at = now - timedelta(days=days)
    claim.expires_at = now - timedelta(days=days - ownership.CLAIM_TTL.days)
    await claim.save()
    return claim


async def test_a_stale_proof_is_re_proved_and_the_date_moves(beanie_test_db):
    """The window crossing, end to end: verify, age past 30 days, re-verify.

    The last assertion is the one a customer feels — it asks the SAME predicate
    the crawl and the bind ask, so this proves the concierge can ground itself
    again rather than merely that a field changed.
    """
    from pocketpaw_ee.sites import foreign_grounding

    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    await _verify(routes=_well_known(claim.token.encode()))
    stale = await _age_the_proof(days=31)
    assert foreign_grounding.verification_is_fresh(stale) is False, (
        "the fixture must actually be stale, or this test passes for the wrong reason"
    )

    again = await _verify(routes=_well_known(claim.token.encode()))

    assert again.status == "verified"
    assert again.verified_at is not None
    assert again.verified_at > stale.verified_at
    record = await ownership.verified_origin_record(_WS_A, _HOST)
    assert foreign_grounding.verification_is_fresh(record) is True


async def test_re_verifying_a_verified_row_actually_FETCHES(beanie_test_db):
    """A re-stamp without a fetch is not a proof, and it is this fix's own
    failure mode: the cheap way to make the window crossing above pass is to move
    the date on a row nobody re-read. Asserted on the transport, which is the
    only thing that can tell those two apart."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    await _verify(routes=_well_known(claim.token.encode()))
    await _age_the_proof(days=31)

    seen: list[httpx.Request] = []
    await _verify(routes=_well_known(claim.token.encode()), seen=seen)

    assert seen, "the re-verify issued no request, so it re-stamped a proof it never checked"
    assert [r.url.path for r in seen] == [ownership.WELL_KNOWN_PATH]


async def test_a_re_verify_that_finds_NOTHING_leaves_the_old_proof_standing(beanie_test_db):
    """A domain that stopped serving its token is told so — and keeps its row.

    Two properties in one test because they are one decision. The refusal is what
    proves the fetch was real (a blind re-stamp would have returned success), and
    the untouched row is what keeps a failed re-probe from cutting off a live,
    paid binding: the owner gets a sentence about their server, not a dead
    concierge.
    """
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    await _verify(routes=_well_known(claim.token.encode()))
    stale = await _age_the_proof(days=31)

    with pytest.raises(ValidationError) as exc:
        await _verify(routes={})

    assert exc.value.code == "sites.origin_token_missing"
    reloaded = await SiteOriginClaim.find_one(SiteOriginClaim.workspace == _WS_A)
    assert reloaded is not None
    assert reloaded.status == "verified"
    assert reloaded.verified_at == stale.verified_at


async def test_a_wrong_token_on_a_re_verify_does_not_re_stamp_the_date(beanie_test_db):
    """The other failure a re-probe must survive: the domain changed hands and
    the new owner publishes their own token. A mismatch refuses and writes
    nothing, so a proof is never advanced by a file we did not issue."""
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    await _verify(routes=_well_known(claim.token.encode()))
    stale = await _age_the_proof(days=31)

    with pytest.raises(ValidationError) as exc:
        await _verify(routes=_well_known(b"pawverify-somebody-elses-token"))

    assert exc.value.code == "sites.origin_token_mismatch"
    reloaded = await SiteOriginClaim.find_one(SiteOriginClaim.workspace == _WS_A)
    assert reloaded is not None and reloaded.verified_at == stale.verified_at


async def test_an_expired_TOKEN_does_not_block_re_verifying_a_proven_origin(beanie_test_db):
    """CLAIM_TTL bounds an UNREDEEMED invitation, not a published proof.

    The TTL is 7 days and the freshness window is 30, so every row due for
    re-verification has an ``expires_at`` weeks in the past. Applying the TTL to
    a verified row would make the re-verify unreachable — the exact dead end this
    fix exists to remove — while a PENDING row that expired is still refused
    (``test_an_expired_claim_refuses_without_fetching_anything``, above).
    """
    claim = await ownership.claim_origin(workspace_id=_WS_A, user_id="u1", host=_HOST)
    await _verify(routes=_well_known(claim.token.encode()))
    aged = await _age_the_proof(days=31)
    assert aged.expires_at.replace(tzinfo=UTC) < datetime.now(UTC), (
        "the fixture's token must have expired, or this test proves nothing"
    )

    again = await _verify(routes=_well_known(claim.token.encode()))

    assert again.status == "verified"
    assert again.verified_at > aged.verified_at


# --------------------------------------------------------------------------- #
# The HTTP surface is a thin mapping — this pins that it exists and is a write
# --------------------------------------------------------------------------- #


async def test_both_origin_routes_are_registered_as_guarded_writes():
    """Read off the router rather than an app, so it needs no DB and no license.

    It proves the wiring, which is the part of this surface a unit test can
    silently miss: a typo'd path or a route registered as a GET would leave the
    whole feature unreachable with every service test still green. Both are
    POSTs — a claim mints a secret and a verification flips a durable permission,
    so neither is a read.
    """
    from pocketpaw_ee.sites.router import router

    routes = {
        route.path: route
        for route in router.routes
        if getattr(route, "path", "").startswith("/sites/origins")
    }

    assert set(routes) == {"/sites/origins/claims", "/sites/origins/verify"}
    for route in routes.values():
        assert route.methods == {"POST"}
        # The plan gate is router-level; the per-action scope is per-route and is
        # what stops a read-only member from claiming a domain. The guard is a
        # closure over the action name, so the name is read out of its cells —
        # its repr is just a function address.
        actions = {
            cell.cell_contents
            for sub in route.dependant.dependencies
            for cell in (sub.call.__closure__ or ())
            if isinstance(cell.cell_contents, str)
        }
        assert "fabric.write" in actions, route.path
