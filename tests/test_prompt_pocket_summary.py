# tests/test_prompt_pocket_summary.py
# Created: 2026-08-03 (PA-8a, feat/prompt-bulk-retrieval) — the bound on
# ``channel.current_pocket`` and the setting that takes bulk widget detail out
# of the prompt entirely.
#
# WHAT THIS FILE IS ABOUT. The block carried an unbounded ``json.dumps`` of the
# client's widget summary near its TOP, and the SCOPE rules plus the
# ``get_pocket`` instruction at its BOTTOM. Measured on the real builder at the
# default 32,000-char budget, a 300-widget pocket rendered it at ~41,000 chars —
# larger than the entire budget — so ``_fit_to_budget`` dropped it whole and the
# agent lost the pocket id AND the standing order to fetch. The pockets big
# enough to need a fetch were the pockets told there wasn't one.
#
# WHY NOT A ``max_chars``, since that is the obvious fix and the one this file
# actively argues against: the assembler's cap truncates the TAIL. On this block
# the tail is the instruction, so a cap turns a missing block into a corrupt one
# with half a JSON array in it. ``test_capping_the_rendered_block_would_break_the_json``
# demonstrates that failure directly rather than asserting it in prose.
#
# The byte-identity half of the acceptance lives in
# ``tests/test_channel_prompt_goldens.py`` (six full-prompt shapes) and is
# duplicated at the LAYER level here by
# ``test_the_flag_off_block_is_the_shipped_bytes``, which hard-codes the
# pre-PA-8a text rather than comparing the implementation with itself.

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from pocketpaw.bootstrap.context_builder import _DEFAULT_BUDGET_CHARS
from pocketpaw.config import Settings
from pocketpaw.prompt import assemble, prompt_layer_registry
from pocketpaw.prompt.channel import ChannelInputs
from pocketpaw.prompt.channel.request import (
    _POCKET_NAME_MAX_CHARS,
    _WIDGET_SUMMARY_MAX_CHARS,
    ChannelCurrentPocketLayer,
    _bounded_widget_summary,
)
from pocketpaw.prompt.layer import PromptContext

pytestmark = pytest.mark.asyncio

_LAYER = ChannelCurrentPocketLayer()


def _ctx(pocket_context: dict | None) -> PromptContext:
    return PromptContext(
        instance=None,
        agent_id="",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        channel_inputs=ChannelInputs(metadata={"pocket_context": pocket_context}),
    )


def _widgets(count: int) -> list[dict]:
    """The shape the measurement was taken on — a real pocket's widget rows."""
    return [
        {
            "id": f"w-{i:03d}",
            "name": f"Widget {i}",
            "type": "chart",
            "title": f"Some widget title {i}",
            "props": {"source": "api", "refresh": 30},
        }
        for i in range(count)
    ]


def _pocket(count: int, **overrides) -> dict:
    pc = {"id": "pk-123", "name": "Launch Tracker", "widgets": _widgets(count)}
    pc.update(overrides)
    return pc


def _summary_line(block: str, key: str) -> str:
    """The value of a ``key: value`` line in the block, or ``""`` if absent."""
    for line in block.splitlines():
        if line.startswith(f"{key}: "):
            return line[len(key) + 2 :]
    return ""


def _flag_on(monkeypatch, value: bool = True) -> None:
    """Flip ``prompt_pocket_summary_only`` with a REAL bool, not a mock attribute."""
    settings = MagicMock()
    settings.prompt_pocket_summary_only = value
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda *a, **k: settings)


# ---------------------------------------------------------------------------
# The failure PA-8a exists to close
# ---------------------------------------------------------------------------


async def test_a_300_widget_pocket_no_longer_loses_its_whole_block_to_the_budget():
    """The headline. A big pocket keeps its id and its fetch instruction.

    Assembled at ``_DEFAULT_BUDGET_CHARS`` — the budget the channel path
    actually passes — which is the condition under which the unbounded block
    was dropped whole.

    MUTATION: set ``_WIDGET_SUMMARY_MAX_CHARS`` to ``10**9`` in
    ``prompt/channel/request.py`` (the pre-PA-8a rendering). The block goes
    back over 40,000 chars, ``_fit_to_budget`` removes it, and all four
    assertions below fail at once.
    """
    layers = [prompt_layer_registry.get("channel.current_pocket")]
    result = await assemble(layers, _ctx(_pocket(300)), budget_chars=_DEFAULT_BUDGET_CHARS)

    assert [d.name for d in result.dropped] == []
    assert "<current-pocket>" in result.text
    assert "id: pk-123" in result.text
    assert "mcp__pocketpaw_pocket__get_pocket" in result.text


