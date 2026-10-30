# test_auto_tagging.py — unit tests for the FL-6 tag-derivation helpers.
# Created: 2026-07-03 — FL-6 "Auto-tagging on ingest". Covers
#   ee.cloud.uploads.tagging.derive_tags (title/caption/text keyword
#   extraction, adapter-label precedence, normalization, dedup, cap) and
#   merge_tags (union semantics that preserve user-applied tags on re-index).
"""Unit tests for ``ee.cloud.uploads.tagging``."""

from __future__ import annotations

from pocketpaw_ee.cloud.uploads.tagging import MAX_TAGS, derive_tags, merge_tags


class TestDeriveTags:
    def test_derives_keywords_from_text(self):
        tags = derive_tags(
            title="Quarterly Invoice",
            captions=[],
            text="invoice invoice payment payment payment amount due",
            metadata={},
        )
        assert "invoice" in tags
        assert "payment" in tags

    def test_prefers_adapter_labels_from_metadata(self):
        tags = derive_tags(
            title=None,
            captions=[],
            text="some generic body text about widgets",
            metadata={"labels": ["Whiteboard", "Diagram"]},
        )
        # Adapter labels come first and are normalized.
        assert tags[0] == "whiteboard"
        assert "diagram" in tags

    def test_accepts_comma_delimited_label_string(self):
        tags = derive_tags(
            title=None,
            captions=[],
            text="",
            metadata={"keywords": "budget, forecast, revenue"},
        )
        assert set(tags) >= {"budget", "forecast", "revenue"}

    def test_normalizes_lowercase_and_trims(self):
        tags = derive_tags(
            title="  ROADMAP  ",
            captions=[],
            text="Roadmap roadmap planning planning strategy",
            metadata={},
        )
        assert "roadmap" in tags
        # No uppercase or surrounding whitespace leaks through.
        assert all(t == t.strip().lower() for t in tags)

    def test_dedups(self):
        tags = derive_tags(
            title="Report report",
            captions=["report REPORT"],
            text="report report report",
            metadata={"labels": ["Report"]},
        )
        assert tags.count("report") == 1

    def test_caps_at_max_tags(self):
        text = " ".join(f"keyword{i}" for i in range(50))
        tags = derive_tags(title=None, captions=[], text=text, metadata={})
        assert len(tags) <= MAX_TAGS

    def test_drops_stopwords_and_short_tokens(self):
        tags = derive_tags(
            title=None,
            captions=[],
            text="the and for a an it is to of on invoice",
            metadata={},
        )
        assert "the" not in tags
        assert "and" not in tags
        assert "invoice" in tags

    def test_empty_extraction_yields_no_tags(self):
        assert derive_tags(title=None, captions=[], text="", metadata={}) == []
        assert derive_tags(title="", captions=None, text=None, metadata=None) == []

    def test_caption_content_contributes(self):
        tags = derive_tags(
            title=None,
            captions=["A photo of a golden retriever puppy playing fetch"],
            text="",
            metadata={},
        )
        assert "retriever" in tags or "puppy" in tags or "golden" in tags


class TestMergeTags:
    def test_union_preserves_existing_user_tags(self):
        merged = merge_tags(["mytag"], ["auto1", "auto2"])
        assert merged[0] == "mytag"
        assert "auto1" in merged and "auto2" in merged

    def test_re_index_does_not_clobber_user_tag(self):
        # Simulate a re-index: file already has a user tag; derivation yields
        # a fresh set — the user tag must survive.
        existing = ["important"]
        derived = ["invoice", "2026"]
        merged = merge_tags(existing, derived)
        assert "important" in merged

    def test_dedups_across_existing_and_derived(self):
        merged = merge_tags(["invoice"], ["invoice", "payment"])
        assert merged.count("invoice") == 1
        assert "payment" in merged

    def test_normalizes_existing_tags(self):
        merged = merge_tags(["  Invoice "], ["invoice"])
        assert merged == ["invoice"]

    def test_none_existing_is_safe(self):
        assert merge_tags(None, ["alpha", "beta"]) == ["alpha", "beta"]

    def test_capped_at_max(self):
        existing = [f"user{i}" for i in range(6)]
        derived = [f"auto{i}" for i in range(6)]
        merged = merge_tags(existing, derived)
        assert len(merged) == MAX_TAGS
        # Existing tags are kept first, so they win the cap.
        assert merged[0] == "user0"
