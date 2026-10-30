# tests/ee/sites/test_build_status_on_the_wire.py — the build lane's state as a CLIENT
# sees it: ``SiteResponse`` (publish + the gallery list) and ``SiteStatusResponse`` (the
# by-pocket status read).
#
# Created 2026-08-10 (SL-3).
#
# WHY THIS FILE EXISTS. SG-9i declared ``build_status`` and ``build_job_id`` on
# ``SiteResponse``, and nothing ever populated them: ``_to_response`` builds the DTO field
# by field, so every response carried the DEFAULTS — ``build_status`` frozen at "none" for
# every site no matter what the row said — and ``build_reason`` was not a field at all.
# Both halves reviewed clean alone. The DTO looked complete because the fields were there;
# the service looked complete because it passed every field it knew about.
#
# The shipped build-status UI reads all three, so the effect was a client polling a value
# that CANNOT CHANGE. That is indistinguishable, from the outside, from a build that never
# starts — which is the one regression this whole sequence exists to prevent.
#
# THE PROPERTY THESE PIN is deliberately not "the field exists". A field can exist and be
# hard-coded. Each test drives a DIFFERENT value onto the row and asserts the wire carries
# THAT value, because the failure mode being guarded is a constant, not an absence.
#
# Mutations in tests/mutations/sl3_build_status_wire.json, run and caught.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import SiteResponse, SiteStatusResponse

#: A settled failure as ``build_job`` writes it: the rung, then the cause, both from
#: closed sets. The shape a client splits on to decide whether to blame the user's code.
FAILED_REASON = "build_failed:install_failed"

#: An infrastructure loss, which settles to the SAME status as the failure above. The pair
#: is what proves the status alone cannot carry the distinction.
INFRA_REASON = "infra_lost:no_sentinel_before_timeout"


def _doc(**overrides: Any) -> Site:
    base: dict[str, Any] = {"workspace": "ws1", "pocket_id": "pk1", "owner": "u1", "name": "S"}
    base.update(overrides)
    return Site(**base)


class TestTheBuildStateReachesSiteResponse:
    """``POST /sites/publish`` and ``GET /sites`` both answer with this DTO."""

    def test_a_queued_build_is_visible_with_its_polling_handle(self) -> None:
        got = sites_service._to_response(
            _doc(build_status="queued", build_job_id="site-build-abc123")
        )
        assert got.build_status == "queued"
        assert got.build_job_id == "site-build-abc123"

    def test_a_building_row_is_visible(self) -> None:
        assert sites_service._to_response(_doc(build_status="building")).build_status == "building"

    def test_a_finished_build_is_visible(self) -> None:
        assert sites_service._to_response(_doc(build_status="built")).build_status == "built"

    def test_a_failure_carries_the_rung_that_explains_it(self) -> None:
        """The reason is the whole point of the field. ``failed`` with nothing else is
        unactionable: it cannot say whether the user's code broke or we lost the
        container, and those need opposite responses."""
        got = sites_service._to_response(_doc(build_status="failed", build_reason=FAILED_REASON))
        assert got.build_status == "failed"
        assert got.build_reason == FAILED_REASON

    def test_the_two_kinds_of_failure_are_distinguishable_on_the_wire(self) -> None:
        """Both settle as ``failed``, so the STATUS is identical and only the rung
        separates them. If the reason did not reach the client, a user would be told their
        site is broken when we lost the sandbox."""
        theirs = sites_service._to_response(_doc(build_status="failed", build_reason=FAILED_REASON))
        ours = sites_service._to_response(_doc(build_status="failed", build_reason=INFRA_REASON))
        assert theirs.build_status == ours.build_status == "failed"
        assert theirs.build_reason != ours.build_reason
        assert theirs.build_reason.split(":")[0] == "build_failed"
        assert ours.build_reason.split(":")[0] == "infra_lost"

    def test_a_site_that_never_built_reads_as_no_build(self) -> None:
        got = sites_service._to_response(_doc())
        assert got.build_status == "none"
        assert got.build_reason is None
        assert got.build_job_id is None

    def test_an_unrecognised_status_is_passed_through_verbatim(self) -> None:
        """The wire's contract is that a CLIENT treats an unknown status as in-progress,
        and that only works if the server does not flatten it first. Normalising here
        would report "nothing is building" about a build running under a status this
        deploy predates — the rolling-deploy hazard ``build_state``'s header describes,
        seen from the read side."""
        got = sites_service._to_response(_doc(build_status="uploading"))
        assert got.build_status == "uploading"

    def test_a_row_predating_the_fields_does_not_break_the_read(self) -> None:
        """Every other field here is read with a ``getattr`` default for this reason: the
        gallery is a hot read and a pre-SG-9i row must not turn it into a 500."""

        class _Ancient:
            id = "64b7f9c2e4b0a1d2c3e4f5a6"
            pocket_id = "pk1"
            name = "old"
            script_name = "s"
            deployed = True
            signed_key = "k"
            url = "https://example.test"

        got = sites_service._to_response(_Ancient())  # type: ignore[arg-type]
        assert got.build_status == "none"
        assert got.build_reason is None
        assert got.build_job_id is None


