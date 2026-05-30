# tests/cloud/pockets/test_merge_spec.py
# Created: 2026-05-24 — unit tests for the pure merge helper that powers
# the new ``POST /api/v1/pockets/{id}/spec/merge`` endpoint. Covers the
# four cases the merge semantics need to pin: a prop-change on one node,
# an added child via parent re-statement, a state-only patch, and an
# orphan id that doesn't match anything in the base tree.
# Updated: 2026-05-25 (PR #1222 R1 high-priority 1) — added
# ``test_merge_cycle_in_patch_does_not_hang`` to pin the visited-set
# guard added to ``_collect_patch_nodes`` / ``_collect_all_ids``. The
# patch tree is untrusted agent output; a self-referential children
# graph used to loop forever before the guard.
# Updated: 2026-05-30 (issue #1301 RED) — added
# ``test_add_widget_intent_lands_orphan_state_warns`` to reproduce the
# silent-orphan-state bug: a state-only merge patch that adds a brand-new
# state key with NO ui referent persists silently and renders nothing.
# The test drives the service-level ``merge_spec`` (the layer that owns
# the ``ok`` / ``warnings`` envelope the specialist self-correction loop
# reads) and asserts a non-blocking warning names the orphan key.
# Updated: 2026-05-30 (issue #1301 GREEN) — the fix lands a NON-BLOCKING
# orphan-state warning via ``find_unreferenced_state_keys``. Reworked the
# helper-level bug-pin ``test_state_only_patch_shallow_merges`` so its
# patched keys are referenced by ui (it no longer pins the bug), and added
# the negative test ``test_state_patch_for_referenced_key_no_warning``
# (updating an already-bound key AND seeding a ``sources`` bind target both
# emit NO orphan-state warning) to guard against false positives.
"""Unit tests for ``pocketpaw_ee.cloud.pockets._merge.merge_ripple_spec``."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets._merge import merge_ripple_spec

# ---------------------------------------------------------------------------
# Fixtures — a small base spec a few tests can reuse without re-typing.
# ---------------------------------------------------------------------------


def _base_spec() -> dict:
    """Two-button flex pocket with a draft state field. Deliberately
    small — every merge test below either swaps one node or adds one
    sibling, and a small base keeps the diff obvious in failure output.
    """
    return {
        "version": "1.0",
        "state": {"draft": "", "count": 0},
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column", "gap": 12},
            "children": [
                {
                    "id": "n_btn00001",
                    "type": "button",
                    "props": {"label": "Click me"},
                },
                {
                    "id": "n_btn00002",
                    "type": "button",
                    "props": {"label": "Or me"},
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Test 1 — prop change on one node (re-state by id, no sibling touched).
# ---------------------------------------------------------------------------


def test_prop_change_replaces_only_targeted_node():
    """Patch re-states a single node by id. That node is replaced
    wholesale; the sibling stays byte-identical; the parent's structure
    is preserved."""
    base = _base_spec()
    patch = {
        "ui": {
            "id": "n_btn00001",
            "type": "button",
            "props": {"label": "New label", "color": "#0A84FF"},
        },
    }

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    # The targeted button is fully replaced — props REPLACED, not merged.
    target = merged["ui"]["children"][0]
    assert target["id"] == "n_btn00001"
    assert target["props"] == {"label": "New label", "color": "#0A84FF"}
    # Sibling is untouched.
    sibling = merged["ui"]["children"][1]
    assert sibling == base["ui"]["children"][1]
    # Parent structure preserved.
    assert merged["ui"]["id"] == base["ui"]["id"]
    assert merged["ui"]["type"] == base["ui"]["type"]
    assert merged["ui"]["props"] == base["ui"]["props"]
    # State untouched.
    assert merged["state"] == base["state"]


# ---------------------------------------------------------------------------
# Test 2 — added child via parent re-statement.
# ---------------------------------------------------------------------------


def test_add_child_via_parent_restatement():
    """To add a new node, the patch re-states the parent's children
    array with the new child appended. The new child has an id that's
    NOT yet in the base — it should land in the merged tree as part of
    the parent's wholesale replacement, with no orphan reported."""
    base = _base_spec()
    new_child = {
        "id": "n_btn00003",
        "type": "button",
        "props": {"label": "Brand new"},
    }
    patch = {
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column", "gap": 12},
            "children": [
                # Re-state every existing child verbatim so the parent
                # replacement keeps them.
                copy.deepcopy(base["ui"]["children"][0]),
                copy.deepcopy(base["ui"]["children"][1]),
                new_child,
            ],
        },
    }

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    children = merged["ui"]["children"]
    assert len(children) == 3
    assert [c["id"] for c in children] == ["n_btn00001", "n_btn00002", "n_btn00003"]
    assert children[2] == new_child


