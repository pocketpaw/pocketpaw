# test_source_files.py — pins how LocalExtractor reads code-like uploads
# (HTML, JS, CSS, SVG): as source, with inline data: URIs and long base64
# runs replaced by a short marker.
#
# The shape that matters is a saved Claude artifact page: a <title>, an empty
# <div id="root">, a large inline <script> that renders everything, and
# base64 images. Its visible text is the title alone, so a "visible text"
# extractor returns one line and silently drops 99% of the file. These tests
# build that shape at test time and assert the source survives.
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.extraction import local as local_mod
from pocketpaw_ee.cloud.extraction.local import LocalExtractor

TITLE = "The Last Boring Slide (Copy)"
BLOB = base64.b64encode(b"\x89PNG\r\n" + bytes(range(256)) * 200).decode()


def artifact_html() -> str:
    """A JS-rendered page: no static body text, one big script, one data: image."""
    script = (
        "function renderSlide(n){document.getElementById('root').innerHTML='<h1>'+n+'</h1>';}\n"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{TITLE}</title><style>.slide{{color:#1877f2}}</style></head>"
        "<body><div id='root'></div>"
        f'<img src="data:image/png;base64,{BLOB}">'
        f"<script>{script * 200}</script></body></html>"
    )


async def test_a_script_rendered_page_returns_its_source_not_just_its_title(
    tmp_path: Path,
) -> None:
    path = tmp_path / "The Last Boring Slide (Copy).html"
    path.write_text(artifact_html(), encoding="utf-8")

    result = await LocalExtractor().extract(path, "text/html")

    assert "<script>" in result.text
    assert "renderSlide" in result.text
    assert ".slide{color:#1877f2}" in result.text
    assert BLOB[:200] not in result.text
    assert "stripped" in result.text  # the blob left a visible marker


@pytest.mark.parametrize(
    ("name", "mime", "body"),
    [
        ("app.js", "text/javascript", f"const logo = 'data:image/png;base64,{BLOB}';\nrun();"),
        (
            "site.css",
            "text/css",
            f"@font-face{{src:url(data:font/woff2;base64,{BLOB})}}\n.a{{color:red}}",
        ),
        ("icon.svg", "image/svg+xml", f"<svg><image href='data:image/png;base64,{BLOB}'/></svg>"),
    ],
)
async def test_code_files_keep_their_source_and_lose_their_blobs(
    tmp_path: Path, name: str, mime: str, body: str
) -> None:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")

    result = await LocalExtractor().extract(path, mime)

    assert BLOB[:200] not in result.text
    assert "stripped" in result.text
    assert len(result.text) < 500


def test_read_source_reports_what_it_stripped(tmp_path: Path) -> None:
    path = tmp_path / "a.html"
    path.write_text(artifact_html(), encoding="utf-8")

    source = local_mod.read_source(path)

    assert source.stripped_chars >= len(BLOB)
    assert source.truncated is False
    assert "<div id='root'></div>" in source.text


def test_a_bare_base64_run_is_stripped_but_ordinary_code_is_not() -> None:
    minified = "a.b(c);" * 2000  # long, but punctuated: real code
    text, stripped = local_mod.strip_inline_blobs(f"x='{BLOB}';{minified}")

    assert BLOB[:200] not in text
    assert stripped >= len(BLOB)
    assert minified in text


def test_read_source_cuts_at_its_byte_limit_and_says_so(tmp_path: Path) -> None:
    path = tmp_path / "big.js"
    path.write_text("x();\n" * 1000, encoding="utf-8")

    source = local_mod.read_source(path, limit=100)

    assert source.truncated is True
    assert len(source.text) == 100


async def test_article_html_still_indexes_text_not_markup(tmp_path: Path) -> None:
    """The source fallback is for pages with no visible text only; an article
    keeps the trafilatura path so the KB never indexes its markup."""
    article = (
        "<html><head><title>Notes</title></head><body><article><h1>Notes</h1>"
        + "<p>A real paragraph a reader would search for, long enough to count.</p>" * 20
        + "</article></body></html>"
    )
    path = tmp_path / "post.html"
    path.write_text(article, encoding="utf-8")

    result = await LocalExtractor().extract(path, "text/html")

    assert "real paragraph" in result.text
    assert "<p>" not in result.text


def test_a_url_encoded_data_uri_is_stripped_and_keeps_its_mime() -> None:
    """Not base64 and under the bare-run length, so only the data: pattern
    can catch it (an inline SVG icon in CSS is the common case)."""
    svg = "%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%3E" + "%3Cpath%2F%3E" * 40
    text, stripped = local_mod.strip_inline_blobs(
        f".icon{{background:url(data:image/svg+xml,{svg})}}"
    )

    assert svg not in text
    assert "data:image/svg+xml,[" in text
    assert stripped == len(svg)