class TestTheDtoDeclaresTheContract:
    def test_site_response_carries_all_three_fields(self) -> None:
        for field in ("build_status", "build_reason", "build_job_id"):
            assert field in SiteResponse.model_fields, field

    def test_site_status_response_carries_all_three_too(self) -> None:
        """The by-pocket read is the only GET keyed on a pocket id, so a builder watching
        a build it just triggered has nowhere else to poll."""
        for field in ("build_status", "build_reason", "build_job_id"):
            assert field in SiteStatusResponse.model_fields, field

    @pytest.mark.parametrize("model", [SiteResponse, SiteStatusResponse])
    def test_the_defaults_mean_no_build_rather_than_an_error(self, model: Any) -> None:
        assert model.model_fields["build_status"].default == "none"
        assert model.model_fields["build_reason"].default is None
        assert model.model_fields["build_job_id"].default is None

    def test_the_reason_survives_serialisation(self) -> None:
        """A field that exists on the model and is dropped by the dump is not on the
        wire. This is the boundary a client actually reads."""
        dumped = sites_service._to_response(
            _doc(build_status="failed", build_reason=FAILED_REASON)
        ).model_dump()
        assert dumped["build_status"] == "failed"
        assert dumped["build_reason"] == FAILED_REASON


class TestTheBuildStateReachesTheByPocketStatus:
    """``GET /sites/by-pocket/{pocket_id}/status`` — what a build badge polls."""

    async def _status(self, monkeypatch: pytest.MonkeyPatch, doc: Site | None) -> Any:
        async def _canonical(_ws: str, _pk: str) -> Any:
            return doc

        async def _patterns(_ws: str, _ids: list[str]) -> dict[str, str]:
            return {}

        async def _engines(_ws: str, _ids: list[str]) -> dict[str, str]:
            return {}

        from pocketpaw_ee.cloud.pockets import service as pockets_service

        monkeypatch.setattr(sites_service, "_canonical_site_doc", _canonical)
        monkeypatch.setattr(pockets_service, "patterns_for_pockets", _patterns)
        monkeypatch.setattr(pockets_service, "engines_for_pockets", _engines)
        return await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk1")

    async def test_a_build_in_flight_is_visible_by_pocket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        got = await self._status(
            monkeypatch, _doc(build_status="building", build_job_id="site-build-xyz")
        )
        assert got.build_status == "building"
        assert got.build_job_id == "site-build-xyz"

    async def test_a_failure_and_its_rung_are_visible_by_pocket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        got = await self._status(
            monkeypatch, _doc(build_status="failed", build_reason=INFRA_REASON)
        )
        assert got.build_status == "failed"
        assert got.build_reason == INFRA_REASON

    async def test_a_pocket_with_no_site_reads_as_no_build_not_as_null(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A draft that was never published has NO build. That must not look the same as a
        build that failed, and it must not look like an error either."""
        got = await self._status(monkeypatch, None)
        assert got.build_status == "none"
        assert got.build_reason is None
        assert got.build_job_id is None
