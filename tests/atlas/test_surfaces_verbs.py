# tests/atlas/test_surfaces_verbs.py — the surface + verb fields that make atlas
# the one place that says what surfaces and composer verbs exist.
# Created: 2026-10-01 (feat/atlas-canonical). Pins:
#   * every authored surface states slash / presentation / agent_openable, the
#     inline set and the agent-openable set are exactly the agreed routes;
#   * slashes are well formed, unique across surfaces + verbs, and clear of the
#     lab's own non-navigation commands;
#   * every verb carries applies_to / triggers / risk / undo, and a verb with a
#     slash trigger has a slash (and vice versa);
#   * the compiler refuses a surface or verb missing its kind fields;
#   * the compiled artifact keeps the new keys off every other kind.
# Review pass (same branch): the compiler refuses a missing slash
# key, a non-rooted route, and an agent_openable denylisted route; risk and undo
# are pinned per verb against the lab's verb catalog.
# Follow-up: the slash is the route minus its leading "/" (agents/activity,
# studio/editor) or a listed alias (home), as the composer ships; the compiler
# refuses one that drifts.

from __future__ import annotations

import json
import re

import pytest

from pocketpaw.atlas import compile as compile_mod
from pocketpaw.atlas.compile import AUTHORED_FILES
from pocketpaw.atlas.model import AtlasEntry, never_agent_openable
from pocketpaw.atlas.store import _DATA_PATH, AtlasStore

_SURFACES_PATH = next(p for p in AUTHORED_FILES if p.name == "surfaces.json")
_VERBS_PATH = next(p for p in AUTHORED_FILES if p.name == "verbs.json")

INLINE_ROUTES = {"/chat", "/files", "/deep-work", "/pockets", "/sites", "/knowledge", "/studio"}
AGENT_OPENABLE_ROUTES = {
    "/files",
    "/studio/editor",
    "/studio/vector",
    "/studio/photo",
    "/studio/design",
    "/chat",
    "/pockets",
    "/knowledge",
}
# Lab composer commands that are not surfaces or verbs; a slash must not shadow them.
RESERVED_SLASHES = {"clear", "help", "history", "tray", "new-task"}
_SLASH_RE = re.compile(r"[a-z][a-z0-9-]*(/[a-z][a-z0-9-]*)*")

_NEW_KEYS = {"slash", "presentation", "agent_openable", "applies_to", "triggers", "risk", "undo"}


