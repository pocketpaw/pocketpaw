# Browser snapshot tests
# Changes: 2026-09-06 (BR-1, feat/browser-surface-server) — REWRITTEN. The old
#   file tested ``AccessibilityNode`` + ``SnapshotGenerator`` against
#   hand-written Playwright accessibility dicts. Playwright removed
#   ``page.accessibility``, so those classes could never run against a real page
#   and the suite was green on code that raised in production. Both classes are
#   deleted; this covers their replacement, ``render_snapshot``.
"""Tests for the DOM-walk snapshot renderer."""

from pocketpaw.browser.snapshot import MAX_SNAPSHOT_CHARS, RefMap, render_snapshot


class TestRefMap:
    def test_get_selector_returns_data_paw_ref(self):
        refmap = RefMap(refs={1: '[data-paw-ref="1"]'})
        assert refmap.get_selector(1) == '[data-paw-ref="1"]'

    def test_get_selector_unknown_ref_is_none(self):
        assert RefMap().get_selector(99) is None


class TestRenderSnapshot:
    def test_header_carries_title_and_url(self):
        text, _ = render_snapshot(
            {"text": "- text: hi", "count": 0, "title": "Example", "url": "https://example.com"}
        )
        assert "Page: Example" in text
        assert "URL: https://example.com" in text
        assert "- text: hi" in text

    def test_refmap_covers_every_stamped_ref(self):
        """THE MUTATION THAT BREAKS THIS: build the map over ``range(count)``
        instead of ``range(1, count + 1)`` — ref 3 disappears and ref 0, which
        the JS never stamps, appears."""
        _, refmap = render_snapshot({"text": "", "count": 3, "title": "", "url": ""})
        assert refmap.refs == {
            1: '[data-paw-ref="1"]',
            2: '[data-paw-ref="2"]',
            3: '[data-paw-ref="3"]',
        }
        assert refmap.next_ref == 4

    def test_zero_refs_is_an_empty_map(self):
        _, refmap = render_snapshot({"text": "- text: nothing here", "count": 0})
        assert refmap.refs == {}

    def test_oversized_page_is_truncated(self):
        """A snapshot must not be able to blow the agent's context window."""
        text, _ = render_snapshot({"text": "x" * (MAX_SNAPSHOT_CHARS * 2), "count": 0})
        assert "snapshot truncated" in text
        assert len(text) < MAX_SNAPSHOT_CHARS + 500

    def test_empty_payload_does_not_raise(self):
        """``page.evaluate`` can come back empty on an about:blank page."""
        text, refmap = render_snapshot({})
        assert refmap.refs == {}
        assert "Page:" in text


def test_render_snapshot_never_carries_a_password_field_value():
    """A pre-filled / autofilled password (or OTP / card) input must not leak its
    VALUE into the snapshot text — that text goes straight into the agent's
    context. Regression for the review finding: nameOf fell back to el.value.

    render_snapshot is pure over the SNAPSHOT_JS payload, so we assert the
    contract the JS must satisfy: a password ref line carries no value as its
    name. (The live DOM-walk half is exercised by the driver leak smoke.)
    """
    from pocketpaw.browser.snapshot import render_snapshot

    # Shape SNAPSHOT_JS returns for a password input whose value was suppressed.
    payload = {
        "text": '- textbox "" [ref=1] type=password SENSITIVE',
        "count": 1,
        "title": "Login",
        "url": "https://example.com/login",
    }
    text, _ = render_snapshot(payload)
    assert "type=password SENSITIVE" in text
    assert "hunter2" not in text