async def test_the_block_has_a_ceiling_no_pocket_can_push_past():
    """Every caller-supplied field the block RENDERS is bounded, so its size is
    a constant plus the pocket id.

    ``widgets`` and ``name`` both arrive from the wire with no ``max_length``
    (``api.v1.schemas.chat.PocketContext``), and ``metadata`` is a bare ``dict``
    on every channel, so neither is a hypothetical. The id is deliberately NOT
    bounded — it is the literal argument of the ``get_pocket`` call, and a
    truncated id would produce a confidently wrong tool call.

    MUTATION: delete the ``_bounded_name`` call in the layer's ``else`` branch.
    The 20,000-char name lands in the block whole and the ceiling assertion
    fails by an order of magnitude.
    """
    pathological = _pocket(10_000, name="N" * 20_000)
    block = (await _LAYER.render(_ctx(pathological))).text

    # 1215 chars of fixed text for a short id, plus the two bounds, plus slack.
    ceiling = 1215 + _WIDGET_SUMMARY_MAX_CHARS + _POCKET_NAME_MAX_CHARS + 500
    assert len(block) < ceiling
    # And far enough under the budget that nothing plausible above it can push
    # this block out — the property the whole task is about.
    assert len(block) < _DEFAULT_BUDGET_CHARS // 8


# ---------------------------------------------------------------------------
# The bound: whole widgets, before serialisation
# ---------------------------------------------------------------------------


async def test_the_bounded_summary_is_still_parseable_json():
    """The bound drops whole elements, so ``widgets_summary`` stays valid JSON.

    MUTATION: in the layer's ``else`` branch, replace ``json.dumps(bounded)``
    with ``json.dumps(pc.get("widgets", []))[:_WIDGET_SUMMARY_MAX_CHARS]`` —
    the "cap the rendered text" fix. ``json.loads`` below raises
    ``JSONDecodeError`` on the dangling object.
    """
    block = (await _LAYER.render(_ctx(_pocket(300)))).text
    parsed = json.loads(_summary_line(block, "widgets_summary"))

    assert isinstance(parsed, list)
    assert parsed  # something survived; the bound is not "drop everything"
    assert all(isinstance(w, dict) and "name" in w for w in parsed)


async def test_the_bound_admits_the_longest_prefix_that_fits():
    """The incremental char accounting is EXACT, not approximate.

    ``json.dumps`` on a list at the default separators is
    ``"[" + ", ".join(dumps(item)) + "]"``, so one more element costs its own
    dump plus two chars. The function relies on that to avoid re-dumping a
    growing list; this pins it from both sides — what was kept fits, and one
    more would not.

    TINY WIDGETS ARE THE CASE THAT CATCHES A WRONG SEPARATOR, and this test
    initially had none. With ~130-char widgets the bound admits ~15 of them, so
    forgetting the two-char separator under-counts by ~28 chars — not enough to
    let one more 130-char element in, and the error is invisible. With 8-char
    widgets it under-counts by hundreds and admits ~50 too many. A rounding bug
    that only shows on small elements is exactly the kind a fixture of one shape
    misses.

    MUTATION: drop the separator from the cost in ``_bounded_widget_summary``
    (``cost = len(piece)``). The tiny-widget row admits 249 elements that
    serialise to 2,490 chars and the "what was kept fits" assertion fails.
    """
    shapes = {
        "measured": _widgets,
        # 8 chars each, so the separator is a fifth of the per-element cost.
        "tiny": lambda n: [{"n": i} for i in range(n)],
        # One element that on its own blows the whole ceiling.
        "oversized": lambda n: [{"blob": "x" * 4000} for _ in range(n)],
    }
    for label, maker in shapes.items():
        for count in (1, 5, 17, 300):
            widgets = maker(count)
            kept, omitted = _bounded_widget_summary(widgets)

            assert len(json.dumps(kept)) <= _WIDGET_SUMMARY_MAX_CHARS, (label, count)
            assert kept == widgets[: len(kept)], (label, count)
            assert omitted == count - len(kept), (label, count)
            if omitted:
                one_more = widgets[: len(kept) + 1]
                assert len(json.dumps(one_more)) > _WIDGET_SUMMARY_MAX_CHARS, (label, count)


