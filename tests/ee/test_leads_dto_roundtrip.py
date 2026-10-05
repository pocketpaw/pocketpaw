# tests/ee/test_leads_dto_roundtrip.py — every Lead field survives lead_to_dto.
#
# lead_to_dto builds LeadOut field by field, so a field added to Lead and LeadOut
# but not to the mapper is silently dropped: right in Mongo, invisible in the UI
# (conversation_ref shipped that way on 2026-08-08). Values are generated from
# dataclasses.fields(Lead) so a new field is covered without editing this file;
# a field that must stay internal goes in _NOT_EXPOSED. Lives under tests/ee/
# (not tests/cloud/) because pyproject addopts ignores tests/cloud in CI.

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.leads.domain import Lead
from pocketpaw_ee.cloud.leads.dto import LeadOut, lead_to_dto

# Lead fields LeadOut deliberately does not carry.
_NOT_EXPOSED = {"workspace_id", "submitter_ref"}


def _sample(name: str, type_hint: str, index: int) -> Any:
    """A per-field value that cannot collide with any default."""
    if type_hint == "str":
        return f"{name}-sample"
    if type_hint == "bool":
        return True  # every bool field defaults to False
    if type_hint == "dict[str, Any]":
        return {name: index}
    if type_hint == "datetime | None":
        return datetime(2030, 1, 1, 0, index, tzinfo=UTC)
    pytest.fail(f"add a sample value for Lead.{name}: {type_hint}")


def test_every_lead_field_reaches_lead_out() -> None:
    fields = dataclasses.fields(Lead)
    lead = Lead(**{f.name: _sample(f.name, str(f.type), i) for i, f in enumerate(fields)})

    out = lead_to_dto(lead)

    for f in fields:
        if f.name in _NOT_EXPOSED:
            continue
        assert f.name in LeadOut.model_fields, (
            f"Lead.{f.name} is not in LeadOut; expose it or add it to _NOT_EXPOSED"
        )
        expected = getattr(lead, f.name)
        if isinstance(expected, datetime):
            expected = iso_utc(expected)
        assert getattr(out, f.name) == expected, (
            f"lead_to_dto dropped Lead.{f.name}: add `{f.name}=lead.{f.name}` to the mapper"
        )
