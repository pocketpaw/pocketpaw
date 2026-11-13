# sites_reference.py — the ``save_site_reference`` and ``compare_site_to_reference``
# tools on the ``pocketpaw_sites_manager`` MCP server. The owner supplies a design
# (a screenshot or mockup attached in chat); the agent stores it on the pocket, then
# compares its draft against it at the same width and reads back side-by-side tiles,
# a diff heatmap, the top mismatch regions and a score per tile. The
# ``sites-reference-match`` skill drives the loop; the image work lives in
# ``sites/site_references.py``.
#
# Invariants a reader must not break:
#   * ``save_site_reference`` never fetches an arbitrary URL. It reads this
#     workspace's own public site assets back from storage (where a chat
#     attachment on /sites lands), or streams from Refero's or Figma's image
#     hosts, matched exactly, under a size cap and a total deadline. Every other host is refused
#     before any request is made;
#   * the stored file is a re-encoded PNG on the PRIVATE adapter, keyed by
#     workspace and pocket, and the pocket is opened as the caller first, so a
#     reference can only be saved to or read from a pocket the user can see;
#   * the draft is photographed through ``sites_create._capture_draft``, the same
#     path as ``preview_site``: only the pocket's own draft, plus a validated path;
#   * identity, ``record_tool_call`` and the sites plan gate run first, and failures
#     are honest: no screenshot means "say the comparison did not run", never "the
#     site is broken".
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pocketpaw_ee.sites import site_references as refs
from pocketpaw_ee.sites.figma.client import FIGMA_IMAGE_HOSTS

from ._audit import record_tool_call
from .sites_create import (
    _capture_draft,
    _error_response,
    _identity,
    _preview_path,
    _preview_wait_ms,
    _require_sites_plan_or_error,
    _with_previous_verification,
)

logger = logging.getLogger(__name__)


#: Hosts a reference may be FETCHED from, matched EXACTLY (no subdomains): Refero's
#: image CDN and Figma's render hosts (``figma_screenshot`` saves through here).
#: Inspo's captures live on a shared Vercel Blob domain where any customer can mint
#: a ``<store>.public.blob.vercel-storage.com`` host, so it is not here: an owner's
#: design arrives as a chat attachment, not a research capture.
_FETCH_HOSTS = frozenset({"images.refero.design"}) | FIGMA_IMAGE_HOSTS
#: Fetch limits: a total deadline for the whole request, not a per-read timeout.
_FETCH_DEADLINE_S = 20.0
#: Tests inject an ``httpx.MockTransport`` here.
_TRANSPORT: Any = None

_REFUSED_SOURCE = (
    "`image_url` must be the PUBLIC URL of an image the owner attached in this chat or "
    "uploaded to this site, or a Refero screenshot. Other hosts are refused. "
    "If the owner's image has no public URL here, say the comparison is unavailable "
    "and compare by eye with preview_site."
)


