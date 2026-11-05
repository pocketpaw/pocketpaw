# tests/ee/sites/test_figma_tokens.py: token derivation from a Figma frame.
#
# Pins: fills / text / strokes / radii / gaps / font sizes become roles with the
# right winner; Enterprise variables win over derived values and follow aliases;
# every role is present, observed or substituted; the CSS blocks carry only
# observed values and a hostile font name cannot break out of them; and the
# ``figma_tokens`` tool asks for variables only when the grant has the scope.
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

KEY = "AbCdEf1234567890"


def _rgb(hex_value: str) -> dict:
    return {
        "r": int(hex_value[1:3], 16) / 255,
        "g": int(hex_value[3:5], 16) / 255,
        "b": int(hex_value[5:7], 16) / 255,
        "a": 1,
    }


def _box(w: float, h: float) -> dict:
    return {"x": 0, "y": 0, "width": w, "height": h}


def _text(text: str, colour: str, size: float, family: str = "Inter") -> dict:
    return {
        "type": "TEXT",
        "name": "t",
        "characters": text,
        "fills": [{"type": "SOLID", "color": _rgb(colour)}],
        "style": {"fontFamily": family, "fontSize": size},
        "absoluteBoundingBox": _box(100, size),
    }


def _frame() -> dict:
    return {
        "nodes": {
            "1:2": {
                "document": {
                    "id": "1:2",
                    "type": "FRAME",
                    "name": "Home",
                    "absoluteBoundingBox": _box(1440, 3000),
                    "fills": [{"type": "SOLID", "color": _rgb("#fafaf7")}],
                    "itemSpacing": 48,
                    "paddingTop": 96,
                    "children": [
                        {
                            "type": "RECTANGLE",
                            "name": "card",
                            "cornerRadius": 16,
                            "absoluteBoundingBox": _box(600, 400),
                            "fills": [{"type": "SOLID", "color": _rgb("#ffffff")}],
                            "strokes": [{"type": "SOLID", "color": _rgb("#e4e4e0")}],
                            "effects": [
                                {
                                    "type": "DROP_SHADOW",
                                    "offset": {"x": 0, "y": 4},
                                    "radius": 12,
                                    "color": {"r": 0, "g": 0, "b": 0, "a": 0.1},
                                }
                            ],
                        },
                        {
                            "type": "FRAME",
                            "name": "button",
                            "cornerRadius": 8,
                            "itemSpacing": 8,
                            "absoluteBoundingBox": _box(160, 48),
                            "fills": [{"type": "SOLID", "color": _rgb("#e5482d")}],
                        },
                        _text("Ship faster", "#111111", 72, "Fraunces"),
                        _text("Body copy " * 40, "#111111", 18),
                        _text("Fine print here", "#6b6b6b", 14),
                    ],
                }
            }
        }
    }


def test_nodes_become_roles():
    from pocketpaw_ee.sites.figma.tokens import derive

    tokens = derive(_frame())
    value = {role: t["value"] for role, t in tokens.items()}
    assert value["color.canvas"] == "#fafaf7"
    assert value["color.surface"] == "#ffffff"
    assert value["color.ink"] == "#111111"
    assert value["color.muted"] == "#6b6b6b"
    assert value["color.accent"] == "#e5482d"
    assert value["color.border"] == "#e4e4e0"
    assert value["radius"] == {"sm": 8, "md": 16}
    assert value["space"] == [8, 48, 96]
    assert value["type"] == {"display": 72, "body": 18, "small": 14}
    assert value["font.display"] == "Fraunces"
    assert value["font.body"] == "Inter"
    assert value["shadow"].startswith("0px 4px 12px 0px #000000")
    assert all(t["status"] == "observed" for t in tokens.values())
    assert tokens["color.ink"]["contrast_on_canvas"] > 15


def test_missing_roles_are_substituted():
    from pocketpaw_ee.sites.figma.tokens import derive

    body = {"nodes": {"1:2": {"document": {"type": "FRAME", "name": "Empty"}}}}
    tokens = derive(body)
    assert set(tokens) >= {"color.canvas", "color.accent", "radius", "type", "font.body"}
    assert all(
        t == {"value": None, "status": "substituted", "source": t["source"]}
        for t in tokens.values()
    )


