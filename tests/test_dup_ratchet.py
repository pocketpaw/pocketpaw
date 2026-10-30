# Created 2026-10-01 (canonicalization Wave 0, CN-1): runs scripts/dup-ratchet
# against the tree (fails only on growth), proves the ratchet trips on a new
# copy, and pins the "OSS core may not import from EE" ignore list so it can
# only shrink.
# Updated 2026-10-01: the ignore list is frozen as a set (swapping an entry
# fails too, not just growing the count).
from __future__ import annotations

import importlib.util
import tomllib
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_loader = SourceFileLoader("dup_ratchet", str(REPO / "scripts" / "dup-ratchet"))
ratchet = importlib.util.module_from_spec(importlib.util.spec_from_loader("dup_ratchet", _loader))
_loader.exec_module(ratchet)

# Today's OSS->EE ignore entries. Remove an entry here when you remove it from
# pyproject.toml; never add one.
OSS_EE_IGNORE_FROZEN = frozenset(
    {
        "pocketpaw.tools.cli -> pocketpaw_ee.cloud",
        "pocketpaw.tools.cli -> pocketpaw_ee.cloud.shared.db",
        "pocketpaw.tools.cli -> pocketpaw_ee.cloud.pockets.service",
        "pocketpaw.tools.cli -> pocketpaw_ee.cloud.pockets.agent_context",
        "pocketpaw.tools.cli -> pocketpaw_ee.agent.pocket_specialist.cli_tool",
        "pocketpaw.dashboard_lifecycle -> pocketpaw_ee.cloud.chat.runs.executor",
        "pocketpaw.dashboard_lifecycle -> pocketpaw_ee.cloud._core.request_log",
        "pocketpaw.api.v1.cloud_projects -> pocketpaw_ee.cloud.daytona.client",
        "pocketpaw.api.v1.cloud_projects -> pocketpaw_ee.cloud.daytona.store",
        "pocketpaw.atlas.fabric -> pocketpaw_ee.fabric",
        "pocketpaw.atlas.overlay -> pocketpaw_ee.agent.atlas_provider",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud.chat.agent_service",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud.uploads.mongo_store",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud.uploads.paths",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud.uploads.folder_store",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud._core.context",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud._core.errors",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud._core.realtime.emit",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud._core.realtime.events",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud.file_versions.service",
        "pocketpaw.tools.builtin.library_verbs -> pocketpaw_ee.cloud.agents.knowledge",
        "pocketpaw.tools.builtin.edit_document -> pocketpaw_ee.cloud.chat.agent_service",
        "pocketpaw.tools.builtin.edit_document -> pocketpaw_ee.cloud._core.context",
        "pocketpaw.tools.builtin.edit_document -> pocketpaw_ee.cloud._core.errors",
        "pocketpaw.tools.builtin.edit_document -> pocketpaw_ee.cloud.file_versions.service",
        "pocketpaw.tools.builtin.edit_document -> pocketpaw_ee.cloud.file_versions.dto",
        "pocketpaw.tools.builtin.edit_slides -> pocketpaw_ee.cloud.chat.agent_service",
        "pocketpaw.tools.builtin.edit_slides -> pocketpaw_ee.cloud._core.context",
        "pocketpaw.tools.builtin.edit_slides -> pocketpaw_ee.cloud._core.errors",
        "pocketpaw.tools.builtin.edit_slides -> pocketpaw_ee.cloud.file_versions.service",
        "pocketpaw.tools.builtin.edit_slides -> pocketpaw_ee.cloud.file_versions.dto",
        "pocketpaw.tools.builtin.edit_spreadsheet -> pocketpaw_ee.cloud.chat.agent_service",
        "pocketpaw.tools.builtin.edit_spreadsheet -> pocketpaw_ee.cloud._core.context",
        "pocketpaw.tools.builtin.edit_spreadsheet -> pocketpaw_ee.cloud._core.errors",
        "pocketpaw.tools.builtin.edit_spreadsheet -> pocketpaw_ee.cloud.file_versions.service",
        "pocketpaw.tools.builtin.edit_spreadsheet -> pocketpaw_ee.cloud.file_versions.dto",
        "pocketpaw.tools.builtin.studio_flow_tool -> pocketpaw_ee.cloud.chat.agent_service",
        "pocketpaw.tools.builtin.studio_flow_tool -> pocketpaw_ee.cloud.studio.schemas",
        "pocketpaw.tools.builtin.studio_flow_tool -> pocketpaw_ee.cloud.studio.service",
        "pocketpaw.agents.spend_attribution -> pocketpaw_ee.cloud.chat.agent_service",
    }
)


def test_no_new_duplicate_primitives():
    assert ratchet.main(REPO) == []


def test_ratchet_fails_on_growth(tmp_path, monkeypatch):
    mod = tmp_path / "src" / "pocketpaw" / "x.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("def try_spend():\n    pass\n")
    row = ("daily try_spend counter", r"def try_spend", ("*",), (), 0, "cloud/metering")
    monkeypatch.setattr(ratchet, "ROWS", [row])
    (msg,) = ratchet.main(tmp_path)
    assert "new copy of daily try_spend counter — use cloud/metering" in msg
    assert "if you consolidated copies, lower max_count" in msg


def test_oss_ee_boundary_ignore_list_only_shrinks():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    (contract,) = [
        c
        for c in data["tool"]["importlinter"]["contracts"]
        if c["name"] == "OSS core may not import from EE"
    ]
    new = set(contract["ignore_imports"]) - OSS_EE_IGNORE_FROZEN
    assert not new, (
        f"new OSS->EE import exemption {sorted(new)} — reach EE through the extension "
        "registry instead"
    )
