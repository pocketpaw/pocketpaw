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
# Updated: 2026-06-08 (feat/plugin-installer-listremove, #1358) — added the
# list/remove slice: install-then-list, install-then-remove (deletes exactly
# this plugin's skill dirs + MCP servers, drops the registry entry, leaves
# nothing orphaned), unknown-plugin raises 404, degraded remove (a missing
# component still completes + drops the entry), and the atomic registry write.
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
        self.removed: list[str] = []  # names passed to remove_server_config
        self._start_results = start_results or {}
        # Names the fake "knows about" so remove_server_config can report
        # found/not-found. Defaults to whatever was added.
        self.known: set[str] = set()

    def add_server_config(self, config) -> None:
        self.added.append(config)
        self.known.add(config.name)

    async def start_server(self, config) -> bool:
        self.started.append(config)
        return self._start_results.get(config.name, True)

    def remove_server_config(self, name) -> bool:
        self.removed.append(name)
        if name in self.known:
            self.known.discard(name)
            return True
        return False


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

    async def test_malformed_mcp_json_does_not_orphan_skills(self, tmp_path, monkeypatch):
        # A bad .mcp.json must degrade to a failed mcp step — NOT raise — so
        # the skills stay installed and the registry entry is still written.
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="p", skills=["alpha"])
        (repo / ".mcp.json").write_text("{not valid json", encoding="utf-8")

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")  # must not raise

        # Skills installed.
        assert report.installed_skills == ["alpha"]
        assert (tmp_path / "skills_install" / "alpha" / "SKILL.md").is_file()
        # mcp step failed (not skipped), report not fully succeeded.
        mcp_step = next(s for s in report.steps if s.name == "mcp")
        assert mcp_step.status == "failed"
        assert report.installed_mcp_servers == []
        assert not report.succeeded()
        # Registry entry still written — skills are NOT orphaned.
        registry_path = tmp_path / "registry" / "plugins.json"
        assert registry_path.is_file()
        entry = json.loads(registry_path.read_text())["p"]
        assert entry["skills"] == ["alpha"]
        assert entry["mcp_servers"] == []
        assert manager.added == []  # nothing registered from a bad config

    async def test_malformed_mcp_servers_shape_is_failed_step(self, tmp_path, monkeypatch):
        # Top-level dict but mcpServers is a list → failed step, no raise.
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="p", skills=["alpha"])
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": []}), encoding="utf-8")

        inst = _installer(repo, tmp_path)
        report = await inst.install("acme/widgets")

        assert report.installed_skills == ["alpha"]
        mcp_step = next(s for s in report.steps if s.name == "mcp")
        assert mcp_step.status == "failed"
        assert (tmp_path / "registry" / "plugins.json").is_file()

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


# --------------------------------------------------------------------------- #
# List + remove (#1358)                                                       #
# --------------------------------------------------------------------------- #
class TestListPlugins:
    async def test_install_then_list(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)

        repo = _write_plugin(tmp_path / "repo", name="combo", skills=["alpha", "beta"])
        _write_mcp_json(repo, {"svc": {"command": "uvx", "args": ["svc-mcp"]}})

        inst = _installer(repo, tmp_path)
        await inst.install("acme/widgets")

        plugins = inst.list_plugins()
        assert len(plugins) == 1
        p = plugins[0]
        assert p.name == "combo"
        assert p.version == "1.2.3"
        assert p.source == "acme/widgets"
        assert sorted(p.skills) == ["alpha", "beta"]
        assert p.mcp_servers == ["plugin:combo:svc"]
        assert p.installed_at  # ISO timestamp recorded

    def test_list_empty_when_no_registry(self, tmp_path):
        inst = _installer(tmp_path, tmp_path)
        assert inst.list_plugins() == []

    def test_list_skips_malformed_entry(self, tmp_path):
        registry_path = tmp_path / "registry" / "plugins.json"
        registry_path.parent.mkdir(parents=True)
        registry_path.write_text(json.dumps({"good": {"version": "1"}, "bad": "not-a-dict"}))
        inst = _installer(tmp_path, tmp_path)
        plugins = inst.list_plugins()
        assert [p.name for p in plugins] == ["good"]


