# tests/ee/sites/test_verify_checks_blocking.py — the page checks that BLOCK a publish
# (pocketpaw_ee.sites.verify_checks entries with ``level: "error"``).
#
# Three defects shipped on real sites in the 2026-10-31 persona run: a mailing-list
# form that was a GET with no handler (the email landed in the address bar), "[Address]"
# and "Call us on [Phone]" on a live café page, and a made-up WhatsApp number
# (9876543210) that belongs to a stranger. Each has a fixture that trips it, plus the
# starter patterns that must stay clean (paw.js subscribe forms, SvelteKit / Astro
# POST-to-self on a Worker, framework bundles that bind submit, search boxes, labels
# that say "Your name"). Mutations: tests/mutations/sites_publish_gate.json.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.sites import verify_checks

from .test_verify_checks import _doc, _ids


def _run(files: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return verify_checks.run_checks({"favicon.svg": "<svg/>", **files}, **kw)


def _errors(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in out["warnings"] if w.get("level") == "error"]


def _error_ids(out: dict[str, Any]) -> list[str]:
    return [w["id"] for w in _errors(out)]


_NAV_SCRIPT = "<script>document.querySelector('.menu').classList.toggle('open')</script>"

#: The yoga site's mailing list: a GET form with an email field on a page whose only
#: script toggles the menu.
_YOGA_FORM = (
    '<form id="mailing-list" class="join"><label for="e">Email</label>'
    '<input id="e" type="email" name="email" required><button>Join the list</button></form>'
)


class TestDeadForms:
    def test_get_form_with_email_on_a_scripted_page_is_an_error(self) -> None:
        out = _run({"index.html": _doc(_YOGA_FORM + _NAV_SCRIPT)})
        assert _error_ids(out) == ["SC-LINK-04"]
        entry = _errors(out)[0]
        assert entry["items"] == ["mailing-list"]
        assert "address bar" in entry["detail"]

    def test_plain_form_on_a_page_with_no_script_is_an_error(self) -> None:
        out = _run({"index.html": _doc('<form id="c"><input name="q"><button>Go</button></form>')})
        assert _error_ids(out) == ["SC-LINK-04"]

    def test_action_to_the_page_itself_is_dead(self) -> None:
        form = '<form action="contact.html" method="get"><input type="tel" name="phone"></form>'
        out = _run({"index.html": _doc("hi"), "contact.html": _doc(form + _NAV_SCRIPT)})
        assert _error_ids(out) == ["SC-LINK-04"]
        assert _errors(out)[0]["page"] == "contact.html"

    def test_post_with_no_server_on_a_scripted_page_is_dead(self) -> None:
        form = '<form method="post"><textarea name="message"></textarea><button>Go</button></form>'
        assert _error_ids(_run({"index.html": _doc(form + _NAV_SCRIPT)})) == ["SC-LINK-04"]

    @pytest.mark.parametrize(
        ("body", "kw"),
        [
            # The static starter's integration form: paw.js binds data-paw-subscribe.
            (
                '<form data-paw-subscribe><input type="email" name="email"><button>Join</button>'
                '</form><script src="/_paw/paw.js" data-site-key="k"></script>',
                {},
            ),
            # SvelteKit / Astro actions: POST to the page itself, a Worker answers it.
            (
                '<form method="POST"><input name="title"><input type="email" name="email">'
                '</form><script type="module" src="/_app/start.js"></script>',
                {"server_routes": True},
            ),
            (
                '<form method="POST" action="?_action=createEntry"><input name="title"></form>',
                {"server_routes": True},
            ),
            # A framework bundle that binds submit (React, Svelte, hand-written JS).
            (
                _YOGA_FORM + "<script>document.addEventListener('submit', e => send(e))</script>",
                {},
            ),
            (_YOGA_FORM + '<script type="module" src="/assets/app.js"></script>', {}),
            # The form is looked up by id, so a script can bind it.
            (_YOGA_FORM + "<script>const f = document.getElementById('mailing-list')</script>", {}),
            # A script from elsewhere: it may bind anything.
            (_YOGA_FORM + '<script src="https://cdn.example.net/forms.js"></script>', {}),
            # Hooks and handlers on the form or inside it.
            ('<form data-netlify="true"><input type="email" name="email"></form>', {}),
            ('<form hx-post="/x"><input type="email" name="email"></form>', {}),
            (
                '<form><input type="email" name="email">'
                '<button type="button" onclick="join()">Join</button></form>',
                {},
            ),
            # A search box on a scripted page, or any GET search on a Worker.
            (
                '<form role="search"><input type="search" name="q"></form>' + _NAV_SCRIPT,
                {},
            ),
            ('<form><input name="q"></form>', {"server_routes": True}),
            # Not judged: a real target, a dialog, a formaction.
            ('<form action="https://formspree.io/f/x"><input type="email" name="e"></form>', {}),
            ('<form method="dialog"><button>Close</button></form>', {}),
            (
                '<form><input type="email" name="email">'
                '<button formaction="/thanks.html">Go</button></form>',
                {},
            ),
        ],
    )
    def test_handled_forms_are_clean(self, body: str, kw: dict[str, Any]) -> None:
        files = {
            "index.html": _doc(body),
            "thanks.html": _doc("ok"),
            "assets/app.js": "form.addEventListener('submit', onSubmit)",
            "_app/start.js": "export const start = () => {}",
        }
        assert "SC-LINK-04" not in _ids(_run(files, **kw))

    def test_a_button_wired_to_fetch_counts_as_bound(self) -> None:
        body = (
            '<form id="signup-form"><input type="email" name="email">'
            '<button type="button" id="go">Join</button></form>'
            "<script>go.addEventListener('click', () => fetch('/api/join'))</script>"
        )
        assert "SC-LINK-04" not in _ids(_run({"index.html": _doc(body)}))

    def test_a_scripted_page_with_a_non_collecting_form_is_not_judged(self) -> None:
        body = "<form><button>Send</button></form>" + _NAV_SCRIPT
        assert "SC-LINK-04" not in _ids(_run({"index.html": _doc(body)}))

    def test_platform_script_alone_does_not_count_as_a_handler(self) -> None:
        body = _YOGA_FORM + '<script src="/_paw/paw.js"></script>'
        files = {"index.html": _doc(body), "_paw/paw.js": "addEventListener('submit',f)"}
        assert _error_ids(_run(files)) == ["SC-LINK-04"]


class TestPlaceholders:
    @pytest.mark.parametrize(
        "text",
        [
            "[Address]",
            "Call us on [Phone]",
            "[Opening hours]",
            "[Your Business Name]",
            "Lorem ipsum dolor",
        ],
    )
    def test_placeholder_copy_is_an_error(self, text: str) -> None:
        out = _run({"index.html": _doc(f"<p>{text}</p>")})
        assert _error_ids(out) == ["SC-HONEST-07"]
        assert _errors(out)[0]["items"]

    def test_an_example_address_in_a_mailto_is_an_error(self) -> None:
        out = _run({"index.html": _doc('<a href="mailto:hello@example.com">Email us</a>')})
        assert _error_ids(out) == ["SC-HONEST-07"]

    @pytest.mark.parametrize(
        "body",
        [
            "<p>Grow Your Business Online</p>",
            "<p>Your Name, founder</p>",
            "<p>WhatsApp XXXXX XXXXX</p>",
            "<label>Phone (XXX-XXX-XXXX)</label>",
            "<p>Card XXXX XXXX XXXX 4242</p>",
            "<span>Your Email</span>",
            "<p>Use the format you@example.com</p>",
            "<p>[Your Guide] to the coast</p>",
            '<input placeholder="[Address]">',
            "<label>[Phone]</label>",
        ],
    )
    def test_generic_or_prompt_text_only_warns(self, body: str) -> None:
        assert _errors(_run({"index.html": _doc(body)})) == []

    @pytest.mark.parametrize(
        "body",
        [
            '<form action="/t.html"><label for="n">Your name</label><input id="n"></form>',
            '<form action="/t.html"><label>Your Name <input name="n"></label></form>',
            "<p>Tell us your name and we'll call.</p>",
        ],
    )
    def test_form_labels_are_not_placeholders(self, body: str) -> None:
        out = _run({"index.html": _doc(body), "t.html": _doc("ok")})
        assert _errors(out) == []


class TestInventedNumbers:
    @pytest.mark.parametrize(
        "body",
        [
            "<p>WhatsApp us on 9876543210</p>",
            "<p>Call +91 98765 43210</p>",
            '<a href="https://wa.me/919876543210">Order on WhatsApp</a>',
            '<a href="tel:+911234567890">Call</a>',
            "<p>Phone 000-000-0000</p>",
            "<p>Call 555-0142</p>",
            "<p>Call (555) 234-5678</p>",
            "<p>Text 07700 900123</p>",
            "<p>Text +44 7700 900123</p>",
        ],
    )
    def test_made_up_numbers_are_errors(self, body: str) -> None:
        out = _run({"index.html": _doc(body)})
        assert _error_ids(out) == ["SC-HONEST-03"]
        assert "real" in _errors(out)[0]["detail"]

    @pytest.mark.parametrize(
        "text",
        [
            "Call +91 98201 47356",
            "Dr Priya Raman, 0113 496 0000",
            "Since 2019-2024",
            "Order #1001",
            "Open 9-5, 7 days",
            "Call 020 7946 1234",
            "Call 55 5280 1234",
            "Call 98111 11111",
            "Call 1800 0123 4567",
            "Server 123.45.67.89",
            "Phone 123.45.67.89",
            "Steps 1 2 3 4 5 6 7 8 9",
            "Ref 9876543210",
            "Batch 1234567890",
        ],
    )
    def test_real_looking_numbers_are_clean(self, text: str) -> None:
        assert _errors(_run({"index.html": _doc(f"<p>{text}</p>")})) == []

    def test_default_names_stay_a_warning(self) -> None:
        out = _run({"index.html": _doc("<p>Sarah Chen, owner</p>")})
        assert _ids(out) == ["SC-HONEST-03"]
        assert _errors(out) == []


class TestBounded:
    @pytest.mark.parametrize(
        "body",
        ["a" * 200_000, "a." * 100_000 + "@", "1 " * 100_000, "[" * 200_000, "X X " * 50_000],
        ids=["letters", "dotted-local-part", "digit-runs", "brackets", "x-runs"],
    )
    def test_adversarial_input_runs_in_bounded_time(self, body: str) -> None:
        import time

        started = time.monotonic()
        _run({"index.html": _doc(f"<p>{body}</p>")})
        assert time.monotonic() - started < 3.0

    def test_a_tree_past_the_time_budget_is_counted_not_finished(self, monkeypatch) -> None:
        monkeypatch.setattr(verify_checks, "MAX_CHECK_SECONDS", 0.0)
        pages = {f"p{i}.html": _doc("<p>ok</p>") for i in range(3)}
        out = _run(pages)
        assert out["pages"] == 0
        assert out["pages_skipped"] == 3
        assert "time budget" in out["note"]


class TestReportOrder:
    def test_errors_survive_the_cap_and_lead(self) -> None:
        # Dead anchors rank above placeholders, so only the error-first sort keeps it.
        pages = {f"p{i}.html": _doc(f'<a href="#nope{i}">x</a>') for i in range(40)}
        pages["z.html"] = _doc("<p>Find us at [Address]</p>")
        out = _run(pages)
        assert len(out["warnings"]) == verify_checks.MAX_WARNINGS
        assert out["warnings"][0]["id"] == "SC-HONEST-07"
        assert out["warnings"][0]["level"] == "error"

    def test_warn_entries_keep_their_shape(self) -> None:
        out = _run({"index.html": _doc('<a href="#nope">x</a>')})
        assert set(out["warnings"][0]) == {"id", "page", "detail"}


class TestReReviewNegatives:
    @pytest.mark.parametrize(
        "text",
        [
            "[Company Name]",
            "[Brand Name]",
            "[Street Address]",
            "[City]",
            "[Zip]",
            "[Postcode]",
            "[Business Name]",
        ],
    )
    def test_more_bracketed_tokens_block(self, text: str) -> None:
        out = _run({"index.html": _doc(f"<p>Visit {text}</p>")})
        assert _error_ids(out) == ["SC-HONEST-07"]

    @pytest.mark.parametrize(
        "body",
        [
            "<p>555-0142</p>",
            "<p>(555) 123-4567</p>",
            "<p>07700 900123</p>",
            "<p>+44 7700 900123</p>",
            "<p>Reach us 9876543210</p>",
            "<p>Hotline 1234567890</p>",
            "<p>Helpline: 9999999999</p>",
            "<p>Ph 9876543210</p>",
            "<p>Mob. 9876543210</p>",
            "<p>T: 9876543210</p>",
            "<dl><dt>Phone</dt><dd>9876543210</dd></dl>",
            "<p>WhatsApp</p><p>9876543210</p>",
        ],
    )
    def test_invented_numbers_found_without_or_beside_a_phone_word(self, body: str) -> None:
        assert _error_ids(_run({"index.html": _doc(body)})) == ["SC-HONEST-03"]

    @pytest.mark.parametrize(
        "text",
        ["Order 123456789", "Invoice 987654321", "Tracking 1234567890", "Ref 9999999999"],
    )
    def test_long_runs_outside_phone_context_pass(self, text: str) -> None:
        assert _errors(_run({"index.html": _doc(f"<p>{text}</p>")})) == []

    def test_the_page_cap_decides_not_the_clock(self, monkeypatch) -> None:
        monkeypatch.setattr(verify_checks, "MAX_PAGES", 2)
        pages = {f"p{i}.html": _doc("<p>ok</p>") for i in range(5)}
        out = _run(pages)
        assert (out["pages"], out["pages_skipped"]) == (2, 3)
        assert verify_checks.MAX_CHECK_SECONDS >= 20

    def test_a_drama_number_in_a_tel_link_blocks(self) -> None:
        out = _run({"index.html": _doc('<a href="tel:+447700900123">Call us</a>')})
        assert _error_ids(out) == ["SC-HONEST-03"]
