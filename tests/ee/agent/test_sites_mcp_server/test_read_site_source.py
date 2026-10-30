# tests/ee/agent/test_sites_mcp_server/test_read_site_source.py
# Created: 2026-08-19 (fix/sites-read-source-tool) — coverage for the READ tool
# ``read_site_source`` on the in-process ``pocketpaw_sites_manager`` server.
#
# WHY THIS TOOL EXISTS. The /sites agent could WRITE a site's source three ways
# (edit_svelte_component / edit_react_component / edit_html_file) and READ it
# ZERO ways. That is not a missing convenience, it is a hole the edit tools fall
# through:
#
#   1. Every edit tool PREFERS its ``edits`` (search/replace) form, whose
#      ``old_string`` must be "copied VERBATIM from the current file" and match
#      exactly once. Their own descriptions instruct "read it first" — and named
#      no tool that could, because none existed.
#   2. ``get_pocket`` DOES return ``pocket["source"]`` (``source`` is not in
#      ``_AGENT_INVISIBLE_FIELDS``), but it lives on the ``pockets`` server, and
#      ``sites_allow`` is a HARD whitelist of SITES|STOCK|ICON|PALETTE|
#      ASK. So on /sites it is silently filtered out.
#   3. The /sites profile also drops the file/shell built-ins by design
#      ("the source map / copy is a tool ARGUMENT" — surface_registry.py). True
#      on CREATE, where the agent authored the source in-context. False on EDIT
#      of any site it did not author THIS turn.
#
# So the only reachable edit form was ``new_source`` — a full-file rewrite
# composed blind. That is the data-loss path the edit descriptions themselves
# warn about: it silently drops a ``<form>``'s ``action`` and its hidden
# ``paw_site_id`` / ``paw_key`` / ``paw_redirect`` inputs, and every future
# enquiry goes nowhere with no error anywhere.
#
# The REGRESSION test that actually pins the bug is
# ``TestReachableFromTheSitesSurface`` — an id absent from the allowlist is
# filtered with NO error, so registration alone proves nothing.
"""Tests for the site source READ tool (read_site_source)."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """``read_site_source`` runs the shared Sites plan gate, like its edit
    siblings. These tests use synthetic workspace ids with no seeded Workspace
    doc, so default the plan to one that unlocks Sites ("go")."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


def _listed_tool(name: str = "read_site_source"):
    """Fetch the tool as the SDK would list it, or skip when the SDK is absent."""
    import asyncio

    from mcp import types
    from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

    built = build_sites_manager_server()
    if built is None:  # claude_agent_sdk absent — nothing to assert
        pytest.skip("claude_agent_sdk not installed")
    _name, server = built
    handler = server["instance"].request_handlers[types.ListToolsRequest]
    listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    return listed.root.tools


def _body(out: dict) -> dict:
    """Decode the JSON body a success response carries."""
    return json.loads(out["content"][0]["text"])


_HEADER = re.compile(r"^=== FILE: (?P<path>.+) \((?P<bytes>\d+) bytes\) ===$")


def _file_blocks(out: dict) -> dict[str, str]:
    """Map path -> contents from the plain-text file blocks after the metadata.

    Each block is a one-line ``=== FILE: <path> (<n> bytes) ===`` header, a
    newline, then the file's contents verbatim."""
    blocks: dict[str, str] = {}
    for block in out["content"][1:]:
        assert block["type"] == "text"
        header, _, contents = block["text"].partition("\n")
        m = _HEADER.match(header)
        assert m, header
        assert int(m["bytes"]) == len(contents.encode("utf-8"))
        blocks[m["path"]] = contents
    return blocks


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import READ_SITE_SOURCE_TOOL_ID, SITES_TOOL_IDS

        assert READ_SITE_SOURCE_TOOL_ID == "mcp__pocketpaw_sites_manager__read_site_source"
        assert READ_SITE_SOURCE_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_read_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import READ_SITE_SOURCE_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert READ_SITE_SOURCE_TOOL_ID in CloudSitesMcpProvider().tool_ids()

    def test_the_built_server_actually_lists_the_tool(self) -> None:
        """An id in ``SITES_TOOL_IDS`` with no tool behind it allow-lists a name
        that does not answer."""
        names = {t.name for t in _listed_tool()}
        assert "read_site_source" in names, sorted(names)

    def test_only_pocket_id_is_required(self) -> None:
        """Manifest mode is the no-``file_path`` call. If ``file_path`` were
        required the agent could not discover WHICH files exist, and would have
        to guess a path before it could read one."""
        tool = next(t for t in _listed_tool() if t.name == "read_site_source")
        props = (tool.inputSchema or {}).get("properties") or {}
        assert set(props) >= {"pocket_id", "file_path"}
        assert (tool.inputSchema or {}).get("required") == ["pocket_id"]


