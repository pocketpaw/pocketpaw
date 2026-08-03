# tests/cloud/surface/test_entity_id_contract.py — the gate that keeps prompt
# rows addressable.
# Created: 2026-08-03 (feat/prompt-entity-ids).
#
# THE RULE: if a tool declares a required ``<kind>_id``, every prompt block that
# lists ``<kind>``s must carry that id on each row.
#
# It was broken in four places at once and nobody noticed, because each list
# handler was written independently and ``rows.append(f"- {name} ...")`` is the
# obvious thing to type. ``pocketpaw.prompt.entity`` is the one spelling; this
# file is what stops the fifth handler from skipping it.
#
# TWO CHECKS, doing different jobs:
#
#   1. NO HAND-ROLLED ROWS (AST). Scans the handler packages for f-strings
#      shaped like an entity row and fails any that did not come from
#      ``entity_line``. Catches the bug at the shape level, before anyone has to
#      reason about whether that particular entity happens to be addressable.
#   2. THE TOOL SURFACE HAS NOT GROWN (derived). Enumerates every MCP server's
#      tool schemas and collects the required ``<kind>_id`` params. This is the
#      authoritative list of what the agent can address, and it is DERIVED — add
#      a tool with a required ``site_id`` and this test fails until someone
#      decides whether the sites preamble now owes an id. That is the property
#      that makes this permanent rather than a one-time cleanup: the check
#      updates itself, and the failure lands on the person adding the tool.
#
# Check 2 is a tripwire, not a proof — it cannot know which preamble lists which
# kind. It is deliberately the cheap mechanism, because the expensive one (map
# every kind to its rendering surface) would itself be a hand-maintained list,
# which is the thing that rots.

from __future__ import annotations

import ast
import asyncio
import importlib
import pkgutil
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1. No hand-rolled entity rows
# ---------------------------------------------------------------------------

_SCANNED_PACKAGES = (
    "ee/pocketpaw_ee/cloud/surface/handlers",
    "src/pocketpaw/prompt",
)

# Rows allowed to stay hand-rolled, as ``module -> (count, reason)``.
#
# THE COUNT IS THE POINT. A bare module-name allow-list would exempt the module
# forever, so the next row added to ``calendar.py`` would inherit the exemption
# and ship the exact bug this file exists to stop. Pinning the count means a new
# hand-rolled row fails here even in an already-listed module — the exemption
# covers the rows that were reviewed, not the file.
#
# Every entry claims the thing being listed is NOT an entity addressed by id:
# either it is a record of an event rather than a thing, or its label already IS
# its address. Both numbers may only ever go DOWN.
_HANDROLLED_ALLOWED = {
    # "- 10:30 AM · Sync with Sarah" ×2 (with and without a parsed time).
    # Google Calendar events via Composio. They carry an id and no tool takes
    # it: the ``meeting_id`` five tools require addresses a ``_MeetingDoc`` in
    # our own collection, a different entity that no preamble lists. Checked
    # against meetings/service.py::cancel_meeting on 2026-08-03.
    "calendar.py": (2, "google calendar events; no tool takes a calendar event id"),
    # "- pocket.created: Sales" — a record OF an action, not a thing to act on.
    "activity.py": (1, "activity feed lines are events, not addressable entities"),
    "audit.py": (1, "audit entries are events, not addressable entities"),
    "home.py": (1, "home activity digest lines are events, not entities"),
    # "- workspace:w1" — a KB scope. The label IS the address; there is no
    # separate id to carry.
    "knowledge.py": (1, "a kb scope string is its own identifier"),
    # "- hero: https://… (alt: "…")" — asset manifest lines. The URL is the
    # address, and it is already rendered.
    "sites.py": (1, "an asset url is its own identifier"),
    # Two rows, exempt for two different reasons. "- Ripple: a Svelte runtime
    # that…" is an atlas glossary entry from a static seed — prose. "- **name**:
    # description" is the skills list, and a skill is invoked BY NAME: the
    # loader keys them in a dict, so the name is unique by construction and is
    # itself the address.
    "environment.py": (2, "atlas entries are prose; a skill's name is its address"),
    "identity.py": (1, "soul knowledge lines are prose, not entities"),
}

