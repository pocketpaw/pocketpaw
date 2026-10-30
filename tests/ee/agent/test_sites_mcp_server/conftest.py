# tests/ee/agent/test_sites_mcp_server/conftest.py
# Every create tool runs ``pocketpaw_ee.sites.verify.verify_site`` and every edit tool
# ``verify.verify_edit`` before it answers. Left real, each tool test would shell out
# to ``paw-sites-gen check`` and reach for Redis — slow, and dependent on what the
# developer's machine has installed. The autouse fixtures replace both with recorders
# that return a canned verdict (``verify_recorder`` / ``edit_verify_recorder``; a test
# that cares sets ``.verdict``), and point the verify store (read by every edit tool for
# ``previous_verification``) at a per-test temp dir. The pipeline itself is covered by
# tests/ee/sites/test_verify_pipeline.py and test_fast_edit_verify.py.
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


def canned_pending(**extra: Any) -> dict[str, Any]:
    """What a clean edit returns: static passed, the sandbox build queued."""
    verdict: dict[str, Any] = {
        "status": "pending",
        "content_hash": "e" * 64,
        "static": "passed",
        "build": "pending",
        "job_id": "site-preview-p-" + "e" * 64,
        "layers": [
            {"name": "static", "status": "passed"},
            {"name": "build", "status": "pending"},
            {"name": "browser", "status": "pending"},
        ],
        "errors": [],
        "warnings": [],
    }
    verdict.update(extra)
    return verdict


@pytest.fixture(autouse=True)
def edit_verify_recorder(monkeypatch) -> VerifyRecorder:
    from pocketpaw_ee.sites import verify

    recorder = VerifyRecorder()
    recorder.verdict = canned_pending()
    monkeypatch.setattr(verify, "verify_edit", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _verify_store_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("PAW_SITES_ARTIFACT_DIR", str(tmp_path / "site-artifacts"))
    monkeypatch.delenv("PAW_SITES_ARTIFACT_STORE", raising=False)
