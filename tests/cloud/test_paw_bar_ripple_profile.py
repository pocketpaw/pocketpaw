# tests/cloud/test_paw_bar_ripple_profile.py — the "ripple" concierge card profile.
#
# A site whose ``concierge_ui_profile`` is "ripple" gets full-catalog Ripple cards
# (``card_spec.RIPPLE_PROFILE``: the vendored ripple-manifest.json, 400 nodes,
# depth 16, 64,000 chars, handlers checked wherever a widget keeps them), the
# Ripple cards paragraph with no catalog, actions or lead capture, and an 8,000
# token reply cap. Every other site keeps ``PAWBAR_PROFILE``. Also covered: the
# per-site daily spend cap over the global one, and both settings through the
# settings PATCH and its response. The accepted card is ripple's recorded
# explainer scenario (tests/fixtures/ripple_explainer_card.json, from ripple
# origin/main 09a56a6, packages/svelte/src/routes/live/fixtures/explainer.json,
# its chunks joined and wrapped as {ui, state}).

# ruff: noqa: F811 — pytest fixtures imported by name

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    _site as _settings_site,
)
from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    client,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "ripple_explainer_card.json"

# Refreshing ripple-manifest.json: see ee/pocketpaw_ee/paw_bar/ripple-manifest.json.source
# (download the release asset, drop the widget examples, update both hashes).
_RIPPLE_MANIFEST_SHA256 = "451f17b6ef0afbc8196cb3a3095bcf531704bab2d16e39baae419cab8986d200"


def _card() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _body(spec: dict) -> str:
    return json.dumps(spec)


def _chain(depth: int, kind: str = "flex") -> dict:
    node: dict = {"type": "text", "props": {"text": "x"}}
    for _ in range(depth - 1):
        node = {"type": kind, "props": {}, "children": [node]}
    return {"ui": node}


# --------------------------------------------------------------------------- #
# card_spec
# --------------------------------------------------------------------------- #


def test_the_vendored_ripple_manifest_has_not_drifted():
    from pocketpaw_ee.paw_bar import card_spec

    raw = card_spec.RIPPLE_MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == _RIPPLE_MANIFEST_SHA256
    assert card_spec.RIPPLE_MANIFEST["version"] == "0.8.0"
    assert len(card_spec.RIPPLE_MANIFEST["widgets"]) == 189
    assert len(card_spec.RIPPLE_PROFILE.widget_types) == 189 - len(card_spec.RIPPLE_DEFERRED)


def test_the_pawbar_profile_is_todays_rules():
    from pocketpaw_ee.paw_bar import card_spec

    p = card_spec.PAWBAR_PROFILE
    assert (p.widget_types, p.actions) == (card_spec.WIDGET_TYPES, card_spec.SPEC_ACTIONS)
    assert (p.max_nodes, p.max_depth, p.max_chars) == (80, 8, 32_000)
    assert p.detailed is None and p.strict_actions is False
    r = card_spec.RIPPLE_PROFILE
    assert r.actions == card_spec.SPEC_ACTIONS
    assert (r.max_nodes, r.max_depth, r.max_chars) == (400, 16, 64_000)


def test_a_recorded_full_catalog_card_passes_ripple_and_not_pawbar():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    body = _body(_card())
    out = render_card(body, [], profile=RIPPLE_PROFILE)
    assert out is not None
    assert json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0]) == _card()
    # Today's widget set has no card, grid, stat, slider...: dropped whole.
    assert render_card(body, []) is None


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        (
            {"ui": {"type": "flex", "children": [{"type": "text"}] * 400}},
            "401 nodes",
        ),
        (_chain(17), "depth 17"),
        (
            {"ui": {"type": "text", "props": {"text": "x" * 64_001}}},
            "over 64,000 chars",
        ),
        (
            {"ui": {"type": "button", "on_click": {"action": "emit", "target": "pay"}}},
            "emit to a non-host event",
        ),
        (
            {"ui": {"type": "button", "on_click": {"action": "toast", "message": "hi"}}},
            "an action outside SPEC_ACTIONS",
        ),
        (
            {"ui": {"type": "ripple-frame", "props": {"spec": {"ui": {"type": "text"}}}}},
            "a deferred widget",
        ),
    ],
)
def test_ripple_refuses_what_breaks_its_rules(spec, why):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    assert render_card(_body(spec), [], profile=RIPPLE_PROFILE) is None, why


