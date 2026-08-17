# tests/ee/agent/test_sites_mcp_server/test_create_react_site.py
# Created: 2026-08-07 (RX-2 — the agent can select the react engine) — coverage
# for the Paw Sites "react track" create tool ``create_react_site`` on the
# in-process ``pocketpaw_sites_manager`` server. It is the 5th create tool,
# mirroring ``create_html_site`` (the agent IS the author; a {path: contents}
# source map is persisted verbatim via ``agent_create`` with ``engine="react"`` +
# ``ripple_spec=None`` + ``trusted=True``) with two react-specific additions.
# Four layers:
#   1. source-map validation (``_missing_react_keys``) — the ``src/App.tsx``
#      composition root is required (both generated entries import it), so the
#      create fails CLOSED rather than persisting a map that builds nothing.
#   2. reserved-path rejection (``_reserved_react_keys``) — the generator owns
#      the build shell + the ``src/paw/`` namespace and THROWS on a collision at
#      build time. Checking at create turns a publish-time throw far from the
#      authoring turn into an actionable error, and stops an author from
#      overwriting the prerender contract that keeps the page from shipping blank.
#   3. Registration — the tool id rides the SAME server allowlist as publish +
#      the other four create tools, which is what makes the /sites react-create
#      surface able to CALL the tool its preamble names.
#   4. End-to-end handler — against a real (mongomock) Beanie DB it persists the
#      source map via ``agent_create`` and reads the PERSISTED _PocketDoc back to
#      confirm engine=="react", source==<map>, type=="site", pattern=="landing"
#      (ground truth in Mongo, NOT agent narration), NO rippleSpec, and that
#      ``interactive`` lands as the pocket's ``keeps_client_bundle`` declaration.
#
# The ``interactive`` -> ``keeps_client_bundle`` cases depend on MT-1's
# ``agent_create`` parameter, which reached this branch by merging
# ``feat/sites-keep-client-bundle``. RX-1 and MT-1 were siblings off dev on the
# pocketpaw side while the paw-sites react generator was already stacked on MT-1's
# generator commit — so the flag existed end to end everywhere EXCEPT the Python
# create path. That is the gap this file's end-to-end layer pins shut.
"""Tests for the react-track create tool (create_react_site)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """create_react_site calls the shared Sites plan gate
    (sites.service.require_sites_plan) before agent_create. These tests use
    synthetic workspace ids with no seeded Workspace doc, so default the plan to
    one that unlocks Sites ("go") to exercise the create mechanics. Plan-gate
    denial is covered separately in tests/ee/sites/test_plan_gate.py."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


# A representative React source map (paths -> file contents). Multi-file to prove
# the whole map persists, not just the composition root. Deliberately writes only
# under src/ — everything the generator owns is absent, which is the shape the
# authoring skill produces.
def _sample_source() -> dict[str, str]:
    return {
        "src/App.tsx": (
            "import './index.css';\n"
            "import Hero from './components/Hero';\n\n"
            "export default function App() {\n"
            "  return (\n    <main>\n      <Hero />\n    </main>\n  );\n}\n"
        ),
        "src/components/Hero.tsx": (
            "export default function Hero() {\n"
            "  return (\n"
            '    <section className="hero">\n'
            "      <h1>Care that fits your whole family</h1>\n"
            "    </section>\n"
            "  );\n}\n"
        ),
        "src/index.css": ":root { --ink: #17130f; }\nbody { color: var(--ink); }\n",
    }


# ---------------------------------------------------------------------------
# source-map validation (pure — no identity / Mongo needed)
# ---------------------------------------------------------------------------


