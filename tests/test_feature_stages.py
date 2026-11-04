# Tests for pocketpaw.feature_stages and GET /api/v1/config/features.
#
# The route tests carry ``enforce_scope``: without it the root conftest bypasses
# require_scope and the anonymous-403 case would pass with the guard deleted.

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pocketpaw
from pocketpaw.agents.registry import get_backend_info, list_backends
from pocketpaw.api.v1.health import router
from pocketpaw.feature_stages import ALPHA, HIDDEN, PREVIEW, beta_backend_ids, stages

ADAPTERS_DIR = Path(pocketpaw.__file__).parent / "bus" / "adapters"


def _all_ids() -> list[str]:
    return [*HIDDEN, *ALPHA, *PREVIEW, *beta_backend_ids()]


def _segment_match(path: str, stage_id: str) -> bool:
    """The client's rule: exact, or a sub-path of the id."""
    return path == stage_id or path.startswith(stage_id + "/")


def test_every_id_is_a_route_or_runtime_id():
    for stage_id in _all_ids():
        assert stage_id.startswith(("/", "channel:", "backend:")), stage_id


def test_every_channel_id_names_a_real_adapter():
    channels = [i.removeprefix("channel:") for i in _all_ids() if i.startswith("channel:")]
    assert channels
    for name in channels:
        assert (ADAPTERS_DIR / f"{name}_adapter.py").is_file(), name


def test_stages_are_disjoint():
    preview = set(PREVIEW)
    assert not HIDDEN & ALPHA
    assert not HIDDEN & preview
    assert not ALPHA & preview


def test_no_route_in_one_stage_segment_matches_another_stage():
    """/belt must not swallow /belt-next, /agent-health not /agent-health-lab."""
    groups = {"hidden": HIDDEN, "alpha": ALPHA, "preview": set(PREVIEW)}
    for name, ids in groups.items():
        others = {i for other, s in groups.items() if other != name for i in s}
        for a in ids:
            for b in others:
                if a.startswith("/") and b.startswith("/"):
                    assert not _segment_match(b, a), f"{name} {a} matches {b}"


def test_belt_hidden_and_belt_next_preview():
    assert "/belt" in HIDDEN
    assert PREVIEW["/belt-next"] == "in-the-works"
    assert "/belt-next" not in HIDDEN
    assert not _segment_match("/belt-next", "/belt")


def test_backend_ids_follow_backend_info_beta():
    expected = set()
    for name in list_backends():
        info = get_backend_info(name)
        if info is not None and info.beta:
            expected.add(f"backend:{name}")
    assert beta_backend_ids() == expected
    assert "backend:pydantic_ai" not in beta_backend_ids()


def test_pydantic_ai_is_not_beta():
    info = get_backend_info("pydantic_ai")
    if info is None:
        pytest.skip("pydantic_ai backend module not importable")
    assert info.beta is False


def test_stages_shape():
    data = stages()
    assert set(data) == {"alpha", "hidden", "preview"}
    assert data["alpha"] == sorted(ALPHA | beta_backend_ids())
    assert data["hidden"] == sorted(HIDDEN)
    assert data["preview"] == PREVIEW
    assert data["preview"] is not PREVIEW


def _client(**state) -> TestClient:
    """App whose middleware stamps request.state the way the auth layers do."""
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        for k, v in state.items():
            setattr(request.state, k, v)
        return await call_next(request)

    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.enforce_scope
class TestFeaturesRoute:
    def test_anonymous_is_refused(self):
        assert _client().get("/api/v1/config/features").status_code == 403

    def test_inactive_cloud_user_is_refused(self):
        resp = _client(user_id="u1").get("/api/v1/config/features")
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "state",
        [
            {"user_id": "u1", "ee_user_authenticated": True},  # cloud JWT user
            {"full_access": True},  # OSS dashboard session / master token / localhost
        ],
    )
    def test_signed_in_caller_gets_the_stages(self, state):
        resp = _client(**state).get("/api/v1/config/features")
        assert resp.status_code == 200
        assert resp.json() == stages()

    def test_version_stays_open(self):
        assert _client().get("/api/v1/version").status_code == 200
