# tests/ee/agent/test_palette_mcp_server/test_mcp_tool.py
# Created: 2026-07-06 (feat/sites-crew-palette, SC-7) — coverage for the
# in-process ``pocketpaw_palette`` MCP server. Mirrors the icons / stock_images
# test layout: registration assertions (server name, tool id namespacing, build
# shape, provider allowlist publication), pure-function tests for
# ``scale_from_base`` (full ordered scale, NO network), and per-handler tests
# that build a SYNTHETIC known-color PIL image in-process and inject an
# httpx.MockTransport (NO live network) — happy path, empty/non-http url,
# download error, and unreadable-bytes fail-soft.
"""MCP server registration + handler tests for the palette-extraction tool."""

from __future__ import annotations

import json
from io import BytesIO

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("PIL")

from PIL import Image  # noqa: E402
from pocketpaw_ee.agent.mcp_servers import palette as palette_mcp  # noqa: E402,I001

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payload(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``. Decode it so
    the tests can assert on dict fields without re-encoding."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


def _mock_transport(handler) -> httpx.MockTransport:
    """Wrap a request handler in an httpx.MockTransport for injection into
    ``palette._TRANSPORT`` so the image fetch never hits the network."""
    return httpx.MockTransport(handler)


def _synthetic_png() -> bytes:
    """Build a tiny known-color PNG in-process: four quadrants of distinct hues
    (red / green / blue) plus a gray, so extraction yields all four roles with
    no network at all."""
    img = Image.new("RGB", (64, 64))
    px = img.load()
    for x in range(64):
        for y in range(64):
            if x < 32 and y < 32:
                px[x, y] = (200, 30, 30)  # red
            elif x >= 32 and y < 32:
                px[x, y] = (30, 170, 60)  # green
            elif x < 32 and y >= 32:
                px[x, y] = (40, 60, 200)  # blue
            else:
                px[x, y] = (128, 128, 128)  # neutral gray
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def restore_transport():
    """Ensure the module-level transport seam is reset after each test."""
    original = palette_mcp._TRANSPORT
    yield
    palette_mcp._TRANSPORT = original


_ORDERED_STEPS = ["50", "100", "200", "300", "400", "500", "600", "700", "800", "900"]


def _relative_luminance(hex_color: str) -> float:
    r, g, b = palette_mcp._hex_to_rgb(hex_color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestPaletteMcpServerRegistration:
    def test_server_name_and_tool_id_namespacing(self) -> None:
        assert palette_mcp.SERVER_NAME == "pocketpaw_palette"
        # The tool id must use the exact ``mcp__<server>__<tool>`` form so the
        # Claude Code allowlist machinery matches it.
        assert palette_mcp.EXTRACT_PALETTE_TOOL_ID == "mcp__pocketpaw_palette__extract_palette"
        # SC-7c added the custom-color tool; the allowlist now carries both ids.
        assert palette_mcp.PALETTE_TOOL_IDS == (
            palette_mcp.EXTRACT_PALETTE_TOOL_ID,
            palette_mcp.SCALE_FROM_COLOR_TOOL_ID,
        )

    def test_extension_provider_advertises_tool_id(self) -> None:
        """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
        allowlist loop — the extract tool id must come through it."""
        from pocketpaw_ee.extensions import CloudPaletteMcpProvider

        advertised = CloudPaletteMcpProvider().tool_ids()
        assert list(palette_mcp.PALETTE_TOOL_IDS) == advertised

    def test_provider_build_server_matches_shape(self) -> None:
        """The provider's ``build_server`` returns ``(name, server)`` when the
        Claude Agent SDK is installed (the ee group), or ``None`` otherwise."""
        from pocketpaw_ee.extensions import CloudPaletteMcpProvider

        out = CloudPaletteMcpProvider().build_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_palette"
            assert server is not None

    def test_build_server_returns_object(self) -> None:
        out = palette_mcp.build_palette_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_palette"
            assert server is not None

    def test_provider_is_ambient_not_opt_in(self) -> None:
        """The palette server must NOT be opt-in — otherwise the bundled site
        skill couldn't reach it without an explicit per-agent opt-in."""
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

        assert "pocketpaw_palette" not in OPT_IN_MCP_SERVERS


# ---------------------------------------------------------------------------
# Pure helper — scale_from_base (no network, no PIL)
# ---------------------------------------------------------------------------


class TestScaleFromBase:
    def test_full_scale_all_steps_present(self) -> None:
        scale = palette_mcp.scale_from_base("#4A7C59")
        assert list(scale.keys()) == _ORDERED_STEPS

    def test_every_step_is_valid_hex(self) -> None:
        scale = palette_mcp.scale_from_base("#4A7C59")
        for step, value in scale.items():
            assert value.startswith("#") and len(value) == 7, (step, value)
            # round-trips through the parser without raising
            palette_mcp._hex_to_rgb(value)

    def test_scale_is_monotonic_light_to_dark(self) -> None:
        """50 is the lightest, 900 the darkest, strictly decreasing luminance."""
        scale = palette_mcp.scale_from_base("#4A7C59")
        lums = [_relative_luminance(scale[step]) for step in _ORDERED_STEPS]
        assert lums[0] == max(lums)  # 50 lightest
        assert lums[-1] == min(lums)  # 900 darkest
        for lighter, darker in zip(lums, lums[1:]):
            assert lighter > darker

    def test_accepts_hex_without_hash_and_shorthand(self) -> None:
        assert set(palette_mcp.scale_from_base("4A7C59").keys()) == set(_ORDERED_STEPS)
        assert set(palette_mcp.scale_from_base("#abc").keys()) == set(_ORDERED_STEPS)

    def test_grayscale_base_still_produces_ordered_scale(self) -> None:
        scale = palette_mcp.scale_from_base("#808080")
        lums = [_relative_luminance(scale[step]) for step in _ORDERED_STEPS]
        for lighter, darker in zip(lums, lums[1:]):
            assert lighter > darker


# ---------------------------------------------------------------------------
# Handler — extract_palette
# ---------------------------------------------------------------------------


class TestExtractPaletteHandler:
    @pytest.mark.asyncio
    async def test_happy_path_synthetic_image_yields_role_scales(self, restore_transport) -> None:
        """A successful extract on a synthetic known-color image returns all four
        roles, each a full 50–900 scale of valid hex strings — no network."""
        captured: dict = {}
        png = _synthetic_png()

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})

        palette_mcp._TRANSPORT = _mock_transport(handler)

        out = await palette_mcp._extract_handler({"image_url": "https://cdn.example.com/hero.png"})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["ok"] is True
        palette = body["palette"]
        assert set(palette.keys()) == {"primary", "secondary", "tertiary", "neutral"}
        for role, scale in palette.items():
            assert list(scale.keys()) == _ORDERED_STEPS, role
            for value in scale.values():
                assert value.startswith("#") and len(value) == 7
            lums = [_relative_luminance(scale[s]) for s in _ORDERED_STEPS]
            assert lums[0] == max(lums) and lums[-1] == min(lums), role
        # the image was actually fetched from the provided url
        assert captured["url"] == "https://cdn.example.com/hero.png"

    @pytest.mark.asyncio
    async def test_empty_url_returns_error_and_never_fetches(self, restore_transport) -> None:
        called = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            called["hit"] = True
            return httpx.Response(200, content=b"")

        palette_mcp._TRANSPORT = _mock_transport(handler)

        out = await palette_mcp._extract_handler({"image_url": "   "})

        assert out.get("is_error") is True
        assert "non-empty" in out["content"][0]["text"]
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self, restore_transport) -> None:
        out = await palette_mcp._extract_handler({})
        assert out.get("is_error") is True
        assert "image_url" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_non_http_url_returns_error_and_never_fetches(self, restore_transport) -> None:
        called = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            called["hit"] = True
            return httpx.Response(200, content=b"")

        palette_mcp._TRANSPORT = _mock_transport(handler)

        out = await palette_mcp._extract_handler({"image_url": "ftp://example.com/x.png"})

        assert out.get("is_error") is True
        assert "http(s)" in out["content"][0]["text"]
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_download_http_error_soft_fails(self, restore_transport) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream unavailable")

        palette_mcp._TRANSPORT = _mock_transport(handler)

        out = await palette_mcp._extract_handler({"image_url": "https://x.example/hero.png"})

        assert out.get("is_error") is True
        assert "image download error" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_transport_exception_soft_fails(self, restore_transport) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        palette_mcp._TRANSPORT = _mock_transport(handler)

        out = await palette_mcp._extract_handler({"image_url": "https://x.example/hero.png"})

        assert out.get("is_error") is True
        assert "image download error" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_unreadable_bytes_soft_fails(self, restore_transport) -> None:
        """Bytes PIL can't open (not an image) return a soft error, never raise."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"this is definitely not an image")

        palette_mcp._TRANSPORT = _mock_transport(handler)

        out = await palette_mcp._extract_handler({"image_url": "https://x.example/notimg.txt"})

        assert out.get("is_error") is True
        assert "palette extraction failed" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# scale_from_color — the custom-color path (SC-7c): a single brand hex → scale.
# Pure math, no network.
# ---------------------------------------------------------------------------

_STEPS = ["50", "100", "200", "300", "400", "500", "600", "700", "800", "900"]


async def test_scale_from_color_valid_hex_full_scale() -> None:
    """A valid hex returns a full 50–900 scale under the default 'primary' role."""
    env = await palette_mcp._scale_from_color_handler({"hex": "#6B21A8"})
    assert "is_error" not in env
    body = _decode_payload(env)
    assert body["ok"] is True
    assert body["role"] == "primary"
    assert list(body["scale"].keys()) == _STEPS
    assert all(v.startswith("#") and len(v) == 7 for v in body["scale"].values())


async def test_scale_from_color_custom_role_label() -> None:
    """An explicit role is echoed back on the scale."""
    body = _decode_payload(
        await palette_mcp._scale_from_color_handler({"hex": "#0A84FF", "role": "accent"})
    )
    assert body["role"] == "accent"
    assert body["scale"]["500"].startswith("#")


async def test_scale_from_color_shorthand_hex() -> None:
    """#RGB shorthand is accepted (mirrors scale_from_base)."""
    body = _decode_payload(await palette_mcp._scale_from_color_handler({"hex": "#f30"}))
    assert body["ok"] is True
    assert len(body["scale"]) == 10


async def test_scale_from_color_bad_hex_soft_error() -> None:
    """A malformed hex fails soft — an error envelope, never a raise."""
    env = await palette_mcp._scale_from_color_handler({"hex": "not-a-color"})
    assert env["is_error"] is True


async def test_scale_from_color_missing_hex_soft_error() -> None:
    """Empty/missing hex returns an error envelope."""
    assert (await palette_mcp._scale_from_color_handler({}))["is_error"] is True
    assert (await palette_mcp._scale_from_color_handler({"hex": "  "}))["is_error"] is True


def test_scale_from_color_tool_id_published() -> None:
    """The new tool id namespaces correctly and joins the allowlist tuple."""
    assert palette_mcp.SCALE_FROM_COLOR_TOOL_ID == "mcp__pocketpaw_palette__scale_from_color"
    assert palette_mcp.SCALE_FROM_COLOR_TOOL_ID in palette_mcp.PALETTE_TOOL_IDS
    assert len(palette_mcp.PALETTE_TOOL_IDS) == 2
