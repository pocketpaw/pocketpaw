# tests/ee/agent/test_sites_mcp_server/conftest.py
# Created 2026-09-24 (PP-2, feat/sites-verify-pipeline): every create and edit tool now
# runs ``pocketpaw_ee.sites.verify.verify_site`` before it answers. Left real, each tool
# test would shell out to ``paw-sites-gen check`` and reach for Redis — slow, and
# dependent on what the developer's machine has installed. The autouse fixture below
# replaces ``verify_site`` with a recorder that returns a canned verdict. A test that
# cares about the verdict requests ``verify_recorder`` by name and sets ``.verdict``;
# the pipeline itself is covered by tests/ee/sites/test_verify_pipeline.py.
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")


def canned_verdict(status: str = "passed", **extra: Any) -> dict[str, Any]:
    layers = [
        {"name": "static", "status": status if status != "unverified" else "passed"},
        {"name": "build", "status": status if status != "unverified" else "unverified"},
        {"name": "browser", "status": status if status != "unverified" else "unverified"},
    ]
    verdict: dict[str, Any] = {
        "status": status,
        "content_hash": "h" * 64,
        "layers": layers,
        "errors": [],
        "warnings": [],
    }
    if status == "unverified":
        verdict["reason"] = "sandbox_unavailable"
    verdict.update(extra)
    return verdict


class VerifyRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verdict: dict[str, Any] = canned_verdict()

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.verdict


@pytest.fixture(autouse=True)
def verify_recorder(monkeypatch) -> VerifyRecorder:
    from pocketpaw_ee.sites import verify

    recorder = VerifyRecorder()
    monkeypatch.setattr(verify, "verify_site", recorder)
    return recorder