class TestRemovePlugin:
    async def _install_one(self, tmp_path, monkeypatch, *, name="combo", skills, mcp):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)
        repo = _write_plugin(tmp_path / "repo", name=name, skills=skills)
        if mcp:
            _write_mcp_json(repo, mcp)
        inst = _installer(repo, tmp_path)
        await inst.install("acme/widgets")
        return inst, manager

    async def test_install_then_remove_cleans_everything(self, tmp_path, monkeypatch):
        inst, manager = await self._install_one(
            tmp_path,
            monkeypatch,
            skills=["alpha", "beta"],
            mcp={"svc": {"command": "uvx", "args": ["svc-mcp"]}},
        )
        install_dir = tmp_path / "skills_install"
        assert (install_dir / "alpha").is_dir()
        assert (install_dir / "beta").is_dir()

        report = await inst.remove("combo")

        assert report.succeeded()
        assert sorted(report.removed_skills) == ["alpha", "beta"]
        assert report.removed_mcp_servers == ["plugin:combo:svc"]
        # Skill dirs gone.
        assert not (install_dir / "alpha").exists()
        assert not (install_dir / "beta").exists()
        # MCP manager asked to remove the namespaced name.
        assert manager.removed == ["plugin:combo:svc"]
        # Registry entry dropped.
        registry_path = tmp_path / "registry" / "plugins.json"
        assert "combo" not in json.loads(registry_path.read_text())
        assert inst.list_plugins() == []

    async def test_remove_leaves_other_plugins_untouched(self, tmp_path, monkeypatch):
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)
        # Install plugin A.
        repo_a = _write_plugin(tmp_path / "repo_a", name="aaa", skills=["alpha"])
        inst = _installer(repo_a, tmp_path)
        await inst.install("acme/aaa")
        # Install plugin B sharing the same install_dir + registry.
        repo_b = _write_plugin(tmp_path / "repo_b", name="bbb", skills=["beta"])
        inst._clone = _clone_factory(repo_b)
        await inst.install("acme/bbb")

        await inst.remove("aaa")

        install_dir = tmp_path / "skills_install"
        assert not (install_dir / "alpha").exists()
        assert (install_dir / "beta").is_dir()  # B's skill untouched
        names = [p.name for p in inst.list_plugins()]
        assert names == ["bbb"]

    async def test_remove_unknown_plugin_raises_404(self, tmp_path):
        inst = _installer(tmp_path, tmp_path)
        with pytest.raises(PluginInstallError) as exc:
            await inst.remove("ghost")
        assert exc.value.status_code == 404

    async def test_remove_invalid_name_raises_400(self, tmp_path):
        inst = _installer(tmp_path, tmp_path)
        with pytest.raises(PluginInstallError) as exc:
            await inst.remove("../etc")
        assert exc.value.status_code == 400

    async def test_remove_with_missing_skill_dir_degrades(self, tmp_path, monkeypatch):
        # One component already missing → step skipped, remove still completes
        # and drops the entry.
        inst, manager = await self._install_one(
            tmp_path,
            monkeypatch,
            skills=["alpha", "beta"],
            mcp=None,
        )
        # Manually delete one skill dir before remove.
        import shutil as _sh

        _sh.rmtree(tmp_path / "skills_install" / "alpha")

        report = await inst.remove("combo")

        alpha_step = next(s for s in report.steps if s.name == "skill:alpha")
        assert alpha_step.status == "skipped"
        beta_step = next(s for s in report.steps if s.name == "skill:beta")
        assert beta_step.status == "succeeded"
        # alpha was already gone, so it's not in removed_skills.
        assert report.removed_skills == ["beta"]
        # Entry still dropped — nothing lingers.
        assert inst.list_plugins() == []

    async def test_remove_with_unregistered_mcp_degrades(self, tmp_path, monkeypatch):
        # MCP server recorded in registry but not in the manager → skipped,
        # remove still completes.
        inst, manager = await self._install_one(
            tmp_path,
            monkeypatch,
            skills=["alpha"],
            mcp={"svc": {"command": "uvx", "args": ["svc"]}},
        )
        # Drop it from the manager so remove_server_config returns False.
        manager.known.discard("plugin:combo:svc")

        report = await inst.remove("combo")

        mcp_step = next(s for s in report.steps if s.name == "mcp:plugin:combo:svc")
        assert mcp_step.status == "skipped"
        assert report.removed_mcp_servers == []
        assert inst.list_plugins() == []

    async def test_remove_audit_logs_plugin_remove(self, tmp_path, monkeypatch):
        inst, _ = await self._install_one(tmp_path, monkeypatch, skills=["alpha"], mcp=None)
        logged: list = []
        monkeypatch.setattr(
            "pocketpaw.plugins.installer.get_audit_logger",
            lambda: type("A", (), {"log": lambda self, ev: logged.append(ev)})(),
        )
        await inst.remove("combo")
        assert any(getattr(ev, "action", None) == "plugin_remove" for ev in logged)


class TestAtomicRegistryWrite:
    async def test_sequential_writes_do_not_corrupt(self, tmp_path, monkeypatch):
        # Install two plugins back-to-back sharing one registry; the
        # temp-file + os.replace path must leave a valid, complete JSON.
        manager = _FakeMCPManager()
        _patch_loader_and_manager(monkeypatch, manager)
        repo_a = _write_plugin(tmp_path / "a", name="aaa", skills=["alpha"])
        inst = _installer(repo_a, tmp_path)
        await inst.install("acme/aaa")
        repo_b = _write_plugin(tmp_path / "b", name="bbb", skills=["beta"])
        inst._clone = _clone_factory(repo_b)
        await inst.install("acme/bbb")

        registry_path = tmp_path / "registry" / "plugins.json"
        data = json.loads(registry_path.read_text())  # parses → not corrupt
        assert set(data) == {"aaa", "bbb"}
        # No leftover temp files in the registry dir.
        leftovers = list(registry_path.parent.glob("*.tmp.*"))
        assert leftovers == []

    def test_save_registry_replaces_atomically(self, tmp_path):
        inst = _installer(tmp_path, tmp_path)
        inst._save_registry({"x": {"version": "1"}})
        registry_path = tmp_path / "registry" / "plugins.json"
        assert json.loads(registry_path.read_text()) == {"x": {"version": "1"}}
        # Overwrite with new content; old file must be cleanly replaced.
        inst._save_registry({"y": {"version": "2"}})
        assert json.loads(registry_path.read_text()) == {"y": {"version": "2"}}
        assert list(registry_path.parent.glob("*.tmp.*")) == []
