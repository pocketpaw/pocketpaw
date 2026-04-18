# tests/ee/test_fleet_router.py — FastAPI TestClient coverage for the
# fleet REST router shipped in feat/fleet-rest-router.
# Created: 2026-04-16 — Asserts the router's contract with the
# paw-enterprise InstallFleetPanel: list bundled templates, install by
# name, emit journal events when opted in, 404 on unknown template,
# 422 on a malformed body.
#
# Updated: 2026-04-16 (feat/ee-journal-dep) — swapped the
# ``_open_default_journal`` patch for FastAPI's ``dependency_overrides``
# so tests exercise the same ``get_journal`` seam production uses.
# The override points at a ``tmp_path`` SQLite file so tests never
# touch the real ``~/.soul/`` dir. ``journal=false`` is now verified by
# inspecting the ``install_fleet`` call signature instead of asserting
# the dep was never called (it's always resolved; the router decides
# whether to forward it).
#
# Mocks the soul-protocol + connector + pocket factories out at the
# ee.fleet.router seam so these tests stay hermetic — no filesystem
# journal writes to the real data dir, no mongo, no soul-protocol runtime.

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from soul_protocol.engine.journal import open_journal

from ee.fleet import FleetTemplate
from ee.fleet.router import router
from ee.journal_dep import get_journal, reset_journal_cache

# ---------------------------------------------------------------------------
# Fixtures — app, client, and a fake fleet factory stack so we never boot
# a real soul runtime inside the test suite.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_journal_cache():
    """Drop any Journal cached by a previous test before/after each run.

    The dep's ``lru_cache`` is module-global, so without a reset an open
    handle from an earlier test could leak into the next one and mask
    override bugs.
    """

    reset_journal_cache()
    yield
    reset_journal_cache()


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    """A disposable SQLite path the tests' ``get_journal`` override
    points at. Shared between the override factory and the read helper.
    """

    return tmp_path / "router_journal.db"


@pytest.fixture
def app(journal_path: Path) -> FastAPI:
    """FastAPI app with the fleet router mounted + ``get_journal``
    overridden to write into a tmp-path journal.

    Using ``dependency_overrides`` is the canonical FastAPI pattern for
    swapping collaborators in tests — it exercises the real Depends
    wiring instead of monkey-patching an internal helper.
    """

    a = FastAPI()
    a.include_router(router)
    a.dependency_overrides[get_journal] = lambda: open_journal(journal_path)
    return a


@pytest.fixture
def fake_soul_factory():
    """Return a ``SoulFactory``-shaped double.

    The installer duck-types on ``load_bundled(name)`` + ``from_template``
    so we only need those two methods. The soul object itself needs a
    ``did`` and ``name`` — the installer's ``_agent_spawned_payload``
    reads them into the journal event.
    """

    factory = MagicMock()
    template = MagicMock()
    template.name = "Arrow"
    factory.load_bundled = MagicMock(return_value=template)

    soul = MagicMock()
    soul.did = "did:soul:fake-sales-fleet"
    soul.name = "Arrow"
    factory.from_template = AsyncMock(return_value=soul)
    return factory


@pytest.fixture
def patch_install_fleet(fake_soul_factory):
    """Replace ``ee.fleet.router.install_fleet`` with a version that
    always hands the fake soul factory to the real installer.

    This keeps journal wiring + report shape real (the tests assert
    on them) without requiring a real SoulFactory on the import path.
    """

    from ee.fleet import install_fleet as real_install

    async def _wrapped(fleet, **kwargs):
        kwargs.setdefault("soul_factory", fake_soul_factory)
        return await real_install(fleet, **kwargs)

    with patch("ee.fleet.router.install_fleet", side_effect=_wrapped) as mock:
        yield mock


@pytest.fixture
def read_journal(journal_path: Path):
    """Expose a helper that re-opens the journal for read assertions
    after the install request has closed its own writer handle.
    """

    def _read() -> list:
        reader = open_journal(journal_path)
        try:
            return reader.query(limit=100)
        finally:
            reader.close()

    return _read


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /fleet/templates
# ---------------------------------------------------------------------------


class TestGetTemplates:
    def test_returns_bundled_templates_envelope(self, client: TestClient) -> None:
        """The list endpoint returns the canonical envelope shape with at
        least one bundled template — currently ``sales-fleet`` ships with
        the package; any additions should keep the count >= 1.
        """

        resp = client.get("/fleet/templates")
        assert resp.status_code == 200
        body = resp.json()
        assert "templates" in body
        assert "total" in body
        assert body["total"] == len(body["templates"])
        assert body["total"] >= 1

    def test_templates_have_full_shape(self, client: TestClient) -> None:
        """Every entry validates as a FleetTemplate so the UI can render
        description + connectors + widgets without a second round-trip.
        """

        resp = client.get("/fleet/templates")
        assert resp.status_code == 200
        for entry in resp.json()["templates"]:
            parsed = FleetTemplate.model_validate(entry)
            assert parsed.name
            assert parsed.soul_template
            assert parsed.pocket_name

    def test_sales_fleet_is_present(self, client: TestClient) -> None:
        """``sales-fleet`` is the canonical reference fleet — its presence
        is a regression guard if the bundled directory moves.
        """

        resp = client.get("/fleet/templates")
        names = [t["name"] for t in resp.json()["templates"]]
        assert "sales-fleet" in names

    def test_bad_template_is_skipped(self, client: TestClient, monkeypatch) -> None:
        """A single bad template can't take down the list endpoint — it
        is logged and skipped while the rest still render.
        """

        def _explode(name: str) -> FleetTemplate:
            if name == "broken":
                raise ValueError("simulated parse error")
            return FleetTemplate(
                name=name,
                soul_template="arrow",
                pocket_name="Pipeline",
            )

        monkeypatch.setattr(
            "ee.fleet.router.list_bundled_fleets",
            lambda: ["broken", "ok"],
        )
        monkeypatch.setattr("ee.fleet.router.load_fleet", _explode)

        resp = client.get("/fleet/templates")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["templates"][0]["name"] == "ok"