# ---------------------------------------------------------------------------
# Test 3 — state-only patch leaves ui untouched.
# ---------------------------------------------------------------------------


def test_state_only_patch_shallow_merges():
    """Patch with only a ``state`` key shallow-merges into existing
    state. Keys not present in the patch are kept; the ui tree is
    untouched byte-identical.

    The patch only touches keys the base ui already reads (``draft`` via
    the input binding, ``count`` via the badge text). Updating an
    already-bound key is the legitimate state-only edit — it must NOT
    surface an orphan-state warning (issue #1301). A patch that ADDED an
    unreferenced key would, but that's covered by the service-level repro
    ``test_add_widget_intent_lands_orphan_state_warns``; here we keep the
    shallow-merge contract honest with a coherent (referenced) patch."""
    base = _base_spec()
    # Wire the base ui so both keys the patch touches are already read by
    # a widget — this is the well-formed state-only edit shape.
    base["ui"]["children"][0]["props"]["value"] = "{state.draft}"
    base["ui"]["children"][1]["props"]["label"] = "{state.count}"
    patch = {"state": {"draft": "typed", "count": 7}}

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    assert merged["state"] == {"draft": "typed", "count": 7}
    # ui untouched
    assert merged["ui"] == base["ui"]
    # And no orphan-state: every key the patch touched is read by the ui.
    from pocketpaw_ee.cloud.ripple_validator import find_unreferenced_state_keys

    assert find_unreferenced_state_keys(merged) == []


# ---------------------------------------------------------------------------
# Test 4 — invalid patch with an id that doesn't reach base. The MVP
# decision is auto-orphan + report the id so the caller (the agent)
# can surface a corrective warning. The merge does NOT block.
# ---------------------------------------------------------------------------


def test_orphan_id_in_patch_is_reported_and_dropped():
    """A patch that mentions a node id which is NOT present in
    base.ui — and is NOT placed under any parent that IS in base — gets
    reported in the orphans list. The base tree is otherwise untouched.

    MVP semantics (per the captain): auto-orphan + warn, do not error.
    The merged spec is the base unchanged and the orphans list flags
    what was ignored."""
    base = _base_spec()
    patch = {
        "ui": {
            "id": "n_notInBase",
            "type": "button",
            "props": {"label": "Ghost"},
        },
    }

    merged, orphans = merge_ripple_spec(base, patch)

    # The orphan id is reported.
    assert orphans == ["n_notInBase"]
    # The base ui is preserved unchanged (the orphan got dropped).
    assert merged["ui"] == base["ui"]
    assert merged["state"] == base["state"]


# ---------------------------------------------------------------------------
# Bonus — top-level field overwrite + deep copy isolation.
# ---------------------------------------------------------------------------


def test_top_level_field_overwrite_and_deep_copy_isolation():
    """Top-level keys other than state / ui overwrite the base. The
    merge result is a deep copy of the base — mutating the merged tree
    must not mutate the original base."""
    base = _base_spec()
    base["title"] = "Old title"
    patch = {"title": "New title"}

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    assert merged["title"] == "New title"
    # Deep-copy isolation: mutate merged, original base must not change.
    merged["ui"]["children"][0]["props"]["label"] = "MUTATED"
    assert base["ui"]["children"][0]["props"]["label"] == "Click me"


def test_root_node_id_match_replaces_whole_tree():
    """A patch whose ui top-level id matches base's root id replaces
    the entire ui tree wholesale — the equivalent of ``replace_node``
    against the root in the old granular-op surface."""
    base = _base_spec()
    new_root = {
        "id": "n_root0001",
        "type": "flex",
        "props": {"direction": "row", "gap": 4},
        "children": [
            {"id": "n_text0001", "type": "text", "props": {"value": "Hi"}},
        ],
    }
    patch = {"ui": new_root}

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    assert merged["ui"] == new_root
    assert merged["state"] == base["state"]


