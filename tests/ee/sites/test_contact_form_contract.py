# tests/ee/sites/test_contact_form_contract.py — the generated contact form and
# the seeded event mapping are two halves of one contract, and nothing was
# holding them together.
#
# Created 2026-08-13. THE BUG THIS REPRODUCES: ``landing_assembler`` emits the
# lead form's first input as ``name="name"`` (landing_assembler.py, ``_lead_form_card``)
# while the event mapping every Site doc is seeded with reads ``{{ payload.full_name }}``
# (sites/service.py, ``_DEFAULT_EVENT_MAPPING``). Nothing aliases the two. So on
# every site built through the deterministic landing path, the visitor types their
# name, the browser POSTs ``name=...``, the interpolator looks up ``payload.full_name``,
# finds nothing, and stores ``full_name: None``. The lead lands, the bell rings, the
# owner opens the Leads view — and the one field they most need is empty. The typed
# value is not recoverable: ``interpolate_mapping`` projects ONLY the mapping's keys,
# so the submitted ``name`` never reaches storage at all.
#
# It is invisible to every existing test because they all hand-build BOTH sides.
# tests/cloud/leads/test_service.py fixes its own mapping (``{"name": "{{ payload.full_name }}"}``)
# and then submits ``{"full_name": "Sam"}`` — a payload matched to that fixture, not
# the one the real form sends. That test passes and proves the interpolator works.
# It cannot fail on this bug, because the form is not in it.
#
# So these tests deliberately import BOTH real artifacts and assert against neither's
# hand-written copy: the field names come out of ``assemble_landing_spec`` and the
# mapping comes out of ``_DEFAULT_EVENT_MAPPING``. A test that restated either side
# would be testing the restatement.
#
# What is pinned here:
#   * the contract — every input the assembler emits is reachable by the seeded
#     mapping, so a form field can never again be generated with nowhere to land;
#   * the end-to-end reproduction — a submission whose keys are the GENERATED input
#     names, run through the real capture pipeline with the real seeded mapping,
#     arrives with the visitor's values intact;
#   * the already-published fleet — sites deployed before the fix still POST the old
#     field names, and their leads must land too (nobody republishes to get a name).
from __future__ import annotations

import re
from typing import Any

import pytest
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec
from pocketpaw_ee.sites.service import _DEFAULT_EVENT_MAPPING, _DEFAULT_FORM_TYPE

# A published Site's ``script_name`` is the deploy script name — a 24-char hex
# ObjectId. Same convention as tests/cloud/leads/test_service.py.
SITE_ID = "6d4a1f2b3c8e9a0f1b2c3d4e"

# The copy a create-landing-site call hands the assembler. Deliberately minimal:
# the form is fixed structure, not copy, so it is emitted whatever the content says.
CONTENT: dict[str, Any] = {
    "brand": "Bright Smile Dental",
    "hero": {"title": "Dentistry that doesn't hurt"},
}

_PLACEHOLDER = re.compile(r"\{\{\s*payload\.([a-zA-Z0-9_]+)\s*\}\}")


def _generated_field_names(spec: dict[str, Any]) -> list[str]:
    """Every ``name`` the assembled spec puts on a submitting input.

    Walks the real node tree rather than reaching into ``_lead_form_card``, so the
    test still sees the truth if the form moves or grows a field."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in {"input", "textarea", "select"}:
                field = (node.get("props") or {}).get("name")
                if field:
                    found.append(str(field))
            for child in node.get("children") or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec.get("ui"))
    return found


def _mapped_payload_keys() -> set[str]:
    """The ``payload.X`` keys the seeded default mapping actually reads."""
    fields = _DEFAULT_EVENT_MAPPING[_DEFAULT_FORM_TYPE]["fields"]
    return {m.group(1) for template in fields.values() for m in _PLACEHOLDER.finditer(template)}


async def _seeded_site(**over: Any) -> Site:
    """A Site carrying the REAL seeded mapping — the doc publish() actually writes."""
    site = Site(
        workspace="ws1",
        pocket_id="pk1",
        owner="u1",
        name="Bright Smile Dental",
        script_name=SITE_ID,
        allowed_origins=["brightsmiledental.com"],
        signed_key="pp_tok_x",
        event_mapping=_DEFAULT_EVENT_MAPPING,
        **over,
    )
    await site.insert()
    return site


# --------------------------------------------------------------------------- #
# THE CONTRACT
# --------------------------------------------------------------------------- #


def test_every_generated_form_field_can_reach_the_seeded_mapping():
    """THE BUG, at the seam. The assembler and the mapping are edited in different
    files by different tasks, and nothing has ever compared them.

    Mutation: rename any input in ``_lead_form_card`` without touching
    ``_DEFAULT_EVENT_MAPPING`` and this fails — which is exactly what happened."""
    generated = _generated_field_names(assemble_landing_spec(CONTENT))
    assert generated, "the assembler emitted no named inputs at all"

    unreachable = [f for f in generated if f not in _mapped_payload_keys()]
    assert not unreachable, (
        f"the generated form POSTs {unreachable} and the seeded mapping reads "
        f"{sorted(_mapped_payload_keys())} — those submissions are silently discarded"
    )


@pytest.mark.asyncio
async def test_a_submission_from_the_generated_form_keeps_the_visitors_values(beanie_test_db):
    """THE BUG, end to end. The payload keys here are not chosen — they are read off
    the assembled spec, so this is the exact body the visitor's browser sends.

    Before the fix the stored properties came back with ``full_name`` empty and the
    typed name nowhere in the document."""
    site = await _seeded_site()
    typed = {
        "name": "Sam Rivera",
        "full_name": "Sam Rivera",
        "email": "sam@example.com",
        "phone": "555-0134",
        "message": "Do you take walk-ins on Saturday?",
    }
    # Submit ONLY the fields the real form actually renders.
    payload = {f: typed[f] for f in _generated_field_names(assemble_landing_spec(CONTENT))}

    lead = await leads_service.capture(
        site=site,
        form_type=_DEFAULT_FORM_TYPE,
        payload=payload,
        submitter_ref="anon",
        rate_key="ip_hash_1",
    )

    assert lead is not None, "a well-formed contact submission was dropped"
    # The visitor's name is the field an owner needs most; it must survive.
    assert "Sam Rivera" in lead.properties.values(), (
        f"the submitted name never reached storage — properties={lead.properties}"
    )
    assert lead.properties.get("email") == "sam@example.com"
    assert lead.properties.get("message") == "Do you take walk-ins on Saturday?"


@pytest.mark.asyncio
async def test_a_site_published_before_the_fix_still_lands_its_leads(beanie_test_db):
    """The fleet is already deployed. Those Workers POST ``name=...`` and will keep
    doing so until someone republishes them — which nobody will, because the site
    looks fine. Whatever the fix is, it has to reach back to them, so the old field
    name must still resolve."""
    site = await _seeded_site()

    lead = await leads_service.capture(
        site=site,
        form_type=_DEFAULT_FORM_TYPE,
        payload={"name": "Dana Okoye", "email": "dana@example.com"},
        submitter_ref="anon",
        rate_key="ip_hash_2",
    )

    assert lead is not None
    assert "Dana Okoye" in lead.properties.values(), (
        f"a live site's lead still loses its name — properties={lead.properties}"
    )