async def test_a_truncated_list_says_that_it_is_truncated():
    """A cut list that looks complete is a lie the model cannot catch.

    MUTATION: delete the ``if omitted:`` branch in the layer. The 300-widget
    block then presents a complete-looking 12-element array and the marker
    assertion fails; the small-pocket half of this test still passes, which is
    the point of asserting both.
    """
    big = (await _LAYER.render(_ctx(_pocket(300)))).text
    kept = len(json.loads(_summary_line(big, "widgets_summary")))
    assert f"widgets_summary_truncated: showing {kept} of 300" in big

    # ...and stays silent when nothing was cut, or every small pocket's bytes
    # would move.
    small = (await _LAYER.render(_ctx(_pocket(1)))).text
    assert "widgets_summary_truncated" not in small


async def test_an_unserialisable_widget_fails_exactly_as_it_did_before():
    """The bound must not quietly RESCUE an input that used to raise.

    ``json.dumps`` on a ``set`` raises ``TypeError`` today, the assembler's
    render guard turns that into a dropped layer, and inventing a rendering
    here would be a behaviour change smuggled in under a size fix. So the bound
    steps aside and lets the original call raise.

    MUTATION: in ``_bounded_widget_summary``, ``continue`` past an
    unserialisable widget instead of returning. No exception is raised and
    ``pytest.raises`` fails.
    """
    with pytest.raises(TypeError):
        await _LAYER.render(_ctx({"id": "pk-1", "name": "N", "widgets": [{"tags": {1, 2}}]}))


async def test_a_non_list_widgets_value_passes_through_untouched():
    """``metadata`` is a bare dict on every channel and can carry anything.

    MUTATION: drop the ``isinstance(widgets, list)`` guard in
    ``_bounded_widget_summary``. Iterating a dict yields its KEYS, so the block
    renders ``["a", "b"]`` instead of the object and this fails.
    """
    block = (await _LAYER.render(_ctx({"id": "pk-1", "name": "N", "widgets": {"a": 1}}))).text
    assert _summary_line(block, "widgets_summary") == '{"a": 1}'


# ---------------------------------------------------------------------------
# Byte identity with the flag off
# ---------------------------------------------------------------------------

# The block EXACTLY as ``ChannelCurrentPocketLayer.render`` built it before
# PA-8a, transcribed from the source rather than captured from the current
# implementation — a golden generated by the thing it guards proves nothing.
_SHIPPED_BLOCK = (
    "\n<current-pocket>\n"
    "id: pk-123\n"
    "name: Launch Tracker\n"
    'widgets_summary: [{"name": "Burndown", "type": "chart"}]\n'
    "\n"
    "SCOPE — read this carefully before doing anything:\n"
    'In this conversation, "pocket" / "this pocket" / "the\n'
    'pocket" always means THIS workspace dashboard\n'
    "(id ``pk-123``) — a MongoDB document the user is\n"
    "viewing on screen. It is NOT the PocketPaw application,\n"
    "NOT the source tree on disk, NOT any file under\n"
    '``D:\\paw`` or ``backend/`` or ``ee/cloud/``. "Edit the\n'
    'pocket", "add a widget", "more widgets" all refer to\n'
    "this document — operate on it through the\n"
    "``mcp__pocketpaw_pocket__*`` tools ONLY. Do NOT use\n"
    "shell, file_edit, grep, or web_search for pocket\n"
    "operations — they cannot read or write the document.\n"
    "\n"
    "NOTE: `widgets_summary` is a shallow hint (names + types)\n"
    "and is OFTEN EMPTY for UISpec-tree pockets — absence here\n"
    "does NOT mean the pocket is empty. The real content lives\n"
    "in rippleSpec.ui.\n"
    "\n"
    "BEFORE answering any question about this pocket's contents,\n"
    "widgets, layout, data, or configuration, you MUST first call:\n"
    "  tool: mcp__pocketpaw_pocket__get_pocket\n"
    '  args: {"pocket_id": "pk-123"}\n'
    "That returns the full document (rippleSpec, widgets,\n"
    "metadata, visibility). Base your answer on that, not on\n"
    "the summary above.\n"
    "</current-pocket>\n"
)