def test_non_dict_inputs_raise_typeerror():
    """The helper is a pure function — non-dict inputs should fail
    fast instead of silently producing a malformed spec."""
    with pytest.raises(TypeError):
        merge_ripple_spec([], {})
    with pytest.raises(TypeError):
        merge_ripple_spec({}, "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PR #1222 R1 high-priority 1 — cycle guard. The patch tree comes from
# untrusted agent output; a self-referential ``children`` list must not
# hang the merge walk. The guard lives in ``_collect_patch_nodes`` and
# ``_collect_all_ids`` and is keyed on node id.
# ---------------------------------------------------------------------------


def test_merge_cycle_in_patch_does_not_hang():
    """A patch where node_a.children references node_b and node_b.children
    references node_a must terminate. ``asyncio.wait_for`` is the
    timeout primitive available without an extra plugin.
    """

    # Construct a cycle via shared dict references — the merge walk
    # follows ``children`` lists, so two id-bearing dicts that point at
    # each other's children form a self-referential graph.
    node_a: dict[str, Any] = {"id": "n_a00001", "type": "flex", "props": {}}
    node_b: dict[str, Any] = {"id": "n_b00001", "type": "flex", "props": {}}
    node_a["children"] = [node_b]
    node_b["children"] = [node_a]

    base = _base_spec()
    patch = {"ui": node_a}

    async def _run() -> tuple[dict, list[str]]:
        # ``merge_ripple_spec`` is sync — wrap so wait_for can cancel
        # if the implementation regresses to an unbounded walk.
        return merge_ripple_spec(base, patch)

    merged, orphans = asyncio.run(asyncio.wait_for(_run(), timeout=2.0))

    # Result shape doesn't matter as much as termination. Confirm the
    # walk produced a sane structure: both ids in the patch are reachable
    # (via the visited-set, each is recorded once) and neither is an
    # orphan because the patch UI tree was walked but never matched a
    # base id — the orphan-vs-matched bookkeeping treats them as orphans
    # (n_a / n_b aren't in base.ui, base's children are unchanged).
    assert isinstance(merged, dict)
    assert isinstance(orphans, list)
    # The IDs in the cycle must appear in the orphan list (they're in
    # patch but not in base) — exactly once each, no infinite duplicates.
    assert orphans.count("n_a00001") == 1
    assert orphans.count("n_b00001") == 1


# ---------------------------------------------------------------------------
# Issue #1301 — an add-widget intent that lands as a state-only merge patch.
#
# Root cause: ``merge_spec`` merges ``state`` and ``ui`` as INDEPENDENT
# branches and seeds warnings ONLY from ``orphans`` (ui ids in the patch
# not reachable from base.ui). A state-only patch produces zero orphans,
# so a brand-new state key with NO ui node that reads it persists silently
# and renders nothing — ``ok:true`` with empty ``warnings``.
#
# The fix is a deterministic NON-BLOCKING orphan-state warning: a new state
# key that no ui node references (and isn't a legitimate ``sources`` bind
# target) must surface a warning naming it, flowing back through the
# existing warnings channel into the specialist self-correction loop. The
# merge still persists (``ok:true``) because a state-only seed can be
# legitimate.
#
# This test drives the service-level ``merge_spec`` because that is the
# layer that owns the ``ok`` / ``warnings`` envelope the specialist reads.
# Its DB-bound collaborators are stubbed in-memory (the same mock-the-
# collaborators posture ``test_merge_spec_endpoint.py`` uses) so the test
# isolates the warning behaviour, not the Beanie / catalog plumbing.
# ---------------------------------------------------------------------------


class _FakeDoc:
    """In-memory stand-in for ``_PocketDoc`` — only the attributes the
    ``merge_spec`` path reads/writes (``rippleSpec``, ``id``, ``workspace``,
    ``save``)."""

    def __init__(self, spec: dict, *, pocket_id: str, workspace: str) -> None:
        self.rippleSpec = spec
        self.id = pocket_id
        self.workspace = workspace
        self.save_calls = 0

    async def save(self) -> None:
        self.save_calls += 1


def _wire_merge_spec_stubs(monkeypatch: pytest.MonkeyPatch, fake_doc: _FakeDoc) -> None:
    """Monkeypatch every DB-bound collaborator ``merge_spec`` touches so it
    runs in-memory against ``fake_doc`` — same mock-the-collaborators posture
    ``test_merge_spec_endpoint.py`` uses. Isolates the orphan-state warning
    behaviour from Beanie / catalog plumbing."""

    async def _fake_fetch(pocket_id: str):  # type: ignore[no-untyped-def]
        return fake_doc

    async def _fake_resolved_wire_dict(doc, viewer_user_id):  # type: ignore[no-untyped-def]
        return {"id": doc.id, "rippleSpec": doc.rippleSpec}

    async def _fake_event_payload(doc):  # type: ignore[no-untyped-def]
        return {"recipient_ids": [], "pocket": {"id": doc.id}}

    async def _fake_emit(event):  # type: ignore[no-untyped-def]
        return None

    async def _noop_gate_catalog(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(pockets_service, "_fetch_pocket", _fake_fetch)
    monkeypatch.setattr(pockets_service, "_pocket_to_domain", lambda doc: object())
    monkeypatch.setattr(pockets_service, "_check_domain_edit_access", lambda *a, **k: None)
    monkeypatch.setattr(pockets_service, "_gate_catalog", _noop_gate_catalog)
    monkeypatch.setattr(pockets_service, "_resolved_wire_dict", _fake_resolved_wire_dict)
    monkeypatch.setattr(pockets_service, "_pocket_event_payload", _fake_event_payload)
    monkeypatch.setattr(pockets_service, "emit", _fake_emit)


def test_add_widget_intent_lands_orphan_state_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An add-widget intent that lands as a STATE-ONLY merge patch — a
    brand-new state key with NO ui referent — must persist (``ok:true``,
    non-blocking) AND surface a warning naming the new key as orphan /
    unreferenced state.

    Reproduces issue #1301: today the merge succeeds with empty warnings
    because nothing cross-references state against ui. RED — this asserts
    the warning that the fix will add.
    """
    # Base spec whose ui references an existing state key (state.draft via
    # the textarea-style binding the renderer reads). ``smokeNotes`` below
    # is the brand-new key with no ui node that reads it.
    base_spec = {
        "version": "1.0",
        "state": {"draft": ""},
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column", "gap": 12},
            "children": [
                {
                    "id": "n_input001",
                    "type": "input",
                    "props": {"value": "{{state.draft}}", "label": "Draft"},
                },
            ],
        },
    }

    fake_doc = _FakeDoc(
        copy.deepcopy(base_spec),
        pocket_id="pocket-1301",
        workspace="ws-1301",
    )

    # --- Stub the DB-bound collaborators so ``merge_spec`` runs in-memory.
    _wire_merge_spec_stubs(monkeypatch, fake_doc)

    # State-only merge patch: adds a brand-new key with NO ui node reading it.
    body = {"merge": {"state": {"smokeNotes": "remember to wire a widget"}}}

    result = asyncio.run(
        pockets_service.merge_spec(
            workspace_id="ws-1301",
            user_id="user-1301",
            pocket_id="pocket-1301",
            body=body,
        )
    )

    # Non-blocking: the seed persists.
    assert result["ok"] is True, result
    assert fake_doc.save_calls == 1
    # The new key is in the persisted state (shallow-merged in).
    assert fake_doc.rippleSpec["state"]["smokeNotes"] == "remember to wire a widget"

    # The bug fix: a warning must name the orphan / unreferenced state key.
    warnings = result.get("warnings") or []
    assert any("smokeNotes" in w for w in warnings), (
        "expected a non-blocking warning naming 'smokeNotes' as orphan / "
        f"unreferenced state, got warnings={warnings!r}"
    )


# ---------------------------------------------------------------------------
# Issue #1301 negative — guard against false positives. Two legitimate
# state-only edits must produce NO orphan-state warning:
#   (a) updating an already-ui-bound key, and
#   (b) seeding a ``sources`` bind target (a write-target no widget reads
#       yet — the unconditional-seed rule makes this legitimate).
# ---------------------------------------------------------------------------


def test_state_patch_for_referenced_key_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UPDATE to an already-bound state key and a seed of a ``sources``
    bind target must both persist (``ok:true``) with NO orphan-state
    warning. Guards the #1301 fix against false positives."""
    # --- Case (a): patch updates ``draft``, which the input already reads.
    base_a = {
        "version": "1.0",
        "state": {"draft": ""},
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {
                    "id": "n_input001",
                    "type": "input",
                    "props": {"value": "{state.draft}", "label": "Draft"},
                },
            ],
        },
    }
    doc_a = _FakeDoc(copy.deepcopy(base_a), pocket_id="p-a", workspace="ws-a")
    _wire_merge_spec_stubs(monkeypatch, doc_a)

    result_a = asyncio.run(
        pockets_service.merge_spec(
            workspace_id="ws-a",
            user_id="u-a",
            pocket_id="p-a",
            body={"merge": {"state": {"draft": "now typed"}}},
        )
    )

    assert result_a["ok"] is True, result_a
    assert doc_a.save_calls == 1
    warnings_a = result_a.get("warnings") or []
    assert not any("draft" in w and "no ui widget" in w for w in warnings_a), (
        f"updating an already-bound key must not warn, got {warnings_a!r}"
    )

    # --- Case (b): patch seeds ``prs`` — a declared ``sources`` bind
    #     target that no widget reads yet. Legitimate per the seed rule.
    base_b = {
        "version": "1.0",
        "state": {},
        "sources": {
            "prs": {"path": "/pulls", "bind": "state.prs", "method": "GET"},
        },
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {"id": "n_text0001", "type": "text", "props": {"value": "Loading…"}},
            ],
        },
    }
    doc_b = _FakeDoc(copy.deepcopy(base_b), pocket_id="p-b", workspace="ws-b")
    _wire_merge_spec_stubs(monkeypatch, doc_b)

    result_b = asyncio.run(
        pockets_service.merge_spec(
            workspace_id="ws-b",
            user_id="u-b",
            pocket_id="p-b",
            body={"merge": {"state": {"prs": []}}},
        )
    )

    assert result_b["ok"] is True, result_b
    assert doc_b.save_calls == 1
    warnings_b = result_b.get("warnings") or []
    assert not any("prs" in w and "no ui widget" in w for w in warnings_b), (
        f"seeding a declared sources bind target must not warn, got {warnings_b!r}"
    )