def _fetch_allowed(url: str) -> bool:
    """HTTPS and a host EXACTLY in ``_FETCH_HOSTS``."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _FETCH_HOSTS


async def _fetch(url: str) -> bytes:
    """Stream an image from an exact-match host, refusing past the size cap.

    A declared ``Content-Length`` over the cap is refused before the body is read,
    and the body is abandoned as soon as it passes the cap, so a hostile or broken
    host can never make this buffer more than the cap. The whole request runs under
    one deadline. Redirects are not followed."""
    import httpx

    cap = refs.MAX_REFERENCE_BYTES
    kwargs: dict[str, Any] = {"timeout": _FETCH_DEADLINE_S, "follow_redirects": False}
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    buf = bytearray()
    try:
        async with asyncio.timeout(_FETCH_DEADLINE_S):
            async with httpx.AsyncClient(**kwargs) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise refs.SiteReferenceError(
                            f"the image fetch returned HTTP {response.status_code}."
                        )
                    if not response.headers.get("content-type", "").startswith("image/"):
                        raise refs.SiteReferenceError("the URL did not return an image.")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > cap:
                        raise refs.SiteReferenceError("the image is too large to read.")
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > cap:
                            raise refs.SiteReferenceError("the image is too large to read.")
    except TimeoutError as exc:
        raise refs.SiteReferenceError("the image fetch took too long.") from exc
    except httpx.HTTPError as exc:
        raise refs.SiteReferenceError(f"the image fetch failed: {exc}") from exc
    return bytes(buf)


async def _read_source(image_url: str, *, workspace_id: str) -> bytes:
    """The bytes behind ``image_url``, from storage or an exact-match host."""
    from pocketpaw_ee.sites.public_assets import PublicAssetError, public_asset_store

    store = public_asset_store()
    if store is not None:
        key = store.key_for_url(image_url, workspace_id=workspace_id)
        if key is not None:
            try:
                return await store.read(key, max_bytes=refs.MAX_REFERENCE_BYTES)
            except PublicAssetError as exc:
                raise refs.SiteReferenceError(str(exc)) from exc
    if not _fetch_allowed(image_url):
        raise refs.SiteReferenceError(_REFUSED_SOURCE)
    return await _fetch(image_url)


async def _open_pocket(pocket_id: str, user_id: str, workspace_id: str) -> dict[str, Any] | None:
    """Open the pocket as the caller, in the chat's workspace. ``None`` on success, an
    error response if not. ``get`` gates by owner and sharing, not by workspace, so
    the workspace is checked here too (the rule ``_settled_previous`` keeps)."""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    try:
        pocket = await pockets_service.get(pocket_id, user_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    if str(pocket.get("workspace") or "") != str(workspace_id):
        return _error_response("that site is not in this workspace.")
    return None


async def _save_site_reference_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__save_site_reference``."""
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "save_site_reference requires workspace and user context (call from a cloud "
            "chat session)."
        )
    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_save_site_reference",
        status="ok",
        ok=True,
    )
    pocket_id = args.get("pocket_id")
    image_url = args.get("image_url")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response("save_site_reference requires the site's `pocket_id`.")
    if not isinstance(image_url, str) or not image_url.strip():
        return _error_response("save_site_reference requires an `image_url`.")
    if (
        gate := await _require_sites_plan_or_error(workspace_id, tool="save_site_reference")
    ) is not None:
        return gate
    if (refused := await _open_pocket(pocket_id, user_id, workspace_id)) is not None:
        return refused
    try:
        data = await _read_source(image_url.strip(), workspace_id=workspace_id)
        saved = await refs.save_reference(data, workspace_id=workspace_id, pocket_id=pocket_id)
    except refs.SiteReferenceError as exc:
        return _error_response(f"{exc} Nothing was saved.")
    except Exception as exc:  # noqa: BLE001 — storage failure, never a failed turn
        logger.warning("sites.save_site_reference: store failed", exc_info=True)
        return _error_response(f"the reference could not be stored ({exc}). Nothing was saved.")
    body = {
        "ok": True,
        "reference_id": saved.reference_id,
        "width": saved.width,
        "height": saved.height,
        "looks_like": saved.viewport,
        "next": (
            "Build to it, then call compare_site_to_reference with this reference_id "
            f"(viewport `{saved.viewport}` unless you know the exact width it was "
            "captured at)."
        ),
    }
    return {"content": [{"type": "text", "text": json.dumps(body)}]}


def _summary(
    result: refs.Comparison, *, reference_id: str, pocket_id: str, label: str, path: str
) -> str:
    where = f" at {path}" if path else ""
    lines = [
        f"Reference {reference_id} against the draft of {pocket_id}{where} at "
        f"{result.width}px ({label}). Compared the top {result.compared_height}px "
        f"(reference {result.reference_height}px tall, build {result.build_height}px).",
        f"Match: {result.match}% of the grid. Score per tile, top first: "
        + ", ".join(str(s) for s in result.tile_scores)
        + ".",
        "Images: one REFERENCE | BUILD pair per tile, then the heatmap (red = the "
        "pixels differ, numbered boxes = the regions below).",
    ]
    if result.dropped_tiles:
        lines.append(
            f"{result.dropped_tiles} lower tile pair(s) were left out to fit the result "
            "size; their scores above still count."
        )
    if result.regions:
        lines.append("Biggest mismatches (CSS px, score 0-100, higher is closer):")
        for n, r in enumerate(result.regions, start=1):
            lines.append(
                f"  {n}. x {r['x']}-{r['x'] + r['w']}, y {r['y']}-{r['y'] + r['h']}: "
                f"score {r['score']}"
            )
    else:
        lines.append("No region is over the mismatch threshold.")
    gap = abs(result.reference_height - result.build_height)
    if gap > result.width // 4:
        lines.append(
            f"The page heights differ by {gap}px: section heights or order are off, and "
            "that comes before any detail."
        )
    lines.append(
        "Fix the biggest mismatch first, in the correction order (content, shell and "
        "section heights, grid, fonts, images, spacing, colour, details, motion), then "
        "compare again; at least two passes. The score measures pixels, not fidelity: "
        "a substituted font or a placeholder image lowers it honestly, so name those "
        "in the verdict rather than chasing them."
    )
    return "\n".join(lines)