def test_variables_win_and_follow_aliases():
    from pocketpaw_ee.sites.figma.tokens import derive

    variables = {
        "meta": {
            "variableCollections": {"c1": {"defaultModeId": "m1"}},
            "variables": {
                "v1": {
                    "name": "Brand/Primary",
                    "resolvedType": "COLOR",
                    "variableCollectionId": "c1",
                    "valuesByMode": {"m1": {"type": "VARIABLE_ALIAS", "id": "v2"}},
                },
                "v2": {
                    "name": "Palette/Blue 600",
                    "resolvedType": "COLOR",
                    "variableCollectionId": "c1",
                    "valuesByMode": {"m1": _rgb("#2050e0"), "m2": _rgb("#000000")},
                },
                "v3": {
                    "name": "Radius/Large",
                    "resolvedType": "FLOAT",
                    "variableCollectionId": "c1",
                    "valuesByMode": {"m1": 20},
                },
            },
        }
    }
    tokens = derive(_frame(), variables)
    assert tokens["color.accent"]["value"] == "#2050e0"
    assert tokens["color.accent"]["source"].startswith("variable Brand/Primary")
    assert tokens["radius"]["value"] == {"sm": 20}
    assert tokens["color.canvas"]["value"] == "#fafaf7"  # not in variables: derived


def test_css_blocks_hold_only_observed_safe_values():
    from pocketpaw_ee.sites.figma.tokens import css, derive

    frame = _frame()
    kids = frame["nodes"]["1:2"]["document"]["children"]
    kids.append(_text("x" * 5000, "#111111", 18, 'Evil"; } body { display:none } /*'))
    out = css(derive(frame))
    assert out["root"].startswith(":root {") and "--color-accent: #e5482d;" in out["root"]
    assert '--font-display: "Fraunces", ' in out["theme"]
    assert "display:none" not in out["root"] + out["theme"]
    assert out["root"].count("}") == 1 and out["theme"].count("}") == 1
    empty = css(derive({"nodes": {}}))
    assert empty["root"] == ":root {\n\n}"


# ------------------------------------------------------------------- tool


class _Source:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def file_meta(self, key):
        return {"name": "Site", "last_touched_at": "t"}

    async def nodes(self, key, ids, depth):
        self.calls.append("nodes")
        return _frame()

    async def variables(self, key):
        self.calls.append("variables")
        return {"meta": {"variables": {}, "variableCollections": {}}}


@pytest.fixture
def tool(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import figma as mcp
    from pocketpaw_ee.sites.figma import session_keys

    session_keys.clear_memory()
    source = _Source()
    conn = SimpleNamespace(meta={}, scopes=[])
    monkeypatch.setattr(mcp, "source_for", AsyncMock(return_value=(source, conn)))
    with (
        patch("pocketpaw_ee.sites.figma.session_keys._redis", return_value=None),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id", return_value="s"),
        patch(
            "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
            new=AsyncMock(return_value="go"),
        ),
    ):
        yield SimpleNamespace(mcp=mcp, source=source, conn=conn)
    session_keys.clear_memory()


URL = f"https://www.figma.com/design/{KEY}/Site?node-id=1-2"


async def test_tokens_tool_reads_variables_only_with_the_scope(tool):
    from pocketpaw_ee.sites.figma import session_keys

    await session_keys.note_message("w", "s", "u", URL)
    out = await tool.mcp._tokens_handler({"url": URL})
    body = json.loads(out["content"][0]["text"])
    assert body["ok"] is True and body["source"] == "derived"
    assert tool.source.calls == ["nodes"]
    assert body["tokens"]["color.accent"]["status"] == "observed"
    assert ":root {" in body["css"]["root"]
    tool.conn.scopes = ["file_content:read", "file_variables:read"]
    out = await tool.mcp._tokens_handler({"url": URL})
    assert json.loads(out["content"][0]["text"])["source"] == "variables+derived"
    assert tool.source.calls == ["nodes", "nodes", "variables"]


async def test_tokens_tool_refuses_an_unpasted_file(tool):
    out = await tool.mcp._tokens_handler({"url": URL})
    assert "figma_not_pasted" in out["content"][0]["text"]
    assert tool.source.calls == []