async def test_the_flag_off_block_is_the_shipped_bytes():
    """Default-off renders the pre-PA-8a block, byte for byte.

    MUTATION: change one character of the SCOPE or fetch text in the layer —
    e.g. ``must first call`` for ``MUST first call``. Fails on the diff.
    """
    pc = {
        "id": "pk-123",
        "name": "Launch Tracker",
        "widgets": [{"name": "Burndown", "type": "chart"}],
    }
    assert (await _LAYER.render(_ctx(pc))).text == _SHIPPED_BLOCK


async def test_a_mock_settings_object_does_not_flip_the_flag_on():
    """A ``MagicMock``'s auto-created attribute is TRUTHY, and six suites stub
    settings with one — including the byte goldens.

    A bare ``if settings.prompt_pocket_summary_only`` would silently turn this
    feature ON inside every one of them, and the goldens would fail with a
    diff that looks like the feature working.

    MUTATION: change ``is True`` to a plain truthiness test in
    ``_summary_only``. The mock reads as ON, the block loses
    ``widgets_summary``, and this fails.
    """
    settings = MagicMock()  # nothing configured — the trap
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pocketpaw.config.get_settings", lambda *a, **k: settings)
        block = (await _LAYER.render(_ctx(_pocket(1)))).text
    assert "widgets_summary:" in block
    assert "widgets_count:" not in block