@_with_previous_verification("compare_site_to_reference", timed=False)
async def _compare_site_to_reference_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__compare_site_to_reference``.

    Identity -> ``record_tool_call`` -> args -> plan gate -> stored reference ->
    draft screenshot at the same width -> comparison -> images plus one text block.
    """
    from pocketpaw.tools.builtin import reference_images

    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "compare_site_to_reference requires workspace and user context (call from a "
            "cloud chat session)."
        )
    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_compare_site_to_reference",
        status="ok",
        ok=True,
    )
    pocket_id = args.get("pocket_id")
    reference_id = args.get("reference_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response("compare_site_to_reference requires the site's `pocket_id`.")
    if not isinstance(reference_id, str) or not reference_id:
        return _error_response(
            "compare_site_to_reference requires a `reference_id` from save_site_reference."
        )
    path, bad = _preview_path(args.get("path"))
    if bad:
        return _error_response(bad)
    wait_ms, bad = _preview_wait_ms(args.get("wait_ms"))
    if bad:
        return _error_response(bad)
    if (
        gate := await _require_sites_plan_or_error(workspace_id, tool="compare_site_to_reference")
    ) is not None:
        return gate
    if (refused := await _open_pocket(pocket_id, user_id, workspace_id)) is not None:
        return refused

    try:
        reference = await refs.load_reference(
            workspace_id=workspace_id, pocket_id=pocket_id, reference_id=reference_id
        )
        ref_w, ref_h = _png_size(reference)
        width, height, label = refs.resolve_width(args.get("viewport"), ref_w, ref_h)
    except refs.SiteReferenceError as exc:
        return _error_response(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("sites.compare_site_to_reference: load failed", exc_info=True)
        return _error_response(f"the reference could not be read ({exc}).")

    image, url, failure = await _capture_draft(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        viewport={"width": width, "height": height},
        path=path,
        wait_ms=wait_ms,
        log_tag="compare_site_to_reference",
    )
    if failure is not None:
        return failure

    try:
        result = await asyncio.to_thread(
            refs.compare, reference, image, width=width, mobile=width < 600
        )
    except refs.SiteReferenceError as exc:
        return _error_response(str(exc))
    text = _summary(
        result,
        reference_id=reference_id,
        pocket_id=pocket_id,
        label=label,
        path=path if url else "",
    )
    return {
        "content": [
            *reference_images.image_blocks(result.images),
            {"type": "text", "text": text},
        ]
    }


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a stored reference (always a PNG we wrote)."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def make_save_site_reference_tool(tool: Any) -> Any:
    """Build the ``save_site_reference`` SDK tool. Same server as preview_site."""

    @tool(
        "save_site_reference",
        (
            "STORE a design the owner supplied (a screenshot or mockup of the page they "
            "want) on the site, so compare_site_to_reference can measure the draft "
            "against it. Args: `pocket_id` (required); `image_url` (required): the "
            "PUBLIC URL of an image the owner attached in this chat or uploaded to this "
            "site, or a Refero screenshot. Other hosts are refused. Returns a "
            "`reference_id`, the image size and the width class it looks like. Saving "
            "the same image twice returns the same id."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string", "description": "Id of the site pocket."},
                "image_url": {
                    "type": "string",
                    "description": "Public URL of the reference image.",
                },
            },
            "required": ["pocket_id", "image_url"],
            "additionalProperties": False,
        },
    )
    async def save_site_reference(args):  # type: ignore[no-untyped-def]
        return await _save_site_reference_handler(args)

    return save_site_reference


def make_compare_site_to_reference_tool(tool: Any) -> Any:
    """Build the ``compare_site_to_reference`` SDK tool. Same server as preview_site."""

    @tool(
        "compare_site_to_reference",
        (
            "COMPARE the site's current DRAFT against a stored reference at the same "
            "width: returns one REFERENCE | BUILD side-by-side per tile, a diff heatmap, "
            "the biggest mismatch regions in CSS px and a score per tile. Args: "
            "`pocket_id` and `reference_id` (from save_site_reference, required); "
            "`viewport`: `desktop` (1280px), `mobile` (390px) or the exact CSS width "
            "the reference was captured at (320-1920; default: guessed from the "
            "reference); `path`: a route on the draft (template-built sites); "
            "`wait_ms` (0-8000) for animated pages. Fix the biggest mismatch first and "
            "compare again; at least two passes. An error means no comparison ran; it "
            "never means the site is broken."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string", "description": "Id of the site pocket."},
                "reference_id": {
                    "type": "string",
                    "description": "A `ref_...` id from save_site_reference.",
                },
                "viewport": {
                    "anyOf": [
                        {"type": "string", "enum": ["desktop", "mobile"]},
                        {"type": "integer", "minimum": 320, "maximum": 1920},
                    ],
                    "description": "Width class or exact CSS width to render at.",
                },
                "path": {
                    "type": "string",
                    "description": "Route on the draft, e.g. `/about` (default home).",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Milliseconds to wait after load (0-8000).",
                },
            },
            "required": ["pocket_id", "reference_id"],
            "additionalProperties": False,
        },
    )
    async def compare_site_to_reference(args):  # type: ignore[no-untyped-def]
        return await _compare_site_to_reference_handler(args)

    return compare_site_to_reference


__all__ = [
    "make_compare_site_to_reference_tool",
    "make_save_site_reference_tool",
]