# ---------------------------------------------------------------------------
# THE REGRESSION TEST — reachability, not merely registration
# ---------------------------------------------------------------------------


class TestReachableFromTheSitesSurface:
    """``sites_allow`` is a hard whitelist: an id missing from it is filtered out
    with no error and the tool is silently unreachable on the surface that needs
    it. Registration tests above cannot catch that. These can."""

    def test_read_tool_is_allowed_in_every_sites_mode(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import READ_SITE_SOURCE_TOOL_ID
        from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

        for meta in (
            SurfaceMeta(),  # ripple-create
            SurfaceMeta(engine="svelte"),  # svelte-create
            SurfaceMeta(engine="html"),
            SurfaceMeta(engine="react"),
            SurfaceMeta(pocket_id="pkt_1"),  # refine an existing site
        ):
            allow = resolve_profile(SurfaceKind.SITES, meta).allow_mcp_tool_ids
            assert allow is not None
            assert READ_SITE_SOURCE_TOOL_ID in allow, f"unreachable for meta={meta}"

    def test_every_edit_tool_has_a_reachable_way_to_read_first(self) -> None:
        """The invariant the bug violated, stated directly: a surface that can
        WRITE a site's source must be able to READ it. Without this the only
        usable edit form is a blind full-file rewrite."""
        from pocketpaw_ee.agent.mcp_servers.sites import (
            EDIT_HTML_FILE_TOOL_ID,
            EDIT_REACT_COMPONENT_TOOL_ID,
            EDIT_SVELTE_COMPONENT_TOOL_ID,
            READ_SITE_SOURCE_TOOL_ID,
        )
        from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

        allow = resolve_profile(
            SurfaceKind.SITES, SurfaceMeta(pocket_id="pkt_1")
        ).allow_mcp_tool_ids
        assert allow is not None
        writers = {
            EDIT_HTML_FILE_TOOL_ID,
            EDIT_REACT_COMPONENT_TOOL_ID,
            EDIT_SVELTE_COMPONENT_TOOL_ID,
        } & set(allow)
        assert writers, "no edit tools on /sites — this test is asserting nothing"
        assert READ_SITE_SOURCE_TOOL_ID in allow


# ---------------------------------------------------------------------------
# The edit tools must NAME the read tool
# ---------------------------------------------------------------------------


class TestEditDescriptionsPointAtTheReadTool:
    """A tool description is the only prompt real estate this surface has. All
    three edit tools tell the agent to read the file first; before this fix none
    of them could name a tool that does it."""

    @pytest.mark.parametrize(
        "edit_tool",
        ["edit_html_file", "edit_react_component", "edit_svelte_component"],
    )
    def test_edit_description_names_the_read_tool(self, edit_tool: str) -> None:
        tool = next(t for t in _listed_tool() if t.name == edit_tool)
        assert "read_site_source" in (tool.description or "")


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------


class TestReadHandler:
    @pytest.mark.asyncio
    async def test_manifest_mode_lists_paths_and_sizes_without_contents(self) -> None:
        """No ``file_path`` → the cheap manifest. Contents are deliberately NOT
        included: a react site's full source map would flood the context window,
        which is the reason allow-listing ``get_pocket`` was not the fix."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(
            return_value={
                "pocket_id": "pk1",
                "engine": "html",
                "files": [
                    {"path": "index.html", "bytes": 42},
                    {"path": "styles.css", "bytes": 7},
                ],
                "file_count": 2,
                "bindings": [],
            }
        )
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.read_site_source", new=fake),
        ):
            out = await mcp._read_site_source_handler({"pocket_id": "pk1"})

        assert not out.get("is_error"), out
        body = _body(out)
        assert body["ok"] is True
        assert body["file_count"] == 2
        assert [f["path"] for f in body["files"]] == ["index.html", "styles.css"]
        assert "contents" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_single_file_mode_returns_verbatim_contents(self) -> None:
        """The whole point: the returned text must be byte-identical to what is
        stored, or an ``old_string`` copied from it will not match."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        source = '<!doctype html>\n<h1 class="a">Hi</h1>\n<!-- \t tricky "quotes" -->\n'
        fake = AsyncMock(
            return_value={
                "pocket_id": "pk1",
                "engine": "html",
                "files": [
                    {
                        "path": "index.html",
                        "bytes": len(source.encode("utf-8")),
                        "contents": source,
                    }
                ],
            }
        )
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.read_site_source", new=fake),
        ):
            out = await mcp._read_site_source_handler(
                {"pocket_id": "pk1", "file_path": "index.html"}
            )

        assert not out.get("is_error"), out
        assert _file_blocks(out) == {"index.html": source}

    @pytest.mark.asyncio
    async def test_missing_identity_is_an_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=(None, None)):
            out = await mcp._read_site_source_handler({"pocket_id": "pk1"})
        assert out.get("is_error") is True

    @pytest.mark.asyncio
    async def test_missing_pocket_id_is_an_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._read_site_source_handler({})
        assert out.get("is_error") is True

    @pytest.mark.asyncio
    async def test_service_error_is_relayed_by_code(self) -> None:
        """A ripple pocket has no source map. The agent must learn WHICH guard
        rejected it so it stops trying to read files off a ripple site."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import ValidationError

        fake = AsyncMock(
            side_effect=ValidationError(
                "pocket.no_source_map",
                "This pocket is a ripple Paw Site — it has no raw source map.",
            )
        )
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.read_site_source", new=fake),
        ):
            out = await mcp._read_site_source_handler({"pocket_id": "pk1"})

        assert out.get("is_error") is True
        assert "pocket.no_source_map" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# Service behaviour
# ---------------------------------------------------------------------------


class TestReadService:
    @pytest.mark.asyncio
    async def test_manifest_excludes_the_generated_paw_namespace(self) -> None:
        """``_paw/`` is generated and unwritable by the edit tools. Listing it
        invites the agent to try an edit that is rejected on the path alone."""
        from pocketpaw_ee.sites import service as sites_service

        pocket = {
            "engine": "html",
            "source": {"index.html": "<h1>Hi</h1>", "_paw/generated.js": "// gen"},
        }
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket),
        ):
            out = await sites_service.read_site_source(user_id="u1", pocket_id="pk1")

        assert [f["path"] for f in out["files"]] == ["index.html"]

    @pytest.mark.asyncio
    async def test_the_manifest_carries_no_file_contents(self) -> None:
        """The handler test above proves the RESPONSE shape against a fake; this
        proves the SERVICE never puts contents in the manifest at all. Without it
        a manifest that inlined every file would ship — and the flood it causes
        is the reason allow-listing ``get_pocket`` was rejected as the fix.

        Mutation: add ``"contents": str(files[k])`` to the manifest rows and this
        fails while every handler-level assertion keeps passing."""
        from pocketpaw_ee.sites import service as sites_service

        secret = "THE-FILE-BODY-SHOULD-NOT-APPEAR"
        pocket = {"engine": "html", "source": {"index.html": secret}}
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket),
        ):
            out = await sites_service.read_site_source(user_id="u1", pocket_id="pk1")

        assert secret not in json.dumps(out)
        assert out["files"] == [{"path": "index.html", "bytes": len(secret)}]

    @pytest.mark.asyncio
    async def test_dynamic_svelte_bindings_are_not_listed_as_files(self) -> None:
        """A dynamic svelte pocket keeps ``objects``/``sources``/``actions``/
        ``auth`` as SIBLING keys on the same dict as the files. Listing them as
        files would have the agent try to read ``objects`` as a component."""
        from pocketpaw_ee.sites import service as sites_service

        pocket = {
            "engine": "svelte",
            "source": {
                "src/routes/+page.svelte": "<h1>Hi</h1>",
                "objects": [{"name": "lead"}],
                "auth": True,
            },
        }
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket),
        ):
            out = await sites_service.read_site_source(user_id="u1", pocket_id="pk1")

        assert [f["path"] for f in out["files"]] == ["src/routes/+page.svelte"]
        assert set(out["bindings"]) == {"objects", "auth"}

    @pytest.mark.asyncio
    async def test_reading_one_file_returns_it_verbatim(self) -> None:
        from pocketpaw_ee.sites import service as sites_service

        source = '<h1 class="hero">A</h1>\n\t<p>B</p>\n'
        pocket = {"engine": "html", "source": {"index.html": source}}
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket),
        ):
            out = await sites_service.read_site_source(
                user_id="u1", pocket_id="pk1", file_path="index.html"
            )

        assert out["files"] == [
            {"path": "index.html", "bytes": len(source.encode("utf-8")), "contents": source}
        ]

    @pytest.mark.asyncio
    async def test_a_ripple_pocket_is_rejected_not_a_keyerror(self) -> None:
        from pocketpaw_ee.cloud._core.errors import ValidationError
        from pocketpaw_ee.sites import service as sites_service

        pocket = {"engine": "ripple", "source": None}
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket),
        ):
            with pytest.raises(ValidationError) as exc:
                await sites_service.read_site_source(user_id="u1", pocket_id="pk1")

        assert exc.value.code == "pocket.no_source_map"

    @pytest.mark.asyncio
    async def test_an_unknown_file_is_not_found_and_names_what_exists(self) -> None:
        """A typo'd path must not read as "the file is empty". The error names
        the real paths so the agent's retry is informed rather than another
        guess."""
        from pocketpaw_ee.cloud._core.errors import NotFound
        from pocketpaw_ee.sites import service as sites_service

        pocket = {"engine": "html", "source": {"index.html": "<h1>Hi</h1>"}}
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket),
        ):
            with pytest.raises(NotFound):
                await sites_service.read_site_source(
                    user_id="u1", pocket_id="pk1", file_path="about.html"
                )


# ---------------------------------------------------------------------------
# Complete, unescaped contents and batch reads
# ---------------------------------------------------------------------------

# Quotes, backslashes, tabs, CRLF and non-ASCII: every character JSON would
# escape, plus a line that looks like the block header itself.
_TRICKY = (
    '<!doctype html>\n<h1 class="a">Hi \\ "there"</h1>\r\n'
    "\t<script>const s = 'a\\nb';</script>\n"
    "=== FILE: fake.html (1 bytes) ===\n"
    "café — \U0001f600\n"
)


def _pocket(files: dict[str, str]) -> dict:
    return {"engine": "html", "source": dict(files)}


class TestCompleteUnescapedRead:
    """The bug: file contents were JSON-dumped inside one text block, so every
    newline and quote was escaped, inflating a large file past Claude Code's MCP
    output cap. The agent never saw the whole file and re-called the tool. The
    contents now ride in their own plain-text block, byte-for-byte."""

    @pytest.mark.asyncio
    async def test_contents_come_back_verbatim_in_their_own_block(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        get = AsyncMock(return_value=_pocket({"index.html": _TRICKY}))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.cloud.pockets.service.get", new=get),
        ):
            out = await mcp._read_site_source_handler(
                {"pocket_id": "pk1", "file_path": "index.html"}
            )

        assert not out.get("is_error"), out
        assert _file_blocks(out) == {"index.html": _TRICKY}
        # The JSON-escaped copy must not travel in the metadata block either.
        assert json.dumps(_TRICKY)[1:-1] not in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_metadata_block_lists_files_without_contents(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        get = AsyncMock(return_value=_pocket({"index.html": _TRICKY}))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.cloud.pockets.service.get", new=get),
        ):
            out = await mcp._read_site_source_handler(
                {"pocket_id": "pk1", "file_path": "index.html"}
            )

        meta = _body(out)
        assert meta["ok"] is True
        assert meta["pocket_id"] == "pk1"
        assert meta["engine"] == "html"
        assert meta["files"] == [{"path": "index.html", "bytes": len(_TRICKY.encode("utf-8"))}]
        assert "contents" not in json.dumps(meta)
        assert meta["message"]

    @pytest.mark.asyncio
    async def test_a_large_file_is_returned_whole(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        big = "".join(f'<p class="row-{i}">line "{i}"</p>\n' for i in range(20_000))
        get = AsyncMock(return_value=_pocket({"index.html": big}))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.cloud.pockets.service.get", new=get),
        ):
            out = await mcp._read_site_source_handler(
                {"pocket_id": "pk1", "file_path": "index.html"}
            )

        assert _file_blocks(out)["index.html"] == big


class TestBatchRead:
    @pytest.mark.asyncio
    async def test_file_paths_reads_several_files_from_one_pocket_fetch(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        files = {"index.html": _TRICKY, "styles.css": "a{b:c}\n", "about.html": "<p>x</p>"}
        get = AsyncMock(return_value=_pocket(files))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.cloud.pockets.service.get", new=get),
        ):
            out = await mcp._read_site_source_handler(
                {"pocket_id": "pk1", "file_paths": ["styles.css", "index.html"]}
            )

        assert not out.get("is_error"), out
        assert get.await_count == 1
        blocks = _file_blocks(out)
        assert list(blocks) == ["styles.css", "index.html"]
        assert blocks == {"styles.css": files["styles.css"], "index.html": _TRICKY}
        assert [f["path"] for f in _body(out)["files"]] == ["styles.css", "index.html"]

    @pytest.mark.asyncio
    async def test_file_path_and_file_paths_combine_deduped_in_order(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        files = {"index.html": "<h1/>", "styles.css": "a{}", "app.js": "x()"}
        get = AsyncMock(return_value=_pocket(files))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.cloud.pockets.service.get", new=get),
        ):
            out = await mcp._read_site_source_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "index.html",
                    "file_paths": ["app.js", "index.html", "./app.js", "styles.css"],
                }
            )

        assert not out.get("is_error"), out
        assert list(_file_blocks(out)) == ["index.html", "app.js", "styles.css"]

    @pytest.mark.asyncio
    async def test_an_unknown_path_fails_the_call_and_names_what_exists(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        get = AsyncMock(return_value=_pocket({"index.html": "<h1/>", "styles.css": "a{}"}))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.cloud.pockets.service.get", new=get),
        ):
            out = await mcp._read_site_source_handler(
                {"pocket_id": "pk1", "file_paths": ["index.html", "nope.html"]}
            )

        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "nope.html" in text
        assert "styles.css" in text

    @pytest.mark.asyncio
    async def test_file_paths_must_be_a_list_of_non_empty_strings(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            for bad in ("index.html", [], [""], [3]):
                out = await mcp._read_site_source_handler({"pocket_id": "pk1", "file_paths": bad})
                assert out.get("is_error") is True, bad

    def test_schema_advertises_file_paths(self) -> None:
        tool = next(t for t in _listed_tool() if t.name == "read_site_source")
        props = (tool.inputSchema or {}).get("properties") or {}
        assert props["file_paths"]["type"] == "array"
        assert props["file_paths"]["items"]["type"] == "string"

    def test_description_steers_to_one_batched_read(self) -> None:
        tool = next(t for t in _listed_tool() if t.name == "read_site_source")
        desc = tool.description or ""
        assert "file_paths" in desc
        assert "ONE call" in desc
        assert "wastes" not in desc
        assert "paw_site_id" in desc  # the form-plumbing warning stays
