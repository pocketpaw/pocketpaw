# tests/ee/sites/test_site_provision_status.py — pins the DP0-1 provisioning-state
# field on the Site document.
# Created: 2026-07-08 (feat/dp0-create-database, DP0-1) — a lightweight
# pydantic-level round-trip: ``provision_status`` defaults to "none" (so static
# sites and pre-DP0 rows read "not provisioning"), and a set value persists through
# a model_dump/re-parse cycle. Documents the value set none | provisioning |
# provisioned | failed. No DB is needed — instantiating the Beanie document is a
# pure pydantic construction here.
from __future__ import annotations

from pocketpaw_ee.cloud.models.site import Site


def _site(**overrides) -> Site:
    base = dict(workspace="ws1", pocket_id="pk1", owner="u1")
    base.update(overrides)
    return Site(**base)


def test_provision_status_defaults_to_none() -> None:
    """A fresh Site (static or pre-DP0) reads provision_status == "none"."""
    assert _site().provision_status == "none"


def test_provision_status_persists_a_set_value() -> None:
    """A provisioning-state value round-trips through model_dump/re-parse."""
    site = _site(provision_status="provisioning", d1_database_id="d1_uuid_xyz")
    assert site.provision_status == "provisioning"
    # The DP0-1 contract pairing: the D1 id is set the instant the D1 is created,
    # while status is still "provisioning" (so a retry reuses the same D1).
    assert site.d1_database_id == "d1_uuid_xyz"
    dumped = site.model_dump()
    assert dumped["provision_status"] == "provisioning"
    reparsed = Site(
        **{k: dumped[k] for k in ("workspace", "pocket_id", "owner")},
        provision_status=dumped["provision_status"],
    )
    assert reparsed.provision_status == "provisioning"


def test_provision_status_accepts_documented_states() -> None:
    """The documented value set is none | provisioning | provisioned | failed."""
    for state in ("none", "provisioning", "provisioned", "failed"):
        assert _site(provision_status=state).provision_status == state