# The renderer itself necessarily contains the row shape it produces. Excluded
# structurally rather than allow-listed — it is the fix, not an exception to it.
_RENDERER_MODULE = "entity.py"

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _handrolled_rows(path: Path) -> list[int]:
    """Line numbers of f-strings shaped like an entity row.

    The shape is an f-string whose literal head starts with ``"- "`` and which
    interpolates at least one value — i.e. a per-item row rather than a static
    line. ``f"... (+{n} more)"`` does not match (wrong head) and neither does a
    plain string constant (nothing interpolated).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or not node.values:
            continue
        head = node.values[0]
        if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
            continue
        if not head.value.startswith("- "):
            continue
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            hits.append(node.lineno)
    return hits


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for pkg in _SCANNED_PACKAGES:
        root = _REPO_ROOT / pkg
        assert root.is_dir(), f"scan target moved: {root}"
        files.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return files


class TestNoHandRolledEntityRows:
    def test_every_entity_row_goes_through_entity_line(self) -> None:
        """The check that makes the renderer unavoidable.

        A correct renderer nobody calls is the original bug intact, so this
        asserts the CALL SHAPE rather than the output: a handler cannot opt out
        by writing a row that happens to include an id today.

        THE MUTATION THAT BREAKS THIS: restore ``pockets_list.py``'s
        ``rows.append(f"- {name} (type={kind}, ...)")``. Run: the offender list
        was non-empty and this failed. (Applied 2026-08-03.)
        """
        offenders = []
        for path in _scanned_files():
            if path.name == _RENDERER_MODULE or path.name in _HANDROLLED_ALLOWED:
                continue
            for lineno in _handrolled_rows(path):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")

        assert not offenders, (
            "hand-rolled entity rows found — use pocketpaw.prompt.entity.entity_line "
            "so the row carries the id the agent needs to act on it:\n  " + "\n  ".join(offenders)
        )

    def test_an_allow_listed_module_cannot_grow_a_new_row(self) -> None:
        """The exemption covers the rows that were reviewed, not the file.

        Without the pinned count, ``calendar.py`` — exempt because Google
        Calendar events are not tool-addressable — would silently cover a
        pockets row someone adds to it later. This is the check that keeps a
        seven-entry allow-list from becoming a seven-module blind spot.

        THE MUTATION THAT BREAKS THIS: add a second hand-rolled row to
        ``knowledge.py``. Run: 2 != 1 and this failed. (Applied 2026-08-03.)
        """
        scanned = {p.name: p for p in _scanned_files()}
        drift = []
        for name, (expected, _reason) in _HANDROLLED_ALLOWED.items():
            path = scanned.get(name)
            if path is None:
                drift.append(f"{name}: no longer scanned — remove the entry")
                continue
            actual = len(_handrolled_rows(path))
            if actual != expected:
                verb = "gained" if actual > expected else "lost"
                drift.append(f"{name}: {verb} rows ({expected} allowed, {actual} found)")

        assert not drift, (
            "hand-rolled row counts moved. A NEW row must go through entity_line; "
            "a REMOVED one means the pinned count should come down:\n  " + "\n  ".join(drift)
        )


# ---------------------------------------------------------------------------
# 2. The addressable-kind surface, derived from the tool schemas
# ---------------------------------------------------------------------------

_REQUIRED_ID = re.compile(r"^(?P<kind>[a-z0-9_]+)_id$")

# Every kind some tool requires an id for, as of 2026-08-03. DERIVED, not
# authored — regenerate by running the test and reading the failure.
#
# The three that a prompt block actually lists are ``pocket`` (pockets_list),
# ``widget`` (the pinned-widgets block) and ``user`` (the about-member block);
# all three now render ids. The rest are addressed from tool output or from the
# request context, and no preamble lists them.
_KNOWN_ADDRESSABLE_KINDS = frozenset(
    {
        "assignee",
        "backtest",
        "custom_scenario",
        "decision",
        "from",
        "input",
        "invite",
        "link",
        "meeting",
        "pocket",
        "project",
        "run",
        "scenario",
        "task",
        "to",
        "user",
        "widget",
    }
)


def _derive_addressable_kinds() -> tuple[set[str], list[str]]:
    """Collect ``<kind>`` for every required ``<kind>_id`` across all MCP tools.

    Returns the kinds and the builders that could not be enumerated. The second
    half matters: a builder that raises is a silent hole in the derivation, and
    a check that quietly enumerated half the tool surface would report a clean
    result while missing the tool that motivated it.
    """
    import pocketpaw_ee.agent.mcp_servers as servers_pkg
    from mcp import types

    kinds: set[str] = set()
    skipped: list[str] = []
    for mod_info in pkgutil.iter_modules(servers_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{servers_pkg.__name__}.{mod_info.name}")
        except Exception as exc:  # pragma: no cover - reported, not asserted here
            skipped.append(f"{mod_info.name}: import {type(exc).__name__}")
            continue
        builders = [
            getattr(module, attr)
            for attr in dir(module)
            if attr.startswith("build_") and callable(getattr(module, attr))
        ]
        for builder in builders:
            try:
                built = builder()
            except Exception as exc:  # pragma: no cover
                skipped.append(f"{mod_info.name}.{builder.__name__}: build {type(exc).__name__}")
                continue
            if not built:
                continue
            try:
                _name, server = built
                handler = server["instance"].request_handlers[types.ListToolsRequest]
                listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
            except Exception as exc:  # pragma: no cover
                skipped.append(f"{mod_info.name}.{builder.__name__}: list {type(exc).__name__}")
                continue
            for tool in listed.root.tools:
                for param in (tool.inputSchema or {}).get("required") or []:
                    match = _REQUIRED_ID.match(param)
                    if match:
                        kinds.add(match.group("kind"))
    return kinds, skipped


class TestAddressableKindsAreReviewed:
    @pytest.fixture(scope="class")
    def derived(self) -> tuple[set[str], list[str]]:
        return _derive_addressable_kinds()

    def test_the_derivation_reaches_every_server(self, derived: tuple[set[str], list[str]]) -> None:
        """A partial enumeration would report clean while missing the point.

        Every MCP server builder must be importable and listable with no
        ambient request context — they are today (0 skipped, 33 tools, 17 kinds
        as of 2026-08-03). If one starts needing a live DB to enumerate its
        tools, this fails rather than quietly shrinking the derived set.

        THE MUTATION THAT BREAKS THIS: point the loop at a package whose
        builders raise. Run: skipped was non-empty and this failed.
        """
        _kinds, skipped = derived
        assert not skipped, "MCP tool schemas could not be enumerated:\n  " + "\n  ".join(skipped)

    def test_a_new_addressable_kind_gets_looked_at(
        self, derived: tuple[set[str], list[str]]
    ) -> None:
        """The tripwire. Adding a tool with a required id lands the decision here.

        This does NOT assert that each kind's preamble renders an id — nothing
        maps kinds to surfaces, and a map that did would be the hand-maintained
        list this design set out to avoid. What it guarantees is that the tool
        surface cannot grow without somebody answering the question, in the
        commit that grows it.

        If this fails: check whether any prompt block LISTS the new kind. If it
        does, render the id through ``entity_line``. If it does not, add the
        kind below. Either way the answer is deliberate.

        THE MUTATION THAT BREAKS THIS: drop ``"widget"`` from
        ``_KNOWN_ADDRESSABLE_KINDS``. Run: widget was reported as new and this
        failed. (Applied 2026-08-03.)
        """
        kinds, _skipped = derived
        new = kinds - _KNOWN_ADDRESSABLE_KINDS
        assert not new, (
            "new tool(s) take a required <kind>_id: " + ", ".join(sorted(new)) + ".\n"
            "Does a prompt block list this kind? If yes, render the id via "
            "pocketpaw.prompt.entity.entity_line. If no, add it to "
            "_KNOWN_ADDRESSABLE_KINDS."
        )

    def test_the_known_set_has_no_dead_kinds(self, derived: tuple[set[str], list[str]]) -> None:
        """A kind that no tool requires any more should leave the set.

        Keeps the list honest about what the agent can currently address, so
        the next reader can trust it as documentation rather than archaeology.

        THE MUTATION THAT BREAKS THIS: add ``"unicorn"`` to
        ``_KNOWN_ADDRESSABLE_KINDS``. Run: reported as dead and this failed.
        """
        kinds, _skipped = derived
        dead = _KNOWN_ADDRESSABLE_KINDS - kinds
        assert not dead, (
            "these kinds no longer have a tool requiring their id: "
            + ", ".join(sorted(dead))
            + " — remove them from _KNOWN_ADDRESSABLE_KINDS."
        )

    def test_the_three_listed_kinds_are_addressable(
        self, derived: tuple[set[str], list[str]]
    ) -> None:
        """The kinds a preamble actually lists, pinned by name.

        ``pocket``, ``widget`` and ``user`` are the three that a prompt block
        enumerates AND a tool addresses by id — the whole reason this work
        exists. If a refactor drops the required id from any of their tools,
        the general tripwire above would report it as merely "dead" and someone
        would delete the entry. This says out loud that these three are load
        bearing.

        THE MUTATION THAT BREAKS THIS: make ``update_widget``'s ``widget_id``
        optional. Run: widget left the derived set and this failed.
        """
        kinds, _skipped = derived
        for kind in ("pocket", "widget", "user"):
            assert kind in kinds, f"no tool requires {kind}_id any more — is the preamble stale?"
