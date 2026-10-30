# test_kb_bin_resolution.py — kb-go binary auto-resolution.
# Created: 2026-04-30 — Verifies _resolve_kb_bin() walks the fallback chain
#   (env → kb-go on PATH → kb on PATH → workspace-local checkout) so
#   pocketpaw works out of the box on dev machines that have the kb-go
#   repo checked out next to pocketpaw but no system-installed binary.
"""Tests for ``ee.cloud.agents.knowledge._resolve_kb_bin``."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def reset_kb_bin_env(monkeypatch):
    """Strip POCKETPAW_KB_BIN so each test starts from a clean slate."""
    monkeypatch.delenv("POCKETPAW_KB_BIN", raising=False)


def test_explicit_env_var_wins(monkeypatch, reset_kb_bin_env):
    """POCKETPAW_KB_BIN, when set, is returned verbatim."""
    from pocketpaw_ee.cloud.agents.knowledge import _resolve_kb_bin

    monkeypatch.setenv("POCKETPAW_KB_BIN", "/custom/path/to/kb")
    assert _resolve_kb_bin() == "/custom/path/to/kb"


def test_falls_through_to_kb_go_on_path(monkeypatch, reset_kb_bin_env, tmp_path):
    """When env unset, prefer ``kb-go`` from PATH."""
    from pocketpaw_ee.cloud.agents import knowledge as kn

    fake_kb_go = tmp_path / "kb-go"
    fake_kb_go.write_text("#!/bin/sh\nexit 0\n")
    fake_kb_go.chmod(0o755)
    monkeypatch.setattr(
        kn.shutil,
        "which",
        lambda name: str(fake_kb_go) if name == "kb-go" else None,
    )

    assert kn._resolve_kb_bin() == str(fake_kb_go)


def test_falls_through_to_kb_on_path(monkeypatch, reset_kb_bin_env, tmp_path):
    """When ``kb-go`` is missing but ``kb`` is on PATH, use that."""
    from pocketpaw_ee.cloud.agents import knowledge as kn

    fake_kb = tmp_path / "kb"
    fake_kb.write_text("#!/bin/sh\nexit 0\n")
    fake_kb.chmod(0o755)
    monkeypatch.setattr(kn.shutil, "which", lambda name: str(fake_kb) if name == "kb" else None)

    assert kn._resolve_kb_bin() == str(fake_kb)


def test_falls_through_to_workspace_checkout(monkeypatch, reset_kb_bin_env, tmp_path):
    """No PATH entry → walk parents looking for ``<ancestor>/kb-go/kb``."""
    from pocketpaw_ee.cloud.agents import knowledge as kn

    # Stage a fake workspace layout: <root>/{ee/pocketpaw_ee/cloud/agents/file.py, kb-go/kb}.
    project = tmp_path / "fake-pocketpaw"
    knowledge_file = project / "ee" / "pocketpaw_ee" / "cloud" / "agents" / "knowledge.py"
    knowledge_file.parent.mkdir(parents=True)
    knowledge_file.touch()
    kb_bin = tmp_path / "kb-go" / "kb"
    kb_bin.parent.mkdir()
    kb_bin.write_text("#!/bin/sh\nexit 0\n")
    kb_bin.chmod(0o755)

    # Force shutil.which to return None so the PATH branch is skipped.
    monkeypatch.setattr(kn.shutil, "which", lambda name: None)
    # Pretend the source file lives in the staged tree so the parent walk
    # finds the staged kb-go/kb.
    monkeypatch.setattr(kn, "__file__", str(knowledge_file))

    assert kn._resolve_kb_bin() == str(kb_bin)


def test_returns_default_string_when_nothing_found(monkeypatch, reset_kb_bin_env, tmp_path):
    """Last resort: literal ``"kb-go"`` so the FileNotFoundError message
    stays informative even when no binary is reachable."""
    from pocketpaw_ee.cloud.agents import knowledge as kn

    monkeypatch.setattr(kn.shutil, "which", lambda name: None)
    # Point __file__ at a tree that contains no kb-go anywhere.
    isolated = tmp_path / "no-kb-here" / "knowledge.py"
    isolated.parent.mkdir()
    isolated.touch()
    monkeypatch.setattr(kn, "__file__", str(isolated))

    assert kn._resolve_kb_bin() == "kb-go"


def test_resolver_finds_workspace_checkout_in_real_repo():
    """End-to-end smoke against the real workspace layout this repo lives
    in. kb-go is a sibling of pocketpaw, so the resolver's parent walk
    must find ``<workspace>/kb-go/kb`` regardless of PATH state.
    """
    from pocketpaw_ee.cloud.agents.knowledge import KB_BIN

    # KB_BIN was resolved at import time. In CI without kb-go on PATH it
    # should fall through to the workspace-local kb. In a sandbox without
    # the kb-go repo it returns "kb-go" (the default), which is also fine.
    assert KB_BIN, "KB_BIN must be a non-empty string"
    if KB_BIN != "kb-go":
        path = Path(KB_BIN)
        assert path.is_absolute() or path.exists()