def test_ripple_bounds_are_inclusive():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    wide = {"ui": {"type": "flex", "children": [{"type": "text"}] * 399}}
    assert render_card(_body(wide), [], profile=RIPPLE_PROFILE) is not None
    assert render_card(_body(_chain(16)), [], profile=RIPPLE_PROFILE) is not None
    # The same depth is past paw-bar's 8.
    assert render_card(_body(_chain(16)), []) is None


def test_ripple_allows_a_declared_host_event():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    spec = {"ui": {"type": "button", "on_click": {"action": "emit", "target": "checkout"}}}
    assert render_card(_body(spec), [], verbs=["checkout"], profile=RIPPLE_PROFILE)
    assert render_card(_body(spec), [], verbs=[], profile=RIPPLE_PROFILE) is None


@pytest.mark.parametrize(
    "node",
    [
        # A handler prop that is not on_*.
        {
            "type": "form-layout",
            "props": {"submitActions": [{"action": "api", "url": "https://x.test"}]},
        },
        # A handler inside a labelled button list.
        {
            "type": "order-status",
            "props": {"actions": [{"label": "Go", "actions": {"action": "navigate"}}]},
        },
        # A node kept in a prop, with its own on_click.
        {
            "type": "popover",
            "props": {
                "trigger": "Open",
                "content": {
                    "type": "flex",
                    "children": [{"type": "button", "on_click": {"action": "invoke_tool"}}],
                },
            },
        },
        # An emit to an undeclared event, nested.
        {
            "type": "wizard-layout",
            "props": {"finishActions": {"action": "emit", "target": "steal"}},
        },
    ],
)
def test_ripple_checks_handlers_wherever_a_widget_keeps_them(node):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    assert render_card(_body({"ui": node}), [], profile=RIPPLE_PROFILE) is None


def test_a_data_rows_own_action_field_is_not_a_handler():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    spec = {
        "ui": {
            "type": "audit-log",
            "props": {"entries": [{"action": "login", "actor": "Sam", "time": "09:00"}]},
        }
    }
    assert render_card(_body(spec), [], profile=RIPPLE_PROFILE) is not None
    ok = {"ui": {"type": "form-layout", "props": {"submitActions": {"action": "set"}}}}
    assert render_card(_body(ok), [], profile=RIPPLE_PROFILE) is not None


def test_the_ripple_listing_is_bounded_and_covers_every_widget():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, compact_manifest

    text = compact_manifest(RIPPLE_PROFILE)
    lines = text.splitlines()
    assert len(lines) == len(RIPPLE_PROFILE.widget_types)
    assert {line[2:].split(":")[0].split(" {")[0] for line in lines} == (
        RIPPLE_PROFILE.widget_types
    )
    assert len(text) < 20_000
    # The rules' widgets carry their props (each's node fields too).
    assert "- stat {label?, value" in text
    assert "- each {items, item_as?, index_as?}" in text


# --------------------------------------------------------------------------- #
# The runtime
# --------------------------------------------------------------------------- #


def _widget(actions=()):
    return SimpleNamespace(id="w1", spec=SimpleNamespace(actions=list(actions)))


def test_the_profile_comes_from_the_site():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import ui_profile

    assert ui_profile(SimpleNamespace(concierge_ui_profile="ripple")) is RIPPLE_PROFILE
    for site in (SimpleNamespace(), SimpleNamespace(concierge_ui_profile="RIPPLE"), None):
        assert ui_profile(site) is PAWBAR_PROFILE


def test_the_ripple_cards_paragraph_needs_no_catalog_or_lead_capture():
    from pocketpaw_ee.paw_bar.concierge_runtime import build_prompt

    site = SimpleNamespace(concierge_ui_profile="ripple", concierge_lead_capture=False)
    prompt = build_prompt([], _widget(), [], "split a bill", site=site)
    assert "<catalog>" in prompt
    assert "- slider {" in prompt
    assert "no exponent operator" in prompt
    assert "There is no flow, branch, toast" in prompt
    assert "product-card" not in prompt
    assert "Do not offer a send_to_team form" in prompt

    pawbar = SimpleNamespace(concierge_lead_capture=False)
    assert "<catalog>" not in build_prompt([], _widget(), [], "hi", site=pawbar)


