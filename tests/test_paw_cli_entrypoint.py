# tests/test_paw_cli_entrypoint.py — the `paw` console script + the remote
# `paw fabric` read commands (paw-cli C1).
# Created: 2026-07-11 (feat/paw-cli).
# What this pins:
#   * the `paw` entry point is declared and resolves to pocketpaw.paw.cli:main
#     (importlib.metadata — the same lookup pip/uv use to generate the script).
#   * `paw fabric stats` / `paw fabric query` execute end-to-end against a
#     stubbed httpx transport (the REAL PawClient runs; only the wire is faked)
#     and print the server's JSON.
#   * connection failures exit non-zero with a readable hint, not a traceback.

from __future__ import annotations

import importlib.metadata
import json

import httpx
from click.testing import CliRunner

import pocketpaw.paw.cli as paw_cli
from pocketpaw.paw.client import PawClient

# ---------------------------------------------------------------------------
# Entry-point contract
# ---------------------------------------------------------------------------


def test_paw_console_script_declared_and_resolves():
    """The installed dist declares `paw` and it loads to the Click group."""
    eps = importlib.metadata.entry_points(group="console_scripts", name="paw")
    matches = [ep for ep in eps if ep.value == "pocketpaw.paw.cli:main"]
    assert matches, "console script `paw` missing or mis-declared in [project.scripts]"
    assert matches[0].load() is paw_cli.main


def test_paw_help_lists_fabric_group():
    runner = CliRunner()
    result = runner.invoke(paw_cli.main, ["--help"])
    assert result.exit_code == 0
    assert "fabric" in result.output


# ---------------------------------------------------------------------------
# paw fabric — reads over a stubbed transport
# ---------------------------------------------------------------------------


def _stub_client_factory(handler):
    """A _make_client replacement returning a PawClient on a MockTransport."""

    def factory(base_url: str, api_key: str | None) -> PawClient:
        return PawClient(base_url, api_key, transport=httpx.MockTransport(handler))

    return factory


def test_fabric_stats_prints_server_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/fabric/stats"
        return httpx.Response(200, json={"types": 3, "objects": 42, "links": 7})

    monkeypatch.setattr(paw_cli, "_make_client", _stub_client_factory(handler))
    runner = CliRunner()
    result = runner.invoke(paw_cli.main, ["fabric", "stats"], catch_exceptions=False)

    assert result.exit_code == 0
    assert json.loads(result.output) == {"types": 3, "objects": 42, "links": 7}


def test_fabric_query_sends_options_and_prints_objects(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["type_name"] == "Customer"
        assert body["limit"] == 5
        return httpx.Response(
            200,
            json={"objects": [{"id": "o1", "type_name": "Customer"}], "total": 1},
        )

    monkeypatch.setattr(paw_cli, "_make_client", _stub_client_factory(handler))
    runner = CliRunner()
    result = runner.invoke(
        paw_cli.main,
        ["fabric", "query", "--type-name", "Customer", "--limit", "5"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["total"] == 1


def test_fabric_api_error_exits_nonzero(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Enterprise license required"})

    monkeypatch.setattr(paw_cli, "_make_client", _stub_client_factory(handler))
    runner = CliRunner()
    result = runner.invoke(paw_cli.main, ["fabric", "types"])

    assert result.exit_code == 1
    assert "Enterprise license required" in result.output


def test_fabric_connection_error_exits_with_hint(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(paw_cli, "_make_client", _stub_client_factory(handler))
    runner = CliRunner()
    result = runner.invoke(paw_cli.main, ["fabric", "stats"])

    assert result.exit_code == 1
    assert "Could not reach" in result.output
