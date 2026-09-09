# tests/ee/sites/test_site_metadata.py — editable site title and description (SM-1).
#
# Two things here are load-bearing beyond ordinary CRUD.
#
# The write is field-scoped (``set``) rather than a whole-document ``save``, because
# this is a human-paced edit against a document a BUILD also writes to. Under
# ``save()`` an owner renaming a site while a publish settles would push their stale
# snapshot back and silently roll ``build_status`` — and ``delete_status`` — backwards.
#
# And a blank NAME is refused rather than trimmed to empty, because the name is both
# the site's identity in the gallery and the exact string the delete confirmation
# asks the owner to type back. A site named "" would make that gate unsatisfiable.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites.dto import SiteMetadataUpdate


def test_a_name_of_only_whitespace_is_refused_not_trimmed_to_empty() -> None:
    """A site named "" cannot be typed back into the delete confirmation."""
    with pytest.raises(ValueError):
        SiteMetadataUpdate(name="   ")
    with pytest.raises(ValueError):
        SiteMetadataUpdate(name="")


def test_an_omitted_name_is_allowed_because_omission_means_leave_alone() -> None:
    """The blank guard must not become a requirement to resend the name on every
    description edit."""
    body = SiteMetadataUpdate(description="A dentist in Leeds.")
    assert "name" not in body.model_fields_set
    assert "description" in body.model_fields_set


def test_an_explicitly_empty_description_is_allowed_because_that_is_how_you_clear_it() -> None:
    body = SiteMetadataUpdate(description="")
    assert "description" in body.model_fields_set
    assert body.description == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "n" * 201), ("description", "d" * 501)],
)
def test_over_long_values_are_refused_at_the_edge(field, value) -> None:
    """A 422 the form can show beats a record that silently lost its tail on the way
    to Mongo."""
    with pytest.raises(ValueError):
        SiteMetadataUpdate(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "n" * 200), ("description", "d" * 500)],
)
def test_the_caps_are_inclusive(field, value) -> None:
    body = SiteMetadataUpdate(**{field: value})
    assert getattr(body, field) == value


def test_three_way_semantics_distinguish_absent_from_empty() -> None:
    """``model_fields_set`` is the only thing that tells "leave alone" from "clear",
    so the service must read the un-dumped model."""
    only_name = SiteMetadataUpdate(name="Bright Smile")
    assert only_name.model_fields_set == {"name"}
    # description is None here, but that is ABSENCE, not a request to clear it.
    assert only_name.description is None

    clearing = SiteMetadataUpdate(description="")
    assert clearing.model_fields_set == {"description"}


def test_the_response_carries_description_rather_than_only_declaring_it() -> None:
    """SG-9i declared build_status / build_job_id on SiteResponse and nothing ever
    passed them, so every site read the default forever. ``_to_response`` builds the
    DTO field by field, which is exactly how that survived review — so this asserts
    the field is POPULATED from the document, not merely present on the model."""
    import inspect

    from pocketpaw_ee.sites import service

    src = inspect.getsource(service._to_response)
    assert "description=" in src, "_to_response must pass description, not default it"


def test_the_service_writes_field_scoped_not_whole_document() -> None:
    """A ``save()`` here would push a stale snapshot over a concurrent build's status
    fields. Asserted on the source because the alternative is a live Mongo round trip
    with a racing writer, which is not something a unit test can stage honestly."""
    import inspect

    from pocketpaw_ee.sites import service

    src = inspect.getsource(service.update_site_metadata)
    assert "site.set(" in src
    assert "site.save(" not in src