class TestSourceMapValidation:
    def test_complete_map_has_no_missing_keys(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_react_keys

        assert _missing_react_keys(_sample_source()) == []

    def test_required_key_is_the_composition_root(self) -> None:
        """Pin the required key so the contract can't silently drift. The
        generated client and server entries BOTH import ``../App`` by that exact
        path, so a map without it resolves nothing and the Vite build dies with a
        Rollup error far from the cause."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import REACT_REQUIRED_KEYS

        assert set(REACT_REQUIRED_KEYS) == {"src/App.tsx"}

    def test_missing_composition_root_is_reported(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_react_keys

        no_root = {k: v for k, v in _sample_source().items() if k != "src/App.tsx"}
        assert "src/App.tsx" in _missing_react_keys(no_root)

    def test_root_only_map_is_valid(self) -> None:
        """A single App.tsx with no separate components or stylesheet is a
        complete react site — the generator owns everything else."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _missing_react_keys

        assert _missing_react_keys({"src/App.tsx": "export default () => <h1>hi</h1>;\n"}) == []


class TestReservedPathRejection:
    """The generator owns the build shell. An author who could overwrite
    ``index.html`` or ``paw-prerender.mjs`` could remove the prerender outlet or
    the pass that fills it, turning the site back into a shell that is blank with
    JavaScript disabled — which is exactly what this engine exists to refuse."""

    def test_clean_map_reserves_nothing(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _reserved_react_keys

        assert _reserved_react_keys(_sample_source()) == []

    @pytest.mark.parametrize(
        "path",
        ["index.html", "package.json", "vite.config.ts", "paw-prerender.mjs"],
    )
    def test_each_build_shell_file_is_rejected(self, path: str) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_create import _reserved_react_keys

        assert _reserved_react_keys({**_sample_source(), path: "x"}) == [path]

    def test_generated_entry_namespace_is_rejected(self) -> None:
        """``src/paw/`` holds the generated client + server entries."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _reserved_react_keys

        hit = "src/paw/entry-client.tsx"
        assert _reserved_react_keys({**_sample_source(), hit: "x"}) == [hit]

    def test_a_sibling_sharing_the_prefix_is_allowed(self) -> None:
        """``src/pawprint.ts`` merely shares the prefix — the guard matches the
        DIRECTORY, not any path starting with those characters. Getting this
        wrong locks an author out of a legitimate filename."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _reserved_react_keys

        assert _reserved_react_keys({**_sample_source(), "src/pawprint.ts": "x"}) == []

    @pytest.mark.parametrize(
        "spelling",
        ["./index.html", "src/./paw/entry-client.tsx", "src\\paw\\entry-client.tsx"],
    )
    def test_path_spelling_does_not_defeat_the_guard(self, spelling: str) -> None:
        """The generator normalizes before it throws; so must this. A guard a
        leading ``./`` or a backslash defeats is not a guard — the author would
        get a create success and a build failure.

        THE MUTATION THAT BREAKS THIS: replace the ``posixpath.normpath`` call in
        ``_reserved_react_keys`` with the raw key. Run: all three spellings pass
        the guard and the assertion below fails."""
        from pocketpaw_ee.agent.mcp_servers.sites_create import _reserved_react_keys

        assert _reserved_react_keys({**_sample_source(), spelling: "x"}) == [spelling]


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            CREATE_REACT_SITE_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert CREATE_REACT_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__create_react_site"
        assert CREATE_REACT_SITE_TOOL_ID in SITES_TOOL_IDS

    def test_create_module_exports_matching_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import CREATE_REACT_SITE_TOOL_ID
        from pocketpaw_ee.agent.mcp_servers.sites_create import (
            CREATE_REACT_SITE_TOOL_ID as CREATE_ID,
        )
        from pocketpaw_ee.agent.mcp_servers.sites_create import SITES_CREATE_TOOL_IDS

        assert CREATE_ID == CREATE_REACT_SITE_TOOL_ID
        assert CREATE_ID in SITES_CREATE_TOOL_IDS

    def test_provider_advertises_react_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import CREATE_REACT_SITE_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert CREATE_REACT_SITE_TOOL_ID in CloudSitesMcpProvider().tool_ids()

    @pytest.mark.asyncio
    async def test_tool_is_registered_on_the_built_server(self) -> None:
        """The id being in a tuple proves nothing about the SERVER. Build the real
        in-process server and ask it to list its tools — otherwise the allowlist
        advertises a name nothing answers to, which is the exact shape of the
        failure pocketpaw/CLAUDE.md's prompt-honesty rule describes: the agent
        would be told to call a tool that is not there, and would improvise.

        THE MUTATION THAT BREAKS THIS: drop ``create_react_site`` from the
        ``tools=[...]`` list in ``sites.py::build_sites_manager_server``. Run: the
        id stays in SITES_TOOL_IDS, every other test in this class still passes,
        and this one fails. (Applied 2026-08-07.)"""
        pytest.importorskip("claude_agent_sdk")
        import mcp.types as mcp_types
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        built = build_sites_manager_server()
        assert built is not None
        _name, server = built
        # create_sdk_mcp_server returns {"type", "name", "instance"}; the instance
        # is a lowlevel mcp Server whose tools/list handler is the only place the
        # registered set actually lives.
        handler = server["instance"].request_handlers[mcp_types.ListToolsRequest]
        result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
        registered = {t.name for t in result.root.tools}
        assert "create_react_site" in registered, (
            f"create_react_site is not on the built server; registered: {sorted(registered)}"
        )


# ---------------------------------------------------------------------------
# End-to-end handler — persist + read back from Mongo (ground truth)
# ---------------------------------------------------------------------------


@pytest.fixture()
def recording_bus():
    """Install a recording EventBus so ``agent_create``'s ``emit(PocketCreated)``
    doesn't raise (the real bus is only wired by ``init_realtime()`` at boot).
    Mirrors ``tests/cloud/conftest.py``, which isn't visible from this package."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


async def _create(source: dict[str, str], **extra) -> dict:
    """Drive the handler with a stubbed identity. Returns the raw MCP response."""
    from bson import ObjectId
    from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

    with (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=str(ObjectId()),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            return_value=str(ObjectId()),
        ),
    ):
        return await sites_create_mcp._create_react_site_handler({"source": source, **extra})


class TestCreateReactSiteEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_react_pocket_with_source_map(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Drive the handler against a real (mongomock) Beanie DB and read the
        persisted _PocketDoc back. Proves a pocket lands with engine=="react",
        source==<map>, type=="site", pattern=="landing" — and NO rippleSpec."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        source = _sample_source()
        out = await _create(source, name="Bright Smile")

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["pocket"]["engine"] == "react"
        pocket_id = body["pocket_id"]
        assert pocket_id

        # Ground truth: read the persisted doc straight from Mongo.
        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.type == "site"
        assert doc.pattern == "landing"
        assert doc.engine == "react"
        assert doc.source == source
        # The react path persists NO rippleSpec.
        assert doc.rippleSpec is None

    @pytest.mark.asyncio
    async def test_interactive_persists_as_keeps_client_bundle(
        self, beanie_test_db, recording_bus
    ) -> None:
        """``interactive=true`` must reach the POCKET as ``keeps_client_bundle``.

        This is the whole point of the argument. The publish path reads
        ``pocket["keepsClientBundle"]`` and threads it to the generator, which
        selects hydrateRoot over strip-the-script. If the create drops it, the
        site builds, deploys, renders correctly — and every onClick is dead.

        THE MUTATION THAT BREAKS THIS: delete ``keeps_client_bundle=interactive``
        from the ``agent_create`` call in ``_create_react_site_handler``. Run: the
        create still succeeds, the engine/source/pattern assertions above still
        pass, and this one fails."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        out = await _create(_sample_source(), name="Interactive", interactive=True)
        assert not out.get("is_error"), out
        pocket_id = json.loads(out["content"][0]["text"])["pocket_id"]

        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.keeps_client_bundle is True

    @pytest.mark.asyncio
    async def test_omitting_interactive_records_no_declaration(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Omitting ``interactive`` must persist ``None``, not ``False``.

        Edited (feat/sites-js-by-default). This test used to assert ``False`` and
        read "a purely static page ships no JavaScript" — that was an assertion
        about the DEFAULT, and the default is what deliberately changed: sites now
        keep their client bundle unless told otherwise. What survives the change
        is the half that was never about the default — the create must not
        FABRICATE a declaration the agent did not make. ``None`` is the honest
        record of "the agent said nothing", and it is what lets publish apply
        ``sites_keep_client_bundle_default`` here while still obeying an explicit
        ``False`` (see the sibling test below).

        THE MUTATION THAT BREAKS THIS: restore the old
        ``interactive = bool(args.get("interactive"))`` coercion in
        ``_create_react_site_handler``. The create still succeeds and every other
        assertion in this class still passes — but the omitted case is recorded as
        a decision, and no react site that skips the argument can ever pick up the
        default."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        out = await _create(_sample_source(), name="Static")
        assert not out.get("is_error"), out
        pocket_id = json.loads(out["content"][0]["text"])["pocket_id"]

        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.keeps_client_bundle is None

    @pytest.mark.asyncio
    async def test_explicit_false_is_recorded_as_an_opt_out(
        self, beanie_test_db, recording_bus
    ) -> None:
        """``interactive=False`` is a real decision and must persist as ``False``.

        This is the opt-out path, and it is the half of the tri-state that keeps
        the new default from being a mandate: a brochure page that has no use for
        a hydration bundle can still refuse one. Distinct from the test above —
        the two inputs differ only in whether the argument was PASSED, and they
        must land on different stored values."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        out = await _create(_sample_source(), name="Opted out", interactive=False)
        assert not out.get("is_error"), out
        pocket_id = json.loads(out["content"][0]["text"])["pocket_id"]

        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.keeps_client_bundle is False

    @pytest.mark.asyncio
    async def test_wire_dict_carries_the_declaration_for_publish(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Publish does not read the Beanie doc — ``publish_pocket`` reads the
        pocket WIRE dict, keyed ``keepsClientBundle`` (camelCase). Persisting the
        field but failing to surface it on the wire would be just as silent as not
        persisting it, so assert the shape publish actually consumes."""
        from bson import ObjectId
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
        from pocketpaw_ee.cloud.pockets.dto import pocket_to_wire_dict
        from pocketpaw_ee.cloud.pockets.service import _pocket_to_domain

        out = await _create(_sample_source(), name="Wire", interactive=True)
        assert not out.get("is_error"), out
        pocket_id = json.loads(out["content"][0]["text"])["pocket_id"]

        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        wire = pocket_to_wire_dict(_pocket_to_domain(doc))
        assert wire.get("engine") == "react"
        assert wire.get("keepsClientBundle") is True

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=None,
            ),
        ):
            out = await sites_create_mcp._create_react_site_handler({"source": _sample_source()})

        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_source_is_error(self) -> None:
        out = await _create({})
        assert out.get("is_error") is True
        assert "requires a `source` object" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_composition_root_is_error(self) -> None:
        source = {k: v for k, v in _sample_source().items() if k != "src/App.tsx"}
        out = await _create(source)
        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "src/App.tsx" in text

    @pytest.mark.asyncio
    async def test_reserved_path_is_error_and_names_the_path(self) -> None:
        out = await _create({**_sample_source(), "paw-prerender.mjs": "// mine now"})
        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "paw-prerender.mjs" in text
        assert "generator-owned" in text

    @pytest.mark.asyncio
    async def test_non_string_file_value_is_error(self) -> None:
        out = await _create({**_sample_source(), "src/data.json": {"not": "a string"}})  # type: ignore[dict-item]
        assert out.get("is_error") is True
        assert "content strings" in out["content"][0]["text"]
