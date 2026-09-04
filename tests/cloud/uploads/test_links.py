# test_links.py — table-driven tests for the note link/tag parser.
# Created: 2026-09-05 (feat/files-links, B1). Covers wikilink forms (alias,
#   heading, both), hashtag placement (line start, after whitespace, never a
#   heading or a URL fragment), frontmatter tag forms (inline list, comma
#   string, block list) and that fenced + inline code are skipped throughout.
"""Tests for ``pocketpaw_ee.cloud.uploads.links``."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.uploads.links import normalize_link_name, parse_note_links


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Name", "name"),
        ("  Name.md ", "name"),
        ("Name.MD", "name"),
        ("Name.md.md", "name.md"),
        ("Über Note", "über note"),
    ],
)
def test_normalize_link_name(raw, expected):
    assert normalize_link_name(raw) == expected


@pytest.mark.parametrize(
    ("text", "links"),
    [
        ("see [[Alpha]]", ("alpha",)),
        ("[[Alpha|the alias]]", ("alpha",)),
        ("[[Alpha#Heading]]", ("alpha",)),
        ("[[Alpha#Heading|alias]]", ("alpha",)),
        ("[[Alpha.md]] and [[alpha]] twice", ("alpha",)),
        ("[[Beta]] then [[Alpha]] then [[Beta]]", ("beta", "alpha")),
        ("[[ ]] [[#only-heading]] [[|alias only]]", ()),
        ("```\n[[Fenced]]\n```\n[[Real]]", ("real",)),
        ("inline `[[Code]]` and [[Real]]", ("real",)),
        ("", ()),
    ],
)
def test_wikilinks(text, links):
    assert parse_note_links(text).link_names == links


@pytest.mark.parametrize(
    ("text", "tags"),
    [
        ("#todo at line start", ("todo",)),
        ("word #todo after space", ("todo",)),
        ("line one\n#todo on second line", ("todo",)),
        ("# Heading is not a tag", ()),
        ("## Also not", ()),
        ("https://example.com/page#frag is a url", ()),
        ("#Mixed/Case-tag_1 #mixed/case-tag_1", ("mixed/case-tag_1",)),
        ("#123 numeric first", ()),
        ("```\n#fenced\n```\n#real", ("real",)),
        ("`#inline` #real", ("real",)),
    ],
)
def test_hashtags(text, tags):
    assert parse_note_links(text).hashtags == tags


@pytest.mark.parametrize(
    ("text", "tags"),
    [
        ("---\ntags: [a, b]\n---\nbody", ("a", "b")),
        ("---\ntags: a, b\n---\nbody", ("a", "b")),
        ("---\ntitle: x\ntags:\n  - a\n  - b\nother: y\n---\nbody", ("a", "b")),
        ("---\ntags: ['A', \"b\"]\n---\n", ("a", "b")),
        ("---\ntag: solo\n---\n", ("solo",)),
        ("---\ntitle: no tags here\n---\nbody", ()),
        ("body first\n---\ntags: [a]\n---\n", ()),
        ("---\ntags: [a]\n---", ("a",)),
    ],
)
def test_frontmatter_tags(text, tags):
    assert parse_note_links(text).frontmatter_tags == tags


def test_frontmatter_is_not_scanned_for_hashtags_or_links():
    text = "---\ntags: [x]\nnote: '#nottag [[NotLink]]'\n---\n#real [[Real]]"
    got = parse_note_links(text)
    assert got.frontmatter_tags == ("x",)
    assert got.hashtags == ("real",)
    assert got.link_names == ("real",)


def test_result_is_frozen():
    got = parse_note_links("[[A]] #b")
    with pytest.raises(Exception):
        got.link_names = ()  # type: ignore[misc]
