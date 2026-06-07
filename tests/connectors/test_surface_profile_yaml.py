# tests/connectors/test_surface_profile_yaml.py
# Created: 2026-06-07 (M3 connector→skill auto-authoring) — pins the optional
#   ``surface_profile:`` block on a connector YAML: a connector WITH a block
#   exposes a parsed ``ConnectorSurfaceProfile`` on its ``ConnectorDef``; one
#   WITHOUT a block parses fine (``surface_profile is None``). Also asserts the
#   shipped gmail.yaml carries ``skill: gmail`` so the Gmail proof wires through.

from __future__ import annotations

from pathlib import Path

from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.yaml_engine import (
    ConnectorSurfaceProfile,
    parse_connector_yaml,
)


def _write_yaml(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(body)
    return p


def test_connector_with_surface_profile_block_parses(tmp_path: Path) -> None:
    """A connector YAML with a surface_profile block exposes it on the def."""
    path = _write_yaml(
        tmp_path,
        "acme",
        """
name: acme
display_name: Acme
surface_profile:
  skill: acme-skill
  allow_tools: ["mcp__*acme*", "Bash"]
  deny_tools: ["mcp__danger"]
""",
    )
    defn = parse_connector_yaml(path)
    assert isinstance(defn.surface_profile, ConnectorSurfaceProfile)
    assert defn.surface_profile.skill == "acme-skill"
    assert defn.surface_profile.allow_tools == ("mcp__*acme*", "Bash")
    assert defn.surface_profile.deny_tools == ("mcp__danger",)


def test_connector_without_block_parses_none(tmp_path: Path) -> None:
    """A connector YAML with NO surface_profile block parses fine (None)."""
    path = _write_yaml(
        tmp_path,
        "plain",
        """
name: plain
display_name: Plain
actions:
  - name: ping
    method: GET
""",
    )
    defn = parse_connector_yaml(path)
    assert defn.surface_profile is None


def test_empty_block_yields_none(tmp_path: Path) -> None:
    """A surface_profile block with no meaningful keys yields None, not a stub."""
    path = _write_yaml(
        tmp_path,
        "blank",
        """
name: blank
display_name: Blank
surface_profile:
  allow_tools: []
  deny_tools: []
""",
    )
    defn = parse_connector_yaml(path)
    assert defn.surface_profile is None


def test_skill_only_block_parses(tmp_path: Path) -> None:
    """The common shape — skill only, no tool patterns — parses with empty tuples."""
    path = _write_yaml(
        tmp_path,
        "skillonly",
        """
name: skillonly
display_name: Skill Only
surface_profile:
  skill: gmail
""",
    )
    defn = parse_connector_yaml(path)
    assert defn.surface_profile is not None
    assert defn.surface_profile.skill == "gmail"
    assert defn.surface_profile.allow_tools == ()
    assert defn.surface_profile.deny_tools == ()


def test_shipped_gmail_yaml_carries_skill() -> None:
    """The real gmail.yaml in /connectors maps to the bundled gmail skill."""
    reg = ConnectorRegistry()
    gmail = reg.get_definition("gmail")
    assert gmail is not None
    assert gmail.surface_profile is not None
    assert gmail.surface_profile.skill == "gmail"