async def test_a_settings_failure_does_not_cost_the_agent_its_pocket_id():
    """Reading the flag must not be able to raise out of ``render``.

    A raising layer is dropped by the assembler's guard — which is precisely
    the failure PA-8a closes, arriving through a new route.

    MUTATION: remove the ``try/except`` from ``_summary_only``. The block is
    dropped, ``result.text`` is empty, and both assertions fail.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("config on fire")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pocketpaw.config.get_settings", _boom)
        result = await assemble(
            [prompt_layer_registry.get("channel.current_pocket")],
            _ctx(_pocket(1)),
            budget_chars=_DEFAULT_BUDGET_CHARS,
        )
    assert result.dropped == []
    assert "mcp__pocketpaw_pocket__get_pocket" in result.text


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


async def test_the_setting_is_off_by_default_and_reads_from_the_environment(monkeypatch):
    """Default OFF, and flippable without a code change.

    ``async`` only because this module carries a file-level ``asyncio`` mark; it
    awaits nothing.

    MUTATION: change the field's ``default`` to ``True`` in ``config.py``. The
    first assertion fails — and so does every golden, which is the safety net
    working as designed.
    """
    assert Settings().prompt_pocket_summary_only is False
    monkeypatch.setenv("POCKETPAW_PROMPT_POCKET_SUMMARY_ONLY", "true")
    assert Settings().prompt_pocket_summary_only is True


async def test_the_flag_on_block_keeps_the_id_the_count_and_the_fetch_order(monkeypatch):
    """ON: the cheap half survives, the bulk detail does not.

    MUTATION: leave the ``widgets_summary`` line in the summary-only head. The
    ``not in`` assertion fails and the block is no longer cheap.
    """
    _flag_on(monkeypatch)
    block = (await _LAYER.render(_ctx(_pocket(300)))).text

    assert "id: pk-123" in block
    assert "name: Launch Tracker" in block
    assert "widgets_count: 300" in block
    assert "mcp__pocketpaw_pocket__get_pocket" in block
    assert 'args: {"pocket_id": "pk-123"}' in block
    # The whole point: no widget detail at all.
    assert "widgets_summary" not in block
    assert "Widget 0" not in block
    assert "Some widget title" not in block


async def test_the_flag_on_block_measurably_drops(monkeypatch):
    """The number behind "drops measurably", on the 300-widget shape.

    MUTATION: none needed to make it fail meaningfully — it fails the moment
    the summary-only branch starts carrying detail again. Kept as a measured
    assertion rather than a comment so the claim cannot rot.
    """
    off = (await _LAYER.render(_ctx(_pocket(300)))).text
    _flag_on(monkeypatch)
    on = (await _LAYER.render(_ctx(_pocket(300)))).text

    assert len(on) < 2_000
    assert len(on) < len(off) / 2


# ---------------------------------------------------------------------------
# The snapshot stamp
# ---------------------------------------------------------------------------


async def test_the_snapshot_moves_on_an_edit_the_summary_cannot_show(monkeypatch):
    """The stamp is why a lossy summary is still safe to cache on.

    Two pockets with the SAME widget count and a different widget: everything
    the summary-only block prints is identical, so without the stamp the two
    would render byte-identical text and hash to one cache key. A backend
    caching an agent on the prompt digest would serve the first pocket's prompt
    for the second.

    MUTATION: delete the ``snapshot:`` line from the summary-only head. Both
    assertions fail — the texts become equal and so do the keys.
    """
    _flag_on(monkeypatch)
    before = await _LAYER.render(_ctx(_pocket(3)))

    edited = _pocket(3)
    edited["widgets"][1]["title"] = "renamed"
    after = await _LAYER.render(_ctx(edited))

    assert "widgets_count: 3" in before.text and "widgets_count: 3" in after.text
    assert before.text != after.text
    assert before.cache_key != after.cache_key


async def test_the_snapshot_holds_still_when_nothing_moved(monkeypatch):
    """...including across key ORDER, which a client round-trip can change.

    A stamp that moved on key order would report every turn as a change and
    rebuild the backend's cached agent for nothing — on the Claude SDK backend
    that is a ~12s reconnect.

    MUTATION: drop ``sort_keys=True`` from ``_pocket_snapshot_stamp``. The
    reordered descriptor hashes differently and the second assertion fails.
    """
    _flag_on(monkeypatch)
    first = (await _LAYER.render(_ctx(_pocket(3)))).text
    again = (await _LAYER.render(_ctx(_pocket(3)))).text
    assert _summary_line(first, "snapshot") == _summary_line(again, "snapshot")

    reordered = {"widgets": _widgets(3), "name": "Launch Tracker", "id": "pk-123"}
    shuffled = (await _LAYER.render(_ctx(reordered))).text
    assert _summary_line(shuffled, "snapshot") == _summary_line(first, "snapshot")


async def test_an_unserialisable_descriptor_still_gets_a_stamp(monkeypatch):
    """A stamp that could raise would lose the block it is stamping.

    ``metadata`` is a bare dict; a ``datetime`` in it is ordinary and a
    self-referential dict is not impossible. Neither may cost the agent its
    pocket id.

    MUTATION: remove the ``try/except`` from ``_pocket_snapshot_stamp``.
    ``render`` raises ``ValueError`` on the cycle and this fails.
    """
    _flag_on(monkeypatch)
    cyclic: dict = {"id": "pk-1", "name": "N", "widgets": []}
    cyclic["self"] = cyclic

    block = (await _LAYER.render(_ctx(cyclic))).text
    assert len(_summary_line(block, "snapshot")) == 16


async def test_capping_the_rendered_block_would_break_the_json():
    """The argument against ``max_chars``, demonstrated rather than asserted.

    A cap sized to hold this block inside the budget lands mid-``json.dumps``,
    so the model reads a dangling object AND loses the ``get_pocket``
    instruction that lives at the bottom. This is what the layer would do if it
    declared a ``max_chars`` instead of bounding its input, and it is why it
    does not.

    MUTATION: none — this test describes the rejected design, so it fails only
    if the block's shape changes such that a tail cut becomes harmless (i.e.
    the instruction moved to the top). That would be a real finding.
    """
    from pocketpaw.prompt.assembler import _apply_cap

    block = (await _LAYER.render(_ctx(_pocket(300)))).text
    capped = _apply_cap("channel.current_pocket", block, 1500, [])

    assert "mcp__pocketpaw_pocket__get_pocket" not in capped
    with pytest.raises(json.JSONDecodeError):
        json.loads(_summary_line(capped, "widgets_summary"))


async def test_the_layer_still_declares_no_max_chars():
    """``max_chars`` stays ``None`` — now for a reason, not by inheritance.

    ``tests/test_context_budget.py`` asserts the same thing on the grounds that
    the old ``_INJECTION_CAPS`` had no row for this block. This restates it as a
    PA-8a decision so a future reader does not "finish the job" by adding one.

    MUTATION: give ``ChannelCurrentPocketLayer`` a ``max_chars``. Fails here and
    in ``test_context_budget.py``.
    """
    assert prompt_layer_registry.get("channel.current_pocket").max_chars is None