def _raw(path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


def _store_entries(kind: str) -> list[AtlasEntry]:
    return [e for e in AtlasStore.load().entries if e.kind == kind]


class TestSurfaceFields:
    def test_every_authored_surface_states_all_three(self):
        for raw in _raw(_SURFACES_PATH):
            missing = {"slash", "presentation", "agent_openable"} - set(raw)
            assert not missing, f"{raw['id']} must state {sorted(missing)} (null slash is fine)"

    def test_inline_set_is_exact(self):
        inline = {e.surface for e in _store_entries("surface") if e.presentation == "inline"}
        assert inline == INLINE_ROUTES

    def test_agent_openable_set_is_exact(self):
        """SECURITY PIN. The open_surface allowlist is derived from this set:
        widening it lets the agent navigate the user's browser somewhere new, so
        it must show up here as a failing, reviewed change."""
        openable = {e.surface for e in _store_entries("surface") if e.agent_openable}
        assert openable == AGENT_OPENABLE_ROUTES

    def test_surface_slash_is_the_route_path(self):
        for e in _store_entries("surface"):
            if e.slash is not None:
                assert e.slash == (e.surface[1:] or "home"), e.id

    @pytest.mark.parametrize(
        ("surface_id", "slash"),
        [
            ("surface:agents-activity", "agents/activity"),
            ("surface:studio_editor", "studio/editor"),
        ],
    )
    def test_nested_routes_keep_the_slash(self, surface_id, slash):
        """Matches the composer's shipped commands (paw-enterprise #1094)."""
        e = AtlasStore.load().describe(surface_id)
        assert e is not None and e.slash == slash

    def test_settings_subpages_have_no_slash(self):
        for e in _store_entries("surface"):
            if e.surface.startswith("/settings/"):
                assert e.slash is None, e.id


class TestSlashes:
    def test_slashes_are_well_formed_and_unique(self):
        slashes = [
            e.slash
            for e in AtlasStore.load().entries
            if e.kind in ("surface", "verb") and e.slash is not None
        ]
        for slash in slashes:
            assert _SLASH_RE.fullmatch(slash), slash
        assert len(slashes) == len(set(slashes)), sorted(slashes)
        assert not set(slashes) & RESERVED_SLASHES


# Risk and undo per verb, checked against the lab's verb catalog
# (paw-enterprise src/lib/components/no-ui-lab/verb-catalog.ts): undo is true
# exactly where the lab returns an inverse.
UNDO_VERBS = {
    "verb:file-rename",
    "verb:file-move",
    "verb:file-share-link",
    "verb:task-complete",
    "verb:task-reopen",
    "verb:task-rename",
    "verb:task-assign",
    "verb:task-due",
    "verb:message-edit",
    "verb:pocket-rename",
    "verb:panel-keep",
}
RISKY_VERBS = {
    "verb:send",
    "verb:message-reply",
    "verb:message-edit",
    "verb:message-delete",
    "verb:file-delete",
    "verb:pocket-delete",
    "verb:site-delete",
    "verb:site-publish",
}


class TestVerbFields:
    def test_verbs_are_complete(self):
        verbs = _store_entries("verb")
        assert len(verbs) >= 30
        for v in verbs:
            assert v.id.startswith("verb:")
            assert v.applies_to and v.triggers
            assert v.risk in ("read", "safe", "risky")
            assert isinstance(v.undo, bool)
            assert ("slash" in v.triggers) == (v.slash is not None), v.id

    @pytest.mark.parametrize(
        ("verb_id", "slash", "risk"),
        [
            ("verb:send", "send", "risky"),
            ("verb:task", "task", "safe"),
            ("verb:site", "site", "safe"),
        ],
    )
    def test_composer_slash_verbs(self, verb_id, slash, risk):
        v = AtlasStore.load().describe(verb_id)
        assert v is not None and v.slash == slash and v.risk == risk

    def test_undo_matches_the_lab(self):
        assert {v.id for v in _store_entries("verb") if v.undo} == UNDO_VERBS

    def test_risky_set(self):
        """risky = speaks for the user where others read it, or deletes with no undo."""
        assert {v.id for v in _store_entries("verb") if v.risk == "risky"} == RISKY_VERBS

    def test_catch_up_has_no_slash(self):
        """The composer has no /catch-me-up command; it is a chat-panel verb."""
        v = AtlasStore.load().describe("verb:catch-up")
        assert v is not None and v.slash is None

    def test_destructive_verbs_are_risky_and_final(self):
        for vid in (
            "verb:file-delete",
            "verb:message-delete",
            "verb:site-delete",
            "verb:pocket-delete",
        ):
            v = AtlasStore.load().describe(vid)
            assert v is not None and v.risk == "risky" and v.undo is False, vid


class TestCompileGate:
    @pytest.mark.parametrize(
        ("entry", "missing"),
        [
            (
                AtlasEntry(id="surface:x", kind="surface", name="X", summary="s", narrative="n"),
                "presentation",
            ),
            (AtlasEntry(id="verb:x", kind="verb", name="X", summary="s", narrative="n"), "risk"),
        ],
    )
    def test_incomplete_surface_or_verb_fails_the_build(self, monkeypatch, entry, missing):
        real = compile_mod.load_authored_entries
        monkeypatch.setattr(compile_mod, "load_authored_entries", lambda: [*real(), entry])
        with pytest.raises(ValueError, match=missing):
            compile_mod.compile_atlas()

    def _with_surface(self, monkeypatch, **fields):
        base = dict(id="surface:x", kind="surface", name="X", summary="s", narrative="n")
        base.update(presentation="window", agent_openable=False, slash=None)
        base.update(fields)
        real = compile_mod.load_authored_entries
        entry = AtlasEntry(**base)
        monkeypatch.setattr(compile_mod, "load_authored_entries", lambda: [*real(), entry])

    @pytest.mark.parametrize("route", ["", "x", "//evil.example", "https://evil.example"])
    def test_non_rooted_route_fails_the_build(self, monkeypatch, route):
        self._with_surface(monkeypatch, surface=route)
        with pytest.raises(ValueError, match="start with one"):
            compile_mod.compile_atlas()

    @pytest.mark.parametrize(
        "route",
        ["/settings", "/settings/billing", "/settings/workspace", "/audit", "/security", "/admin"],
    )
    def test_denylisted_route_flagged_openable_fails_the_build(self, monkeypatch, route):
        """SECURITY: settings / audit / security / admin are never agent-openable."""
        self._with_surface(monkeypatch, surface=route, agent_openable=True)
        with pytest.raises(ValueError, match="never be agent_openable"):
            compile_mod.compile_atlas()

    @pytest.mark.parametrize(("route", "slash"), [("/x/y", "x-y"), ("/x", "y"), ("/", "x")])
    def test_slash_that_drifts_from_the_route_fails_the_build(self, monkeypatch, route, slash):
        self._with_surface(monkeypatch, surface=route, slash=slash)
        with pytest.raises(ValueError, match="slash must match its route"):
            compile_mod.compile_atlas()

    def test_missing_slash_key_fails_the_build(self, monkeypatch, tmp_path):
        raw = json.loads(_SURFACES_PATH.read_text(encoding="utf-8"))
        del raw["entries"][0]["slash"]
        bad = tmp_path / "surfaces.json"
        bad.write_text(json.dumps(raw), encoding="utf-8")
        files = tuple(bad if p == _SURFACES_PATH else p for p in AUTHORED_FILES)
        monkeypatch.setattr(compile_mod, "AUTHORED_FILES", files)
        with pytest.raises(ValueError, match="missing the slash key"):
            compile_mod.compile_atlas()


class TestDenylist:
    @pytest.mark.parametrize(
        "route",
        [
            "/settings",
            "/settings/",
            "/settings/billing",
            "/audit",
            "/security",
            "/admin",
            "/admin/x",
            "//x",
            "x",
        ],
    )
    def test_never_openable(self, route):
        assert never_agent_openable(route)

    @pytest.mark.parametrize("route", ["/files", "/chat", "/studio/editor", "/settingsx"])
    def test_ordinary_routes_are_allowed(self, route):
        assert not never_agent_openable(route)


def test_artifact_keeps_new_keys_off_other_kinds():
    entries = json.loads(_DATA_PATH.read_text(encoding="utf-8"))["entries"]
    for raw in entries:
        if raw["kind"] not in ("surface", "verb"):
            assert not _NEW_KEYS & set(raw), raw["id"]
