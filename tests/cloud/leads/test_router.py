# tests/cloud/leads/test_router.py — exercises capture hardening at the router
# (origin pinning + signed key live here) plus the authed read. Pattern: build
# an app that mounts the leads router; for the public endpoint, no auth needed;
# for the read endpoint, follow the existing cloud router test pattern for
# injecting an authed user + active workspace.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.4): router-level
# tests for the public capture surface — wrong-origin reject, bad-signed-key
# reject, and the happy-path accept.
# Updated 2026-05-30 (security hardening): added C1 oversized-payload→413 (no
# lead written) and H1 constant-time signed-key compare (secrets.compare_digest
# is the mechanism; valid key still 200, bad key still 401) coverage.
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): added coverage for the
# NATIVE-FORM capture sibling POST /capture/form (final URL /api/v1/capture/form)
# — the endpoint imported sites' rewired <form>s post to as urlencoded, with
# hidden paw_site_id / paw_key / paw_page / paw_redirect fields. Valid key →
# recorded through the SAME capture pipeline + 303 back to Origin+paw_redirect;
# bad key → 401; absolute / protocol-relative paw_redirect → 400 (open-redirect
# guard); wrong origin → 403; urlencoded content-type accepted natively.
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.site import Site


@pytest.fixture
def capture_app():
    from fastapi import FastAPI
    from pocketpaw_ee.cloud.leads.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


async def _site(ws="ws1", site_id="site_1") -> Site:
    site = Site(
        workspace=ws,
        pocket_id="pk1",
        owner="u1",
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="key_ok",
        event_mapping={
            "AppointmentRequest": {
                "creates": "AppointmentRequest",
                "fields": {"name": "{{ payload.full_name }}"},
            }
        },
    )
    await site.insert()
    return site


async def _capture_json(capture_app, *, origin, site_id="site_1"):
    headers = {"origin": origin} if origin is not None else {}
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        return await c.post(
            f"/api/v1/sites/{site_id}/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers=headers,
        )


@pytest.mark.asyncio
async def test_capture_accepts_an_unrecognized_origin_and_flags_it(mongo_db, capture_app):
    """THE DEFAULT FLIPPED, deliberately. This used to assert 403.

    The origin pin guards a credential that is ALREADY PUBLIC on three of the four
    engines (``paw_key`` ships as a hidden input in the page source), and ``Origin``
    constrains browsers only — any script forges it. So as a gate it did not stop a
    determined spammer; it 403'd real submissions whenever the allowlist and the
    serving host disagreed (a doc predating deployed-host stamping, an async react
    build inserted with ``url=""``, apex vs ``www.``, a preview URL, a ``file://``
    open). Every one of those failed CLOSED, and on the native-form path the
    customer's own prospect was shown a raw JSON 403.

    So the submission is now ACCEPTED and ATTRIBUTED. The flag is what makes the
    trade honest — the lead is not silently indistinguishable from an expected one.

    Mutation: restore the unconditional ``origin_allowed`` gate in the router and
    this fails."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _site()
    resp = await _capture_json(capture_app, origin="https://evil.example.com")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    leads = await leads_service.list_for_site(site.workspace, "site_1")
    assert len(leads) == 1
    assert leads[0].origin == "https://evil.example.com"
    assert leads[0].origin_unrecognized is True


@pytest.mark.asyncio
async def test_capture_records_a_recognized_origin_without_flagging_it(mongo_db, capture_app):
    """The other half: an expected origin is recorded too, and NOT flagged — so the
    flag distinguishes rather than firing on everything."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _site()
    resp = await _capture_json(capture_app, origin="https://brightsmiledental.com")

    assert resp.status_code == 200, resp.text
    leads = await leads_service.list_for_site(site.workspace, "site_1")
    assert leads[0].origin == "https://brightsmiledental.com"
    assert leads[0].origin_unrecognized is False


