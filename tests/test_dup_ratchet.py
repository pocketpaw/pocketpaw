# Created 2026-10-01 (canonicalization Wave 0, CN-1): runs scripts/dup-ratchet
# against the tree (fails only on growth), proves the ratchet trips on a new
# copy, and pins the "OSS core may not import from EE" ignore list so it can
# only shrink.
from __future__ import annotations

import importlib.util
import tomllib
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_loader = SourceFileLoader("dup_ratchet", str(REPO / "scripts" / "dup-ratchet"))
ratchet = importlib.util.module_from_spec(importlib.util.spec_from_loader("dup_ratchet", _loader))
_loader.exec_module(ratchet)

# Today's size of the OSS->EE ignore list. Lower it when you remove an entry.
OSS_EE_IGNORE_MAX = 40


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
    assert len(contract["ignore_imports"]) <= OSS_EE_IGNORE_MAX, (
        "new OSS->EE import exemption — reach EE through the extension registry instead"
    )
