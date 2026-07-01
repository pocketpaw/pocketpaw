# tests/ee/sites/test_publish_generator_failure.py
# Created: 2026-06-25 (feat/paw-sites-prod-deploy, DEP-3) — reproduce-first coverage
# for the "publish path crashes with an unhandled 500 when the generator toolchain
# is missing" bug.
#
# In the deployed enterprise image the publish path shells out to the paw-sites
# generator (paw-sites-gen) + bun at build time. Before DEP-1/DEP-2 the image
# shipped none of that toolchain, so generator.build() raised a bare
# FileNotFoundError (no such binary on PATH) / RuntimeError (generator non-zero
# exit) that escaped publish() as an UNHANDLED 500 — the cloud error handler only
# maps CloudError subclasses, so anything else surfaces as an opaque 500 with no
# machine-readable code.
#
# DEP-3 wraps the unconditional generator.build() call in the publish path so a
# generator / install / smoke failure maps to a clean CloudError (Internal →
# 500 with code "sites.generator_failed") instead. These tests assert that
# mapping: point PAW_SITES_GEN_CMD at a non-existent binary (the real prod
# failure) — or inject a runner that raises — and require a CloudError, NOT a raw
# FileNotFoundError / RuntimeError / SmokeGateFailed.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")


def _ripple_spec() -> dict:
    return {"version": 1, "state": {}, "ui": {"type": "container"}}


class TestPublishGeneratorFailureMapsToCloudError:
    @pytest.mark.asyncio
    async def test_missing_generator_binary_is_cloud_error_not_500(
        self, beanie_test_db, monkeypatch
    ) -> None:
        """The exact prod failure: the generator binary is not on PATH. With the
        REAL subprocess runner, generator.build() raises FileNotFoundError when it
        tries to exec a non-existent ``paw-sites-gen``. The publish path must map
        that to a CloudError (a clean 5xx envelope), NOT let it escape as an
        unhandled 500."""
        from bson import ObjectId
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.sites import service as sites_service

        # Force LOCAL deploy mode (no Cloudflare creds) and point the generator at a
        # binary that does not exist, so the REAL subprocess runner's exec fails with
        # FileNotFoundError inside generator.build().
        monkeypatch.setenv("PAW_SITES_LOCAL", "1")
        monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
        monkeypatch.setenv("PAW_SITES_GEN_CMD", "/nonexistent/paw-sites-gen-does-not-exist-xyz")
        # Keep the per-pocket build dir off the real home.
        monkeypatch.setenv("PAW_SITES_BUILD_DIR", "/tmp/paw-sites-test-build")

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())
        pocket_id = str(ObjectId())

        with pytest.raises(CloudError) as ei:
            await sites_service.publish(
                workspace_id=workspace_id,
                user_id=user_id,
                pocket_id=pocket_id,
                ripple_spec=_ripple_spec(),
                theme={},
                name="Acme",
            )
        # A real cloud envelope: a 5xx with a machine-readable code, never an
        # unhandled 500 (which would NOT be a CloudError at all).
        assert 500 <= ei.value.status_code < 600
        assert ei.value.code == "sites.generator_failed"

    @pytest.mark.asyncio
    async def test_runtime_error_from_generator_is_cloud_error(
        self, beanie_test_db, monkeypatch
    ) -> None:
        """A generator that exits non-zero raises RuntimeError("generator failed:
        ...") inside build(). The publish path must surface that as a CloudError,
        not an unhandled 500. Injected via a fake generator so no subprocess runs."""
        from bson import ObjectId
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.sites import service as sites_service

        monkeypatch.setenv("PAW_SITES_LOCAL", "1")
        monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

        class _BoomGenerator:
            async def build(self, **_kwargs):  # noqa: ANN003
                raise RuntimeError("generator failed: boom from tsc")

        with pytest.raises(CloudError) as ei:
            await sites_service.publish(
                workspace_id=str(ObjectId()),
                user_id=str(ObjectId()),
                pocket_id=str(ObjectId()),
                ripple_spec=_ripple_spec(),
                theme={},
                name="Acme",
                _generator=_BoomGenerator(),
            )
        assert 500 <= ei.value.status_code < 600
        assert ei.value.code == "sites.generator_failed"

    @pytest.mark.asyncio
    async def test_smoke_gate_failure_is_cloud_error(self, beanie_test_db, monkeypatch) -> None:
        """A SmokeGateFailed (the workerd SSR fail-gate, or a bun install/build
        failure) is also an infra failure on the publish path — it must map to a
        CloudError, not escape unhandled. Injected via a fake generator."""
        from bson import ObjectId
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.sites import service as sites_service
        from pocketpaw_ee.sites.generator_client import SmokeGateFailed

        monkeypatch.setenv("PAW_SITES_LOCAL", "1")
        monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

        class _SmokeFailGenerator:
            async def build(self, **_kwargs):  # noqa: ANN003
                raise SmokeGateFailed("bun install failed (exit 1): ...")

        with pytest.raises(CloudError) as ei:
            await sites_service.publish(
                workspace_id=str(ObjectId()),
                user_id=str(ObjectId()),
                pocket_id=str(ObjectId()),
                ripple_spec=_ripple_spec(),
                theme={},
                name="Acme",
                _generator=_SmokeFailGenerator(),
            )
        assert 500 <= ei.value.status_code < 600
        assert ei.value.code == "sites.generator_failed"

    @pytest.mark.asyncio
    async def test_preview_build_failure_is_cloud_error(self, beanie_test_db, monkeypatch) -> None:
        """The PREVIEW branch of publish() also builds (smoke=False) before serving
        locally. A toolchain failure there must likewise map to a CloudError so the
        edit/arm preview path never 500s opaquely either."""
        from bson import ObjectId
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.sites import service as sites_service

        monkeypatch.setenv("PAW_SITES_LOCAL", "1")
        monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

        class _BoomGenerator:
            async def build(self, **_kwargs):  # noqa: ANN003
                raise FileNotFoundError(2, "No such file or directory", "paw-sites-gen")

        with pytest.raises(CloudError) as ei:
            await sites_service.publish(
                workspace_id=str(ObjectId()),
                user_id=str(ObjectId()),
                pocket_id=str(ObjectId()),
                ripple_spec=_ripple_spec(),
                theme={},
                name="Acme",
                preview=True,
                _generator=_BoomGenerator(),
            )
        assert 500 <= ei.value.status_code < 600
        assert ei.value.code == "sites.generator_failed"
