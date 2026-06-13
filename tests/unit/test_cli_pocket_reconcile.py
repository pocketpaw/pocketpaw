# tests/unit/test_cli_pocket_reconcile.py
# Created: 2026-06-13 (feat/pocket-template-reconcile, P2.4) — tests for the
# `pocketpaw pocket reconcile` CLI adapter. The CLI is thin: it builds the
# loopback-bypass headers and POSTs to the running dashboard's reconcile
# endpoints. These tests invoke ``run_pocket_cmd`` directly and stub
# ``httpx.post`` so no live server is needed — they pin the adapter's
# contract: correct URL/verb, the four bypass headers, exit codes, and the
# preview-vs-apply / --json output shapes. The reconcile BEHAVIOUR (partition
# correctness) is covered by tests/cloud/pockets/test_template_reconcile*.py.
"""Unit tests for the reconcile CLI adapter (network stubbed)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from pocketpaw.cli import pocket as pocket_cli


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Recorder:
    """Captures the single httpx.post call and returns a canned response."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.url: str | None = None
        self.headers: dict | None = None

    def __call__(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.url = url
        self.headers = headers
        return self.response


@pytest.fixture(autouse=True)
def _identity_env(monkeypatch) -> None:
    """Provide the bypass token + identity so the command reaches the network
    stub rather than short-circuiting on a missing-credential guard."""
    monkeypatch.setenv("POCKETPAW_INTERNAL_TOKEN", "tok-123")
    monkeypatch.setenv("POCKETPAW_WORKSPACE_ID", "ws_1")
    monkeypatch.setenv("POCKETPAW_USER_ID", "u_1")


def _install_stub(monkeypatch, response: _FakeResponse) -> _Recorder:
    import httpx

    rec = _Recorder(response)
    monkeypatch.setattr(httpx, "post", rec)
    return rec


def test_preview_hits_preview_endpoint_with_bypass_headers(monkeypatch) -> None:
    diff = {
        "pocket_id": "p1",
        "template_slug": "applications-triage",
        "template_owned_regions": ["ui", "actions", "sources", "shape"],
        "changed_regions": ["ui"],
        "unchanged_regions": ["actions", "sources", "shape"],
        "preserved_regions": ["state"],
        "has_changes": True,
    }
    rec = _install_stub(monkeypatch, _FakeResponse(200, diff))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pocket_cli.run_pocket_cmd(subaction="reconcile", pocket_id="p1")

    assert rc == 0
    # Default is preview (no --apply).
    assert rec.url.endswith("/api/v1/pockets/p1/reconcile/preview")
    # All four bypass headers present and correct.
    assert rec.headers["X-PocketPaw-Internal"] == "true"
    assert rec.headers["X-PocketPaw-Internal-Token"] == "tok-123"
    assert rec.headers["X-PocketPaw-Workspace-Id"] == "ws_1"
    assert rec.headers["X-PocketPaw-User-Id"] == "u_1"
    out = buf.getvalue()
    assert "applications-triage" in out
    assert "ui" in out


def test_apply_hits_apply_endpoint(monkeypatch) -> None:
    body = {
        "ok": True,
        "skipped": False,
        "diff": {"changed_regions": ["actions"], "preserved_regions": ["state"]},
        "pocket": {"_id": "p1"},
    }
    rec = _install_stub(monkeypatch, _FakeResponse(200, body))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pocket_cli.run_pocket_cmd(subaction="reconcile", pocket_id="p1", apply=True)

    assert rc == 0
    assert rec.url.endswith("/api/v1/pockets/p1/reconcile/apply")
    assert "Reconciled pocket p1" in buf.getvalue()


def test_apply_skipped_when_in_sync(monkeypatch) -> None:
    body = {"ok": True, "skipped": True, "diff": {"changed_regions": []}}
    _install_stub(monkeypatch, _FakeResponse(200, body))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pocket_cli.run_pocket_cmd(subaction="reconcile", pocket_id="p1", apply=True)

    assert rc == 0
    assert "already matches its template" in buf.getvalue()


def test_json_output_passthrough(monkeypatch) -> None:
    diff = {"pocket_id": "p1", "template_slug": "t", "changed_regions": []}
    _install_stub(monkeypatch, _FakeResponse(200, diff))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pocket_cli.run_pocket_cmd(subaction="reconcile", pocket_id="p1", as_json=True)

    assert rc == 0
    assert json.loads(buf.getvalue()) == diff


def test_server_error_surfaces_message(monkeypatch, capsys) -> None:
    err = {"error": {"code": "reconcile.no_template", "message": "nothing to reconcile"}}
    _install_stub(monkeypatch, _FakeResponse(422, err))

    rc = pocket_cli.run_pocket_cmd(subaction="reconcile", pocket_id="p1")
    assert rc == 1
    captured = capsys.readouterr()
    assert "nothing to reconcile" in captured.err
    assert "reconcile.no_template" in captured.err


def test_missing_token_is_clean_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("POCKETPAW_INTERNAL_TOKEN", raising=False)
    rc = pocket_cli.run_pocket_cmd(
        subaction="reconcile", pocket_id="p1", workspace="ws_1", user="u_1"
    )
    assert rc == 2
    assert "POCKETPAW_INTERNAL_TOKEN" in capsys.readouterr().err


def test_unknown_subaction_usage(capsys) -> None:
    rc = pocket_cli.run_pocket_cmd(subaction="frobnicate", pocket_id="p1")
    assert rc == 2
    assert "Usage:" in capsys.readouterr().err


def test_connection_error_is_friendly(monkeypatch, capsys) -> None:
    import httpx

    def _boom(url, headers=None, timeout=None):  # noqa: ANN001
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", _boom)
    rc = pocket_cli.run_pocket_cmd(subaction="reconcile", pocket_id="p1")
    assert rc == 1
    assert "not running" in capsys.readouterr().err