@pytest.mark.asyncio
async def test_capture_still_403s_when_the_site_opts_into_enforcement(mongo_db, capture_app):
    """The strict behaviour is not gone, it is OPT-IN. A site that flips
    ``enforce_origin`` gets the old fail-closed gate back verbatim.

    Mutation: drop the ``site.enforce_origin and`` guard's first operand (making the
    gate unconditional) and the two tests above fail; delete the guard entirely and
    this one fails. Both directions are covered."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _site()
    site.enforce_origin = True
    await site.save()

    resp = await _capture_json(capture_app, origin="https://evil.example.com")

    assert resp.status_code == 403
    assert await leads_service.count_for_site(site.workspace, "site_1") == 0


@pytest.mark.asyncio
async def test_an_enforcing_site_still_fails_closed_on_a_missing_origin(mongo_db, capture_app):
    """``origin_allowed`` fails closed on an absent header, and opting in must
    preserve that — otherwise "strict" would be weaker than the old default against
    the one caller that trivially omits the header."""
    site = await _site()
    site.enforce_origin = True
    await site.save()

    resp = await _capture_json(capture_app, origin=None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_capture_accepts_a_submission_with_no_origin_header_by_default(mongo_db, capture_app):
    """A missing Origin is the ``file://`` / stripped-header case. Under the old
    fail-closed default it was a 403; it is now an accepted, unflagged lead — there
    is no origin to find unrecognized."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _site()
    resp = await _capture_json(capture_app, origin=None)

    assert resp.status_code == 200, resp.text
    leads = await leads_service.list_for_site(site.workspace, "site_1")
    assert leads[0].origin == ""
    assert leads[0].origin_unrecognized is False


@pytest.mark.asyncio
async def test_a_sites_own_deployed_host_is_never_unrecognized(mongo_db, capture_app):
    """THE CASE THAT PRODUCED THE ORIGINAL 403 REPORT: a site deployed to
    ``*.workers.dev`` whose ``allowed_origins`` still carried only the localhost
    seed, so its own visitors were foreign to it.

    ``allowed_origins`` is stamped by the publish deploy path, and there are real
    ways to land a Site row that never got the stamp — a draft/preview publish
    returns before it, an async react build inserts with ``url=""`` and fills the
    url in later, and rows predating the stamping keep the seed. Deriving the
    effective set from the site's own ``url`` closes all of them at once.

    Mutation: make ``_effective_origins`` return ``site.allowed_origins`` unchanged
    and this fails."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _site()
    site.allowed_origins = ["localhost", "127.0.0.1"]  # the un-stamped seed
    site.url = "https://bright.workers.dev"
    await site.save()

    resp = await _capture_json(capture_app, origin="https://bright.workers.dev")

    assert resp.status_code == 200, resp.text
    leads = await leads_service.list_for_site(site.workspace, "site_1")
    assert leads[0].origin_unrecognized is False, (
        "a site's own deployed host was recorded as unrecognized"
    )


@pytest.mark.asyncio
async def test_an_attached_custom_domain_is_never_unrecognized(mongo_db, capture_app):
    """Same derivation, other source. ``add_domain`` appends to ``allowed_origins``,
    but a row whose domain was attached by any path that did not is still the site's
    own domain — and its visitors are its own."""
    from pocketpaw_ee.cloud.leads import service as leads_service
    from pocketpaw_ee.cloud.models.site import SiteDomain

    site = await _site()
    site.allowed_origins = ["localhost"]
    site.domains = [SiteDomain(hostname="northdental.example", status="live")]
    await site.save()

    resp = await _capture_json(capture_app, origin="https://northdental.example")

    assert resp.status_code == 200, resp.text
    leads = await leads_service.list_for_site(site.workspace, "site_1")
    assert leads[0].origin_unrecognized is False


@pytest.mark.asyncio
async def test_an_enforcing_site_accepts_its_own_deployed_host(mongo_db, capture_app):
    """The derivation has to reach the GATE too, not just the flag. A site that opts
    into enforcement while carrying an un-stamped allowlist would otherwise 403 its
    own pages — the strict mode would be unusable exactly where it is wanted."""
    site = await _site()
    site.allowed_origins = ["localhost"]
    site.url = "https://bright.workers.dev"
    site.enforce_origin = True
    await site.save()

    resp = await _capture_json(capture_app, origin="https://bright.workers.dev")

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_derivation_does_not_admit_an_unrelated_host(mongo_db, capture_app):
    """The widening is bounded: only hosts WE wrote (the deploy url, attached
    domains) join the set, so a third-party origin is still unrecognized."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _site()
    site.url = "https://bright.workers.dev"
    await site.save()

    resp = await _capture_json(capture_app, origin="https://evil.example.com")

    assert resp.status_code == 200, resp.text
    leads = await leads_service.list_for_site(site.workspace, "site_1")
    assert leads[0].origin_unrecognized is True