def test_ripple_raises_the_reply_cap_and_pawbar_keeps_the_setting():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import _model_settings

    settings = SimpleNamespace(pawbar_concierge_max_tokens=1_234)
    ripple = _model_settings(settings, None, "ws", profile=RIPPLE_PROFILE)
    assert ripple["max_tokens"] == 8_000
    assert _model_settings(settings, None, "ws", profile=PAWBAR_PROFILE)["max_tokens"] == 1_234
    assert _model_settings(settings, None, "ws")["max_tokens"] == 1_234


def test_the_fence_filter_checks_cards_against_the_profile():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import FenceFilter

    reply = f"Here you go.\n```pawbar-card\n{_body(_card())}\n```"
    ripple = FenceFilter(profile=RIPPLE_PROFILE)
    out = "".join(ripple.feed(reply) + ripple.close())
    assert '"type":"stat"' in out
    pawbar = FenceFilter()
    out = "".join(pawbar.feed(reply) + pawbar.close())
    assert "pawbar-card" not in out and out.startswith("Here you go.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "global_cap", "spent", "over"),
    [
        (None, 5.0, 4.0, False),  # no site cap: the global one
        (None, 5.0, 6.0, True),
        (50.0, 5.0, 6.0, False),  # the site's cap replaces the global one
        (50.0, 5.0, 50.0, True),
        (2.0, 5.0, 3.0, True),
        (0.0, 5.0, 0.0, True),  # 0 pauses the site
        (10.0, 0.0, 11.0, True),  # a site cap holds with no global cap
    ],
)
async def test_the_sites_own_spend_cap_wins_over_the_global_one(
    monkeypatch, own, global_cap, spent, over
):
    from pocketpaw_ee.paw_bar import concierge_runtime

    async def _spent(workspace_id, pocket_id, **_):
        return spent

    monkeypatch.setattr(concierge_runtime, "site_spend_today_usd", _spent)
    settings = SimpleNamespace(pawbar_concierge_daily_spend_cap=global_cap)
    site = SimpleNamespace(concierge_daily_spend_cap=own)
    assert await concierge_runtime._over_spend_cap(settings, "ws", "p", site) is over


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_default_to_pawbar_and_the_global_cap(client):
    c, _store = client
    site = await _settings_site()

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/settings")).json()

    assert body["concierge_ui_profile"] == "pawbar"
    assert body["concierge_daily_spend_cap"] is None


@pytest.mark.asyncio
async def test_settings_patch_round_trips_the_profile_and_the_cap(client):
    from pocketpaw_ee.cloud.models.site import Site

    c, _store = client
    site = await _settings_site()
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(
        url, json={"concierge_ui_profile": "ripple", "concierge_daily_spend_cap": 25}
    )
    assert res.status_code == 200, res.text
    assert res.json()["concierge_ui_profile"] == "ripple"
    assert res.json()["concierge_daily_spend_cap"] == 25
    stored = await Site.get(site.id)
    assert (stored.concierge_ui_profile, stored.concierge_daily_spend_cap) == ("ripple", 25)

    # Another field's PATCH leaves both alone; null clears the cap only.
    await c.patch(url, json={"concierge_greeting": "Hi"})
    body = (await c.get(url)).json()
    assert (body["concierge_ui_profile"], body["concierge_daily_spend_cap"]) == ("ripple", 25)
    res = await c.patch(url, json={"concierge_daily_spend_cap": None, "concierge_ui_profile": None})
    assert res.json()["concierge_daily_spend_cap"] is None
    assert res.json()["concierge_ui_profile"] == "ripple"
    res = await c.patch(
        url, json={"concierge_ui_profile": "pawbar", "concierge_daily_spend_cap": 0}
    )
    assert (res.json()["concierge_ui_profile"], res.json()["concierge_daily_spend_cap"]) == (
        "pawbar",
        0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"concierge_ui_profile": "react"},
        {"concierge_ui_profile": ""},
        {"concierge_daily_spend_cap": -1},
        {"concierge_daily_spend_cap": 500.01},
        {"concierge_daily_spend_cap": "lots"},
    ],
)
async def test_settings_patch_rejects_a_bad_profile_or_cap(client, patch):
    c, _store = client
    site = await _settings_site()
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(url, json=patch)

    assert res.status_code == 422
    body = (await c.get(url)).json()
    assert (body["concierge_ui_profile"], body["concierge_daily_spend_cap"]) == ("pawbar", None)
