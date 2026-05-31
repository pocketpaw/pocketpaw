from pathlib import Path

import pytest
from pocketpaw_ee.cloud.files.dto import MountConfig
from pocketpaw_ee.cloud.files.mounts_config import load_mounts, resolve_template


def test_load_mounts_returns_ordered_list(tmp_path: Path):
    yaml = tmp_path / "mounts.yaml"
    yaml.write_text(
        "- provider_id: a\n  mount_template: /A\n  writable: false\n  order: 20\n"
        "- provider_id: b\n  mount_template: /B\n  writable: true\n  order: 10\n"
    )
    cfg = load_mounts(yaml)
    assert [m.provider_id for m in cfg] == ["b", "a"]
    assert all(isinstance(m, MountConfig) for m in cfg)


def test_resolve_template_substitutes_vars():
    assert (
        resolve_template("/Workspaces/{workspace_id}/KB", {"workspace_id": "ws_1"})
        == "/Workspaces/ws_1/KB"
    )


def test_resolve_template_leaves_unknown_vars_as_error():
    with pytest.raises(KeyError):
        resolve_template("/Workspaces/{workspace_id}/KB", {})


def test_resolve_template_no_vars():
    assert resolve_template("/My Files", {}) == "/My Files"


def test_load_mounts_rejects_relative_template(tmp_path: Path):
    yaml = tmp_path / "mounts.yaml"
    yaml.write_text(
        "- provider_id: a\n  mount_template: relative/path\n  writable: false\n  order: 1\n"
    )
    with pytest.raises(ValueError):
        load_mounts(yaml)