@pytest.mark.asyncio
async def test_capture_rejects_bad_signed_key(mongo_db, capture_app):
    await _site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "WRONG",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_capture_accepts_valid_submission(mongo_db, capture_app):
    await _site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["lead_id"]


@pytest.mark.asyncio
async def test_capture_rejects_oversized_payload(mongo_db, capture_app):
    """C1: an oversized body (> MAX_PAYLOAD_BYTES) is rejected with 413 and no
    lead is written — the size cap is enforced before the service is called."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    from pocketpaw.sites_capture.models import MAX_PAYLOAD_BYTES

    site = await _site()
    # A single field whose value alone blows past the 8KB cap.
    big_value = "x" * (MAX_PAYLOAD_BYTES + 1024)
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": big_value},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 413
    # The oversized submission never reached the persist path.
    assert await leads_service.count_for_site(site.workspace, "site_1") == 0


@pytest.mark.asyncio
async def test_capture_uses_constant_time_key_compare(mongo_db, capture_app, monkeypatch):
    """H1: the signed-key check goes through secrets.compare_digest (a
    constant-time comparison), not a plain ``!=``. We spy on compare_digest and
    assert the valid-key path both invokes it and still returns 200."""
    import secrets as _secrets

    import pocketpaw_ee.cloud.leads.router as router_mod

    await _site()
    calls: list[tuple[str, str]] = []
    real = _secrets.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(router_mod.secrets, "compare_digest", _spy)

    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 200
    assert calls, "signed-key check must route through secrets.compare_digest"
    assert ("key_ok", "key_ok") in calls


# --------------------------------------------------------------------------- #
# SI-4 — native-form capture: POST /capture/form (final URL /api/v1/capture/form).
# Imported sites rewire their <form>s to a plain urlencoded POST here, with hidden
# paw_site_id / paw_key / paw_page / paw_redirect fields. Same hardening ladder as
# the JSON path, then a 303 back to Origin + the RELATIVE paw_redirect.
# --------------------------------------------------------------------------- #


async def _form_site(ws="ws1", site_id="site_form") -> Site:
    """A site seeded the way create/publish seeds it: a 'lead' event mapping (the
    default form_type the rewired form posts under)."""
    site = Site(
        workspace=ws,
        pocket_id="pk_form",
        owner="u1",
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="key_ok",
        event_mapping={
            "lead": {
                "creates": "Lead",
                "fields": {"full_name": "{{ payload.full_name }}", "email": "{{ payload.email }}"},
            }
        },
    )
    await site.insert()
    return site


def _form_fields(**overrides) -> dict:
    fields = {
        "full_name": "Sam Smiles",
        "email": "sam@example.com",
        "paw_site_id": "site_form",
        "paw_key": "key_ok",
        "paw_page": "index.html",
        "paw_redirect": "/thanks.html",
    }
    fields.update(overrides)
    return fields


@pytest.mark.asyncio
async def test_capture_form_valid_key_records_and_303s(mongo_db, capture_app):
    """A valid urlencoded native-form POST records the lead through the SAME
    capture pipeline (mapping applied, paw_* control fields stripped) and 303s to
    the validated Origin + the relative paw_redirect."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        # httpx ``data=`` sends application/x-www-form-urlencoded — the exact
        # content type a native <form method=post> submits.
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "https://brightsmiledental.com/thanks.html"

    leads = await leads_service.list_for_site(site.workspace, "site_form")
    assert len(leads) == 1
    assert leads[0].properties["full_name"] == "Sam Smiles"
    assert leads[0].properties["email"] == "sam@example.com"
    # The paw_* control fields never become lead properties.
    assert not any(k.startswith("paw_") for k in leads[0].properties)