# ---------------------------------------------------------------------------
# POST /fleet/install — happy path, journal opt-in, 404, 422.
# ---------------------------------------------------------------------------


class TestInstallFleet:
    def test_installs_known_template_and_returns_report(
        self,
        client: TestClient,
        patch_install_fleet,
    ) -> None:
        """``sales-fleet`` installs end-to-end against fake factories and
        the router returns the serialized ``FleetInstallReport``. The
        report's ``soul_id`` tracks the fake soul from the factory.
        """

        resp = client.post(
            "/fleet/install",
            json={"template_name": "sales-fleet", "journal": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fleet"] == "sales-fleet"
        assert body["soul_id"] == "did:soul:fake-sales-fleet"
        assert isinstance(body["steps"], list)
        assert body["steps"], "install report should record at least one step"

    def test_install_with_journal_emits_correlated_events(
        self,
        client: TestClient,
        patch_install_fleet,
        read_journal,
    ) -> None:
        """``journal=true`` hands the shared org Journal into the
        installer and yields the canonical ``fleet.install.started`` /
        ``agent.spawned`` / ``fleet.installed`` trio sharing one
        correlation id. Under the dependency override the journal lives
        at ``tmp_path``, so we can re-open it for read assertions.
        """

        resp = client.post(
            "/fleet/install",
            json={"template_name": "sales-fleet", "journal": True},
        )
        assert resp.status_code == 200

        events = read_journal()
        actions = [e.action for e in events]
        assert actions == [
            "fleet.install.started",
            "agent.spawned",
            "fleet.installed",
        ]
        corr_ids = {e.correlation_id for e in events}
        assert len(corr_ids) == 1

    def test_install_without_journal_forwards_none(
        self,
        client: TestClient,
        patch_install_fleet,
    ) -> None:
        """``journal=false`` must forward ``None`` into ``install_fleet``.
        The dep itself is still resolved (FastAPI has no graceful way to
        skip it), but the router is responsible for the opt-out.
        """

        resp = client.post(
            "/fleet/install",
            json={"template_name": "sales-fleet", "journal": False},
        )
        assert resp.status_code == 200

        assert patch_install_fleet.call_count == 1
        kwargs = patch_install_fleet.call_args.kwargs
        assert kwargs["journal"] is None

    def test_unknown_template_returns_404(self, client: TestClient) -> None:
        """Missing templates surface as 404 with a message that names the
        offending ``template_name``.
        """

        resp = client.post(
            "/fleet/install",
            json={"template_name": "does-not-exist", "journal": False},
        )
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]

    def test_malformed_body_returns_422(self, client: TestClient) -> None:
        """Missing required ``template_name`` field must fail validation
        before the installer is even considered.
        """

        resp = client.post("/fleet/install", json={})
        assert resp.status_code == 422

    def test_actor_spec_is_forwarded_to_installer(
        self,
        client: TestClient,
        patch_install_fleet,
        read_journal,
    ) -> None:
        """When the caller supplies an ActorSpec it reaches the journal
        events as the authoring actor instead of the fallback
        ``system:fleet-installer`` identity.
        """

        resp = client.post(
            "/fleet/install",
            json={
                "template_name": "sales-fleet",
                "journal": True,
                "actor": {
                    "kind": "user",
                    "id": "user-123",
                    "scope_context": ["org:sales:*"],
                },
            },
        )
        assert resp.status_code == 200

        events = read_journal()
        assert events, "journal should have captured events"
        for event in events:
            assert event.actor.kind == "user"
            assert event.actor.id == "user-123"


# ---------------------------------------------------------------------------
# Response shape — a smoke test to keep Pydantic warnings out of the logs
# when FastAPI serializes the install report.
# ---------------------------------------------------------------------------


class TestResponseShape:
    def test_install_report_serializes_without_warnings(
        self,
        client: TestClient,
        patch_install_fleet,
        recwarn: Any,
    ) -> None:
        """Serialising the install report must not raise PydanticSerializationUnexpectedValue
        or similar warnings — the router's response_model is the canonical
        ``FleetInstallReport`` so downstream TypeScript clients can rely on it.
        """

        resp = client.post(
            "/fleet/install",
            json={"template_name": "sales-fleet", "journal": False},
        )
        assert resp.status_code == 200
        pydantic_warnings = [w for w in recwarn.list if "pydantic" in str(w.category).lower()]
        assert not pydantic_warnings, [str(w) for w in pydantic_warnings]
