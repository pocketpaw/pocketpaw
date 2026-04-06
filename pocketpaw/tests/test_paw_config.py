# Tests for paw module PawConfig.
# Created: 2026-03-02
# Covers: load() defaults, load() from yaml, env var overrides,
#         default_soul_path, and paw_dir properties.

from __future__ import annotations

from pocketpaw.paw.config import PawConfig


class TestPawConfigDefaults:
    """PawConfig.load() with no paw.yaml present."""

    def test_load_no_yaml_returns_defaults(self, tmp_path):
        config = PawConfig.load(tmp_path)

        assert config.project_root == tmp_path
        assert config.soul_name == "Paw"
        assert config.provider == "claude"
        assert config.soul_path is None

    def test_load_uses_cwd_when_no_root_given(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = PawConfig.load()

        assert config.project_root == tmp_path

    def test_default_soul_path_uses_soul_name(self, tmp_path):
        config = PawConfig.load(tmp_path)
        expected = tmp_path / ".paw" / "paw.soul"

        assert config.default_soul_path == expected

    def test_paw_dir_is_dot_paw_under_project_root(self, tmp_path):
        config = PawConfig.load(tmp_path)

        assert config.paw_dir == tmp_path / ".paw"


class TestPawConfigFromYaml:
    """PawConfig.load() reading a valid paw.yaml."""

    def test_loads_soul_name(self, tmp_path):
        (tmp_path / "paw.yaml").write_text("soul_name: Buddy\nprovider: openai\n")

        config = PawConfig.load(tmp_path)

        assert config.soul_name == "Buddy"

    def test_loads_provider(self, tmp_path):
        (tmp_path / "paw.yaml").write_text("provider: openai\n")

        config = PawConfig.load(tmp_path)

        assert config.provider == "openai"

    def test_loads_soul_path(self, tmp_path):
        soul_file = tmp_path / "my.soul"
        (tmp_path / "paw.yaml").write_text(f"soul_path: {soul_file}\n")

        config = PawConfig.load(tmp_path)

        assert config.soul_path == soul_file

    def test_name_alias_for_soul_name(self, tmp_path):
        # Some configs use 'name' instead of 'soul_name'
        (tmp_path / "paw.yaml").write_text("name: Rex\n")

        config = PawConfig.load(tmp_path)

        assert config.soul_name == "Rex"

    def test_empty_yaml_gives_defaults(self, tmp_path):
        (tmp_path / "paw.yaml").write_text("")

        config = PawConfig.load(tmp_path)

        assert config.soul_name == "Paw"
        assert config.provider == "claude"


class TestPawConfigEnvOverrides:
    """Env vars PAW_PROVIDER and PAW_SOUL_PATH override yaml and defaults."""

    def test_paw_provider_overrides_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "paw.yaml").write_text("provider: openai\n")
        monkeypatch.setenv("PAW_PROVIDER", "ollama")

        config = PawConfig.load(tmp_path)

        assert config.provider == "ollama"

    def test_paw_provider_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAW_PROVIDER", "none")

        config = PawConfig.load(tmp_path)

        assert config.provider == "none"

    def test_paw_soul_path_overrides_yaml(self, tmp_path, monkeypatch):
        soul_from_yaml = tmp_path / "yaml.soul"
        (tmp_path / "paw.yaml").write_text(f"soul_path: {soul_from_yaml}\n")

        env_soul = tmp_path / "env.soul"
        monkeypatch.setenv("PAW_SOUL_PATH", str(env_soul))

        config = PawConfig.load(tmp_path)

        assert config.soul_path == env_soul

    def test_paw_soul_path_overrides_default(self, tmp_path, monkeypatch):
        env_soul = tmp_path / "custom.soul"
        monkeypatch.setenv("PAW_SOUL_PATH", str(env_soul))

        config = PawConfig.load(tmp_path)

        assert config.soul_path == env_soul


class TestPawConfigProperties:
    """Property correctness for various soul_name values."""

    def test_default_soul_path_lowercases_soul_name(self, tmp_path):
        config = PawConfig(project_root=tmp_path, soul_name="MyPet")

        assert config.default_soul_path == tmp_path / ".paw" / "mypet.soul"

    def test_paw_dir_does_not_create_directory(self, tmp_path):
        config = PawConfig(project_root=tmp_path)

        # The property just returns a Path — it does not mkdir
        assert not config.paw_dir.exists()
        assert config.paw_dir == tmp_path / ".paw"