@pytest.mark.asyncio
async def test_capture_form_invalid_key_is_401(mongo_db, capture_app):
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(paw_key="WRONG"),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 401
    assert await leads_service.count_for_site(site.workspace, "site_form") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_redirect",
    ["https://evil.example.com/phish", "//evil.example.com", "/\\evil", "javascript://alert(1)"],
)
async def test_capture_form_non_relative_redirect_is_400(mongo_db, capture_app, bad_redirect):
    """Open-redirect guard: an absolute / protocol-relative / backslash / scheme'd
    paw_redirect is rejected BEFORE anything is recorded."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(paw_redirect=bad_redirect),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 400, resp.text
    assert await leads_service.count_for_site(site.workspace, "site_form") == 0


@pytest.mark.asyncio
async def test_capture_form_accepts_an_unrecognized_origin(mongo_db, capture_app):
    """The native-form half of the flipped default, and the one that motivated it:
    here the 403 was rendered TO THE VISITOR as raw JSON. A prospect filling in a
    contact form saw ``{"detail":"Origin not allowed for this site"}`` instead of a
    thank-you page, and the site owner saw no lead and no error."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://evil.example.com"},
        )

    assert resp.status_code == 303
    assert await leads_service.count_for_site(site.workspace, "site_form") == 1


@pytest.mark.asyncio
async def test_capture_form_redirects_to_the_site_not_the_claimed_origin(mongo_db, capture_app):
    """THE OPEN REDIRECT THE FLIPPED DEFAULT WOULD OTHERWISE HAVE OPENED.

    The 303 Location used to be the request Origin verbatim, which was safe ONLY
    because the origin had just been pinned. With the pin opt-in, echoing it back
    would let anyone POST with ``Origin: https://evil.example.com`` and have us send
    the browser there. ``_redirect_base`` therefore falls back to the site's OWN url
    whenever the origin is not allowlisted.

    Mutation: return ``origin`` unconditionally from ``_redirect_base`` and this
    fails."""
    site = await _form_site()
    site.url = "https://bright.workers.dev"
    await site.save()

    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://evil.example.com"},
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == "https://bright.workers.dev/thanks.html", location
    assert "evil.example.com" not in location


@pytest.mark.asyncio
async def test_capture_form_keeps_the_visitor_on_a_recognized_origin(mongo_db, capture_app):
    """An allowlisted origin IS used as the base, so a visitor on the custom domain
    stays there instead of being bounced to the workers.dev url after submitting."""
    site = await _form_site()
    site.url = "https://bright.workers.dev"
    await site.save()

    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://brightsmiledental.com"},
        )

    assert resp.headers["location"] == "https://brightsmiledental.com/thanks.html"


@pytest.mark.asyncio
async def test_capture_form_403s_for_an_enforcing_site(mongo_db, capture_app):
    """Opt-in strictness works on this path too."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    site.enforce_origin = True
    await site.save()

    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://evil.example.com"},
        )

    assert resp.status_code == 403
    assert await leads_service.count_for_site(site.workspace, "site_form") == 0


@pytest.mark.asyncio
async def test_capture_form_unknown_site_is_404(mongo_db, capture_app):
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(paw_site_id="site_missing"),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 404
