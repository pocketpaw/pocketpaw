# tests/test_plugin_installer.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — covers the
# .claude-plugin skills installer: manifest parse + structural validation,
# source parsing, skills discovery/copy/reload, registry write, and error
# paths (bad source, missing manifest, no skills). The clone is mocked by
# injecting a clone factory that yields a local fixture .claude-plugin dir.
# Updated: 2026-06-08 (feat/plugin-installer-mcp) — added the MCP-routing
# slice: .mcp.json -> MCPServerConfig mapping, plugin:<p>:<server>
# namespacing, registered-but-needs-env (succeeded + needs-env detail),
# a skills+mcp bundle, and the no-.mcp.json skip. The MCP manager is mocked
# via a fake injected with monkeypatch (no real servers are spawned).
"""Unit tests for pocketpaw.plugins (skills + MCP slices)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from pocketpaw.plugins.installer import (
    PluginInstaller,
    PluginInstallError,
    parse_source,
)
from pocketpaw.plugins.models import PluginManifest


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _write_plugin(
    root: Path,
    *,
    name: str = "my-plugin",
    version: str = "1.2.3",
    skills: list[str] | None = None,
    skills_subdir: str = "skills",
    write_manifest: bool = True,
) -> Path:
    """Build a fake .claude-plugin tree under *root* and return it."""
    if write_manifest:
        manifest_dir = root / ".claude-plugin"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps({"name": name, "version": version, "description": "test"}),
            encoding="utf-8",
        )
    for skill_name in skills or []:
        sk = root / skills_subdir / skill_name
        sk.mkdir(parents=True, exist_ok=True)
        (sk / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
    return root


@pytest.fixture
def fixture_repo(tmp_path) -> Path:
    """A valid plugin repo with two skills."""
    return _write_plugin(tmp_path / "repo", skills=["alpha", "beta"])


def _clone_factory(repo_root: Path):
    """Return a clone factory that yields *repo_root* (no real git)."""

    @asynccontextmanager
    async def _clone(owner, repo, timeout=60):
        yield repo_root

    return _clone


def _installer(repo_root: Path, tmp_path: Path) -> PluginInstaller:
    return PluginInstaller(
        clone_factory=_clone_factory(repo_root),
        install_dir=tmp_path / "skills_install",
        registry_path=tmp_path / "registry" / "plugins.json",
    )


# --------------------------------------------------------------------------- #
# Source parsing                                                              #
# --------------------------------------------------------------------------- #
class TestParseSource:
    def test_owner_repo(self):
        assert parse_source("acme/widgets") == ("acme", "widgets", None)

    def test_owner_repo_subdir(self):
        assert parse_source("acme/widgets/plugins/foo") == ("acme", "widgets", "plugins/foo")

    def test_https_url(self):
        assert parse_source("https://github.com/acme/widgets") == ("acme", "widgets", None)

    def test_url_with_git_suffix(self):
        assert parse_source("https://github.com/acme/widgets.git") == ("acme", "widgets", None)

    def test_url_with_tree_subdir(self):
        owner, repo, subdir = parse_source("https://github.com/acme/widgets/tree/main/plugins/x")
        assert (owner, repo, subdir) == ("acme", "widgets", "plugins/x")

    def test_empty_source_raises(self):
        with pytest.raises(PluginInstallError) as exc:
            parse_source("   ")
        assert exc.value.status_code == 400

    def test_single_segment_raises(self):
        with pytest.raises(PluginInstallError):
            parse_source("justowner")

    def test_invalid_owner_raises(self):
        with pytest.raises(PluginInstallError):
            parse_source("bad owner/repo")

    def test_traversal_subdir_raises(self):
        with pytest.raises(PluginInstallError):
            parse_source("acme/widgets/../etc")


# --------------------------------------------------------------------------- #
# Manifest model                                                              #
# --------------------------------------------------------------------------- #
class TestPluginManifest:
    def test_minimal_manifest(self):
        m = PluginManifest.model_validate({"name": "x"})
        assert m.name == "x"
        assert m.version == "0.0.0"
        assert m.skills is None

    def test_full_manifest(self):
        m = PluginManifest.model_validate(
            {"name": "x", "version": "2.0", "description": "d", "skills": "custom"}
        )
        assert (m.version, m.description, m.skills) == ("2.0", "d", "custom")

    def test_missing_name_invalid(self):
        with pytest.raises(Exception):
            PluginManifest.model_validate({"version": "1.0"})


# --------------------------------------------------------------------------- #
# Install happy path                                                          #
# --------------------------------------------------------------------------- #
class TestInstall:
    async def test_installs_skills_and_returns_report(self, fixture_repo, tmp_path, monkeypatch):
        reloaded = {"count": 0}
        monkeypatch.setattr(
            "pocketpaw.plugins.installer.get_skill_loader",
            lambda: type("L", (), {"reload": lambda self: reloaded.__setitem__("count", 1)})(),
        )

        inst = _installer(fixture_repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert report.plugin == "my-plugin"
        assert sorted(report.installed_skills) == ["alpha", "beta"]
        assert report.succeeded()
        assert reloaded["count"] == 1

        # Files copied into the loader path.
        install_dir = tmp_path / "skills_install"
        assert (install_dir / "alpha" / "SKILL.md").is_file()
        assert (install_dir / "beta" / "SKILL.md").is_file()

        # Step report has the expected stages.
        names = [s.name for s in report.steps]
        assert "read_manifest" in names
        assert "skill:alpha" in names
        assert "reload_loader" in names
        assert "record_registry" in names

    async def test_writes_registry_entry(self, fixture_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "pocketpaw.plugins.installer.get_skill_loader",
            lambda: type("L", (), {"reload": lambda self: None})(),
        )
        inst = _installer(fixture_repo, tmp_path)
        await inst.install("acme/widgets")

        registry_path = tmp_path / "registry" / "plugins.json"
        assert registry_path.is_file()
        registry = json.loads(registry_path.read_text())
        assert "my-plugin" in registry
        entry = registry["my-plugin"]
        assert entry["version"] == "1.2.3"
        assert entry["source"] == "acme/widgets"
        assert sorted(entry["skills"]) == ["alpha", "beta"]
        assert "installed_at" in entry

    async def test_registry_merges_existing(self, fixture_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "pocketpaw.plugins.installer.get_skill_loader",
            lambda: type("L", (), {"reload": lambda self: None})(),
        )
        registry_path = tmp_path / "registry" / "plugins.json"
        registry_path.parent.mkdir(parents=True)
        registry_path.write_text(json.dumps({"other": {"version": "9"}}))

        inst = _installer(fixture_repo, tmp_path)
        await inst.install("acme/widgets")

        registry = json.loads(registry_path.read_text())
        assert "other" in registry  # untouched
        assert "my-plugin" in registry

    async def test_subdir_plugin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "pocketpaw.plugins.installer.get_skill_loader",
            lambda: type("L", (), {"reload": lambda self: None})(),
        )
        repo = tmp_path / "repo"
        _write_plugin(repo / "plugins" / "foo", name="foo", skills=["one"])

        inst = PluginInstaller(
            clone_factory=_clone_factory(repo),
            install_dir=tmp_path / "skills_install",
            registry_path=tmp_path / "registry" / "plugins.json",
        )
        report = await inst.install("acme/widgets/plugins/foo")
        assert report.plugin == "foo"
        assert report.installed_skills == ["one"]

    async def test_manifest_skills_path_override(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "pocketpaw.plugins.installer.get_skill_loader",
            lambda: type("L", (), {"reload": lambda self: None})(),
        )
        repo = tmp_path / "repo"
        _write_plugin(repo, name="ov", skills=["s1"], skills_subdir="custom-skills")
        # Patch the manifest to point skills at the custom dir.
        manifest = repo / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps({"name": "ov", "skills": "custom-skills"}))

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")
        assert report.installed_skills == ["s1"]


# --------------------------------------------------------------------------- #
# Error paths                                                                 #
# --------------------------------------------------------------------------- #
class TestInstallErrors:
    async def test_bad_source(self, tmp_path):
        inst = _installer(tmp_path, tmp_path)
        with pytest.raises(PluginInstallError) as exc:
            await inst.install("nope")
        assert exc.value.status_code == 400

    async def test_missing_manifest(self, tmp_path):
        repo = _write_plugin(tmp_path / "repo", skills=["a"], write_manifest=False)
        inst = _installer(repo, tmp_path)
        with pytest.raises(PluginInstallError) as exc:
            await inst.install("acme/widgets")
        assert exc.value.status_code == 404

    async def test_no_skills(self, tmp_path):
        repo = _write_plugin(tmp_path / "repo", skills=[])  # manifest only
        inst = _installer(repo, tmp_path)
        with pytest.raises(PluginInstallError) as exc:
            await inst.install("acme/widgets")
        assert exc.value.status_code == 404

    async def test_invalid_manifest_json(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".claude-plugin").mkdir(parents=True)
        (repo / ".claude-plugin" / "plugin.json").write_text("{not json")
        inst = _installer(repo, tmp_path)
        with pytest.raises(PluginInstallError) as exc:
            await inst.install("acme/widgets")
        assert exc.value.status_code == 400

    async def test_clone_failure_maps_to_502(self, tmp_path, monkeypatch):
        @asynccontextmanager
        async def boom(owner, repo, timeout=60):
            raise RuntimeError("Clone failed: not found")
            yield  # pragma: no cover

        inst = PluginInstaller(
            clone_factory=boom,
            install_dir=tmp_path / "i",
            registry_path=tmp_path / "r.json",
        )
        with pytest.raises(PluginInstallError) as exc:
            await inst.install("acme/widgets")
        assert exc.value.status_code == 502

    async def test_clone_timeout_maps_to_504(self, tmp_path):
        @asynccontextmanager
        async def slow(owner, repo, timeout=60):
            raise TimeoutError
            yield  # pragma: no cover

        inst = PluginInstaller(
            clone_factory=slow,
            install_dir=tmp_path / "i",
            registry_path=tmp_path / "r.json",
        )
        with pytest.raises(PluginInstallError) as exc:
            await inst.install("acme/widgets")
        assert exc.value.status_code == 504


# --------------------------------------------------------------------------- #
# MCP routing                                                                 #
# --------------------------------------------------------------------------- #
class _FakeMCPManager:
    """Records add_server_config + start_server calls; never spawns servers.

    ``start_results`` maps a (namespaced) server name to the bool returned
    by ``start_server`` — defaults to True (started cleanly) when unset.
    """

    def __init__(self, start_results: dict[str, bool] | None = None):
        self.added: list = []  # MCPServerConfig objects
        self.started: list = []  # MCPServerConfig objects
        self._start_results = start_results or {}

    def add_server_config(self, config) -> None:
        self.added.append(config)

    async def start_server(self, config) -> bool:
        self.started.append(config)
        return self._start_results.get(config.name, True)


def _write_mcp_json(plugin_root: Path, servers: dict) -> None:
    (plugin_root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _patch_loader_and_manager(monkeypatch, manager: _FakeMCPManager) -> None:
    monkeypatch.setattr(
        "pocketpaw.plugins.installer.get_skill_loader",
        lambda: type("L", (), {"reload": lambda self: None})(),
    )
    monkeypatch.setattr(
        "pocketpaw.plugins.installer.get_mcp_manager",
        lambda: manager,
    )


class TestMCPRouting:
    async def test_no_mcp_json_skips_cleanly(self, fixture_repo, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        inst = _installer(fixture_repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert report.installed_mcp_servers == []
        assert report.succeeded()
        mcp_steps = [s for s in report.steps if s.name == "mcp"]
        assert mcp_steps and mcp_steps[0].status == "skipped"
        assert manager.added == []

    async def test_spec_maps_to_config_and_namespaces(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="my-plugin", skills=["alpha"])
        _write_mcp_json(
            repo,
            {
                "weather": {
                    "command": "npx",
                    "args": ["-y", "weather-mcp"],
                    "env": {"REGION": "us"},
                }
            },
        )

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert report.installed_mcp_servers == ["plugin:my-plugin:weather"]
        # Config mapped correctly.
        assert len(manager.added) == 1
        cfg = manager.added[0]
        assert cfg.name == "plugin:my-plugin:weather"
        assert cfg.transport == "stdio"  # default when type unset
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "weather-mcp"]
        assert cfg.env == {"REGION": "us"}
        # Server was started.
        assert [c.name for c in manager.started] == ["plugin:my-plugin:weather"]
        # Step succeeded.
        step = next(s for s in report.steps if s.name == "mcp:weather")
        assert step.status == "succeeded"

    async def test_http_transport_from_type(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="p", skills=["alpha"])
        _write_mcp_json(
            repo,
            {
                "remote": {"type": "streamable-http", "url": "https://mcp.example.com"},
            },
        )

        inst = _installer(repo, tmp_path)
        await inst.install("acme/widgets")

        cfg = manager.added[0]
        assert cfg.transport == "streamable-http"
        assert cfg.url == "https://mcp.example.com"
        assert cfg.command == ""

    async def test_needs_env_is_non_fatal(self, tmp_path, monkeypatch):
        # Server registers but start_server returns False; the spec declares
        # an empty env var, so the step is succeeded with a needs-env detail.
        ns_name = "plugin:p:db"
        manager = _FakeMCPManager(start_results={ns_name: False})
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="p", skills=["alpha"])
        _write_mcp_json(
            repo,
            {"db": {"command": "npx", "args": ["db-mcp"], "env": {"DB_URL": ""}}},
        )

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        step = next(s for s in report.steps if s.name == "mcp:db")
        assert step.status == "succeeded"
        assert "needs env: DB_URL" in step.detail
        # Still recorded as installed (registered, just not running yet).
        assert report.installed_mcp_servers == [ns_name]
        assert report.succeeded()

    async def test_hard_start_failure_is_failed(self, tmp_path, monkeypatch):
        # start_server returns False AND there is no missing env → failed.
        ns_name = "plugin:p:broken"
        manager = _FakeMCPManager(start_results={ns_name: False})
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="p", skills=["alpha"])
        _write_mcp_json(
            repo,
            {"broken": {"command": "npx", "args": ["broken-mcp"]}},
        )

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        step = next(s for s in report.steps if s.name == "mcp:broken")
        assert step.status == "failed"
        assert ns_name not in report.installed_mcp_servers
        assert not report.succeeded()

    async def test_skills_and_mcp_together(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="combo", skills=["alpha", "beta"])
        _write_mcp_json(repo, {"svc": {"command": "uvx", "args": ["svc-mcp"]}})

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert sorted(report.installed_skills) == ["alpha", "beta"]
        assert report.installed_mcp_servers == ["plugin:combo:svc"]
        assert report.succeeded()

        # Registry entry records both skills and mcp servers.
        registry_path = tmp_path / "registry" / "plugins.json"
        entry = json.loads(registry_path.read_text())["combo"]
        assert sorted(entry["skills"]) == ["alpha", "beta"]
        assert entry["mcp_servers"] == ["plugin:combo:svc"]

    async def test_empty_mcp_servers_skips(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="p", skills=["alpha"])
        _write_mcp_json(repo, {})  # file exists, no servers

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert report.installed_mcp_servers == []
        step = next(s for s in report.steps if s.name == "mcp")
        assert step.status == "skipped"

    async def test_manifest_mcp_path_override(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="ov", skills=["alpha"])
        # Point the manifest at a custom MCP config path.
        manifest = repo / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps({"name": "ov", "mcp_servers": "config/mcp.json"}))
        custom = repo / "config"
        custom.mkdir(parents=True)
        (custom / "mcp.json").write_text(
            json.dumps({"mcpServers": {"svc": {"command": "npx", "args": ["svc"]}}})
        )

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert report.installed_mcp_servers == ["plugin:ov:svc"]
