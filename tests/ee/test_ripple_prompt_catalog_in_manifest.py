# tests/ee/test_ripple_prompt_catalog_in_manifest.py — the chat prompt's widget
# vocabulary matches the Ripple manifest, and the chat card quality log line.
#
# The manifest is the vendored copy at ee/pocketpaw_ee/paw_bar/ripple-manifest.json
# (same widget set as the published @ripple-ui/svelte manifest the backend fetches),
# read by path so the OSS prompt modules stay free of ee imports. The chat prompt's
# catalog is the shared WIDGET_CATALOG plus the chat-only _INLINE_MORE_WIDGETS
# block. Guards both directions: a name in either catalog or intent map that the
# manifest lacks renders as nothing, and a manifest widget missing from both is one
# the chat agent never learns exists.

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_MANIFEST = (
    Path(__file__).resolve().parents[2] / "ee" / "pocketpaw_ee" / "paw_bar" / "ripple-manifest.json"
)


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _manifest_types() -> set[str]:
    return {w["type"] for w in _manifest()["widgets"]}


def _catalog_names(block: str) -> set[str]:
    """Widget names from a `category  a, b,` block (the atlas catalog format)."""
    names: set[str] = set()
    category = ""
    for line in block.splitlines():
        match = re.match(r"^([a-z][a-z-]*)\s{2,}(\S.*)$", line)
        if match:
            category, rest = match.group(1), match.group(2)
        elif line.startswith(" ") and category and "→" not in line:
            rest = line.strip()
        else:
            category = "" if not line.startswith(" ") else category
            continue
        if category != "control":
            names |= {t.strip() for t in rest.split(",") if t.strip()}
    return names


def _intent_targets(block: str) -> set[str]:
    targets = set()
    for line in block.splitlines():
        if "→" not in line:
            continue
        rhs = line.split("→", 1)[1].split()
        token = rhs[0].strip("`") if rhs else ""
        if re.fullmatch(r"[a-z][a-z0-9-]*", token):
            targets.add(token)
    return targets


def _prompt_catalog() -> set[str]:
    from pocketpaw.ripple._design import WIDGET_CATALOG
    from pocketpaw.ripple._inline import _INLINE_MORE_WIDGETS

    more = _INLINE_MORE_WIDGETS.split("## Data widgets")[0]
    return _catalog_names(WIDGET_CATALOG) | _catalog_names(more)


def test_shared_catalog_parse_matches_atlas():
    """The parser here reads WIDGET_CATALOG the way atlas does."""
    from pocketpaw.atlas.compile import _parse_widget_catalog
    from pocketpaw.ripple._design import WIDGET_CATALOG

    assert _catalog_names(WIDGET_CATALOG) == set(_parse_widget_catalog())


def test_every_prompt_catalog_widget_exists_in_the_manifest():
    unknown = sorted(_prompt_catalog() - _manifest_types())
    assert not unknown, f"the chat prompt catalog names widgets the manifest lacks: {unknown}"


def test_every_manifest_widget_is_in_the_prompt_catalog():
    control = {w["type"] for w in _manifest()["widgets"] if w.get("category") == "control"}
    missing = sorted(_manifest_types() - control - _prompt_catalog())
    assert not missing, f"manifest widgets the chat agent is never told about: {missing}"


def test_every_intent_map_target_exists_in_the_manifest():
    from pocketpaw.ripple._design import USE_THE_WIDGET_RULE
    from pocketpaw.ripple._inline import _INLINE_MORE_WIDGETS

    targets = _intent_targets(USE_THE_WIDGET_RULE) | _intent_targets(_INLINE_MORE_WIDGETS)
    assert len(targets) > 50
    unknown = sorted(targets - _manifest_types())
    assert not unknown, f"the intent map points at widgets the manifest lacks: {unknown}"


def test_chat_prompt_carries_the_more_widgets_block():
    from pocketpaw.ripple import INLINE_RIPPLE_SYSTEM_PROMPT
    from pocketpaw.ripple._inline import _INLINE_MORE_WIDGETS

    assert _INLINE_MORE_WIDGETS in INLINE_RIPPLE_SYSTEM_PROMPT


def test_inline_prompt_names_only_handled_emit_channels():
    from pocketpaw.ripple import INLINE_RIPPLE_SYSTEM_PROMPT as p

    for dropped in ("chat.suggest", "tool.invoke", "nav.open"):
        assert dropped not in p, f"{dropped} is dropped by the chat host"
    assert "ONE typed widget is the answer most of the time" not in p
    assert "# EXPRESSIONS (verified against the engine; follow exactly)" in p
    assert "15 to 35 nodes" in p
    assert '"each" takes items at node level, never inside props' in p


# ---------------------------------------------------------------------------
# log_chat_spec_quality — log-only, never blocks or changes the card
# ---------------------------------------------------------------------------

_SPEC = {
    "version": "1.0",
    "ui": {
        "type": "flex",
        "props": {"direction": "column"},
        "children": [
            {"type": "stat", "props": {"label": "Each pays", "valu": 3}},
            # `description` is a known timeline item alias (-> `detail`): the
            # log-only check must report it without rewriting the card.
            {
                "type": "timeline",
                "props": {"events": [{"date": "Jan", "title": "A", "description": "x"}]},
            },
        ],
    },
}


@pytest.mark.asyncio
async def test_chat_spec_quality_logs_counts_without_changing_the_spec(caplog):
    from pocketpaw_ee.cloud import ripple_validator

    spec = copy.deepcopy(_SPEC)
    with (
        patch.object(ripple_validator, "get_manifest", AsyncMock(return_value=_manifest())),
        caplog.at_level(logging.INFO, logger=ripple_validator.log.name),
    ):
        await ripple_validator.log_chat_spec_quality(spec, run_id="run-1")

    assert spec == _SPEC
    records = [r for r in caplog.records if r.getMessage() == "ripple_chat_spec.quality"]
    assert len(records) == 1
    rec = records[0]
    assert rec.run_id == "run-1"
    assert rec.node_count == 3
    assert rec.widget_types == ["flex", "stat", "timeline"]
    assert "stat.valu" in rec.unknown_props
    assert "stat.value" in rec.missing_required
    assert rec.missing_required_count >= 1
    assert rec.item_alias_count == 1


@pytest.mark.asyncio
async def test_chat_spec_quality_skips_quietly_without_a_manifest(caplog):
    from pocketpaw_ee.cloud import ripple_validator

    with (
        patch.object(ripple_validator, "get_manifest", AsyncMock(return_value=None)),
        caplog.at_level(logging.INFO, logger=ripple_validator.log.name),
    ):
        await ripple_validator.log_chat_spec_quality(copy.deepcopy(_SPEC), run_id="run-2")
    assert not [r for r in caplog.records if r.getMessage() == "ripple_chat_spec.quality"]


@pytest.mark.asyncio
async def test_chat_spec_quality_never_raises():
    from pocketpaw_ee.cloud import ripple_validator

    with patch.object(ripple_validator, "get_manifest", AsyncMock(side_effect=RuntimeError("x"))):
        await ripple_validator.log_chat_spec_quality(copy.deepcopy(_SPEC), run_id="run-3")
