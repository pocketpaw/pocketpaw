# tests/ee/sites/test_play_test_forms.py — play_test_site submits every form on the draft
# with test data before the agent calls the site ready (pocketpaw_ee.sites.play_test
# ``judge_form`` / ``exercise_page_forms``, the ``exercise_forms`` tool arg).
#
# The judge fails a form only for what a visitor certainly hits: a GET that echoes the
# entry into the address bar (the yoga mailing list), a 404 / 405 / 5xx answer, or a
# submit that sent nothing. Refusals of the test entry (401/403/422/429, captchas,
# validation), posts to another service and popups are "unchecked". A confirmation is
# a data-paw-state, an alert, any visible change near the form or a move to another
# page, in any language. The loop never lets a test entry leave the draft: an external
# form is never submitted, every off-origin request but static assets is aborted
# (any method), popups are closed, same-origin answers are fetched without following
# redirects so a hop to another host is aborted too, and the Paw gateway is stubbed in
# the shape paw.js expects. The tool side is in
# tests/ee/agent/test_sites_mcp_server/test_play_test_site.py.
from __future__ import annotations

import time
from typing import Any

import pytest
from pocketpaw_ee.sites import play_test

_BASE = "https://abc.preview.example.com/"


def _info(**kw: Any) -> dict[str, Any]:
    return {"name": "join", "fields": ["email"], "invalid": [], "external": "", "text": "", **kw}


def _post(status: int, url: str = _BASE + "api/join") -> dict[str, Any]:
    return {"kind": "site", "method": "POST", "url": url, "status": status}


class TestJudge:
    def test_get_to_the_same_page_is_a_failure(self) -> None:
        sent = [{"kind": "navigation", "method": "GET", "url": _BASE + "?email=x", "status": 200}]
        out = play_test.judge_form(_info(), {"text": "", "state": None}, sent, _BASE)
        assert out.status == "failed"
        assert "address bar" in out.outcome

    @pytest.mark.parametrize("code", [404, 405, 500, 503])
    def test_a_broken_answer_is_a_failure(self, code: int) -> None:
        out = play_test.judge_form(_info(), {"text": "", "state": None}, [_post(code)], _BASE)
        assert (out.status, out.outcome) == (
            "failed",
            f"the site answered with an error (HTTP {code})",
        )

    @pytest.mark.parametrize("code", [400, 401, 403, 422, 429])
    def test_a_refused_test_entry_is_unchecked(self, code: int) -> None:
        out = play_test.judge_form(_info(), {"text": "", "state": None}, [_post(code)], _BASE)
        assert out.status == "unchecked"
        assert f"HTTP {code}" in out.outcome

    def test_a_captcha_is_unchecked(self) -> None:
        out = play_test.judge_form(_info(), {"text": "", "captcha": True}, [], _BASE)
        assert out.status == "unchecked" and "captcha" in out.outcome

    def test_a_send_with_no_visible_change_is_unchecked_not_failed(self) -> None:
        state = {"text": "Join us", "state": None, "changed": False}
        out = play_test.judge_form(_info(), state, [_post(200)], _BASE)
        assert out.status == "unchecked"
        assert "nothing on the page changed" in out.outcome

    def test_nothing_sent_is_a_failure(self) -> None:
        out = play_test.judge_form(_info(), {"text": "", "state": None}, [], _BASE)
        assert (out.status, out.detail) == ("failed", "no request was sent")

    @pytest.mark.parametrize(
        ("state", "sent", "dialogs"),
        [
            ({"text": "Thanks! You're on the list.", "state": None}, [_post(201)], []),
            (
                {"text": "", "state": "success"},
                [{"kind": "gateway", "method": "POST", "url": "https://api/x", "status": 200}],
                [],
            ),
            (
                {"text": "", "state": None},
                [
                    _post(303, _BASE),
                    {"kind": "navigation", "method": "GET", "url": _BASE + "thanks", "status": 200},
                ],
                [],
            ),
            # Not English and no known word: only the visible change says it worked.
            ({"text": "Ευχαριστούμε! Θα σας γράψουμε σύντομα.", "changed": True}, [_post(200)], []),
            # An alert() saying so.
            ({"text": "", "changed": False}, [_post(200)], ["Danke, wir melden uns."]),
        ],
        ids=["english-words", "paw-state", "moved", "non-english-change", "alert"],
    )
    def test_a_confirmed_send_passes(self, state: dict, sent: list, dialogs: list) -> None:
        out = play_test.judge_form(_info(text="Join"), state, sent, _BASE, dialogs=dialogs)
        assert (out.status, out.outcome) == ("ok", "sent and confirmed")

    def test_a_success_word_already_on_the_page_does_not_count(self) -> None:
        page = "Thank you for visiting"
        out = play_test.judge_form(
            _info(text=page), {"text": page, "state": None}, [_post(200)], _BASE
        )
        assert out.status != "ok"

    def test_validation_other_services_and_popups_are_unchecked(self) -> None:
        invalid = play_test.judge_form(_info(invalid=["phone: wrong format"]), None, [], _BASE)
        assert invalid.status == "unchecked"
        ext = [{"kind": "external", "method": "POST", "url": "https://formspree.io/f/x"}]
        out = play_test.judge_form(_info(), None, ext, _BASE)
        assert out.status == "unchecked" and "formspree.io" in out.outcome
        popup = play_test.judge_form(_info(), None, [{"kind": "popup", "url": _BASE}], _BASE)
        assert popup.status == "unchecked" and "new window" in popup.outcome

    def test_an_external_form_is_unchecked(self) -> None:
        out = play_test.judge_form(_info(external="docs.google.com"), None, [], _BASE)
        assert out.status == "unchecked" and "docs.google.com" in out.outcome


class _Response:
    def __init__(self, status: int, location: str = "") -> None:
        self.status = status
        self.headers = {"location": location} if location else {}


class _Route:
    def __init__(self, url: str, method: str, kind: str = "fetch", answer: int = 200) -> None:
        self.request = type("R", (), {"url": url, "method": method, "resource_type": kind})()
        self.answer = answer
        self.location = ""
        self.done = ""
        self.fetch_kw: dict[str, Any] = {}
        self.body = ""

    async def continue_(self) -> None:
        self.done = "continue"

    async def fetch(self, **kw: Any) -> _Response:
        self.fetch_kw = kw
        return _Response(self.answer, self.location)

    async def fulfill(self, **kw: Any) -> None:
        self.done = "fulfill"
        self.body = kw.get("body", "")

    async def abort(self, *_a: Any) -> None:
        self.done = "abort"


class _Context:
    def __init__(self) -> None:
        self.router: Any = None
        self.handlers: dict[str, list] = {}

    async def route(self, _pattern: str, handler: Any) -> None:
        self.router = handler

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)


class _Popup:
    def __init__(self) -> None:
        self.url = _BASE + "thanks"
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Page:
    """A page with one form. Its submit sends ``routes`` through the context router,
    opens ``popup`` and raises ``dialog``, then shows ``after``."""

    def __init__(
        self,
        routes: list[_Route],
        *,
        info: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        popup: _Popup | None = None,
        dialog: str = "",
    ) -> None:
        self.context = _Context()
        self.routes = routes
        self.info = info or _info(count=1)
        self.after = after or {"text": "", "state": None, "changed": False}
        self.popup = popup
        self.dialog = dialog
        self.submitted = False
        self.page_handlers: dict[str, list] = {}

    def on(self, event: str, handler: Any) -> None:
        self.page_handlers.setdefault(event, []).append(handler)

    async def goto(self, *_a: Any, **_k: Any) -> None:
        await self.context.router(_Route(_BASE + "style.css", "GET", "stylesheet"))

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def wait_for_load_state(self, *_a: Any, **_k: Any) -> None:
        return None

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js is play_test._FILL_FORM_JS:
            return self.info if arg[0] == 0 else None
        if js is play_test._SUBMIT_FORM_JS:
            self.submitted = True
            for route in self.routes:
                await self.context.router(route)
            if self.popup is not None:
                for handler in self.context.handlers.get("page", []):
                    handler(self.popup)
            if self.dialog:
                accepted: list[str] = []

                class _Dialog:
                    message = self.dialog

                    async def accept(self_inner) -> None:
                        accepted.append("yes")

                for handler in self.page_handlers.get("dialog", []):
                    handler(_Dialog())
            return True
        return self.after


async def _run(page: _Page) -> play_test.PlayResult:
    result = play_test.PlayResult(url=_BASE)
    await play_test.exercise_page_forms(page, _BASE, result, time.monotonic() + 60)
    return result


class TestExercise:
    async def test_test_entries_never_leave_the_draft(self) -> None:
        routes = [
            _Route("https://api.pocketpaw.xyz/sites/s1/x/newsletter/subscribe", "POST"),
            _Route("https://formspree.io/f/abc", "POST"),
            # A GET is no safer: Google Forms and Apps Script write on GET.
            _Route("https://docs.google.com/forms/d/x/formResponse?entry.1=a", "GET", "fetch"),
            _Route("https://script.google.com/macros/s/x/exec", "GET", "xhr"),
            _Route("https://fonts.gstatic.com/s/inter.woff2", "GET", "font"),
            _Route(_BASE + "api/join", "POST"),
        ]
        page = _Page(routes, after={"text": "Thanks, you're on the list", "changed": True})
        result = await _run(page)
        assert [r.done for r in routes] == [
            "fulfill",
            "abort",
            "abort",
            "abort",
            "continue",
            "fulfill",
        ]
        assert routes[-1].fetch_kw == {"max_redirects": 0}
        (form,) = result.forms
        assert form.status == "ok"

    async def test_an_external_form_is_never_submitted(self) -> None:
        page = _Page([], info=_info(count=1, external="docs.google.com"))
        result = await _run(page)
        assert page.submitted is False
        assert result.forms[0].status == "unchecked"

    async def test_a_popup_is_closed_and_unchecked(self) -> None:
        popup = _Popup()
        page = _Page([_Route(_BASE + "api/join", "POST")], popup=popup)
        result = await _run(page)
        assert popup.closed is True
        assert result.forms[0].status == "unchecked"
        assert "new window" in result.forms[0].outcome

    async def test_a_redirect_to_another_host_is_not_followed(self) -> None:
        first = _Route(_BASE + "api/join", "POST", answer=307)
        first.location = "https://hooks.zapier.com/x"
        hop = _Route("https://hooks.zapier.com/x", "POST", "document")
        result = await _run(_Page([first, hop]))
        assert first.fetch_kw == {"max_redirects": 0} and first.done == "fulfill"
        assert hop.done == "abort"
        # Answered with a stub, never followed: the browser gets no Location to obey.
        assert first.body == play_test._REDIRECT_STUB
        assert result.forms[0].status == "unchecked"
        assert "redirects off-site to hooks.zapier.com" in result.forms[0].outcome

    async def test_an_alert_counts_as_the_confirmation(self) -> None:
        page = _Page([_Route(_BASE + "api/join", "POST")], dialog="Danke!")
        result = await _run(page)
        assert result.forms[0].status == "ok"

    async def test_a_422_is_unchecked(self) -> None:
        result = await _run(_Page([_Route(_BASE + "api/join", "POST", answer=422)]))
        assert result.forms[0].status == "unchecked"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/sites/s1/x/stripe/create_checkout", "url"),
            ("/sites/s1/x/newsletter/subscribe/", "ok"),
        ],
    )
    async def test_the_gateway_stub_answers_in_the_shape_paw_js_expects(
        self, path: str, expected: str
    ) -> None:
        import json

        route = _Route("https://api.pocketpaw.xyz" + path, "POST")
        await _run(_Page([route], after={"changed": True}))
        assert route.done == "fulfill"
        body = json.loads(route.body)
        assert body["ok"] is True
        if expected == "url":
            assert body["result"]["url"].startswith("https://")
        else:
            assert isinstance(body["result"], dict)

    async def test_a_page_without_forms_says_so(self) -> None:
        class _Empty(_Page):
            async def evaluate(self, js: str, arg: Any = None) -> Any:
                return None

        result = await _run(_Empty([]))
        assert result.forms == []
        assert result.forms_note == "this page has no forms"


class TestTestData:
    def test_phones_follow_the_site_region_and_stay_fictional_or_unassigned(self) -> None:
        phones = play_test.TEST_DATA["phones"]
        assert set(phones) == {"in", "gb", "us"}
        assert phones["in"][0].startswith("+91")
        assert phones["gb"][0].startswith("07700900")
        assert "555" in phones["us"][0]


class TestReReview:
    def test_a_multi_step_form_that_moves_on_is_unchecked(self) -> None:
        out = play_test.judge_form(_info(), {"text": "Step 2 of 3", "changed": True}, [], _BASE)
        assert out.status == "unchecked" and "another step" in out.outcome

    def test_a_mailto_form_is_unchecked(self) -> None:
        out = play_test.judge_form(_info(external="an email app (mailto:)"), None, [], _BASE)
        assert out.status == "unchecked" and "mailto" in out.outcome

    def test_a_draft_that_does_not_answer_is_unchecked(self) -> None:
        sent = [{"kind": "site", "method": "POST", "url": _BASE + "x", "error": "reset"}]
        out = play_test.judge_form(_info(), None, sent, _BASE)
        assert out.status == "unchecked" and "did not answer" in out.outcome

    async def test_off_site_images_and_scripts_are_aborted(self) -> None:
        routes = [
            _Route("https://tracker.example.net/p.gif?e=x", "GET", "image"),
            _Route("https://list.example.net/sub.js?e=x", "GET", "script"),
            _Route("https://fonts.gstatic.com/s/a.woff2", "GET", "font"),
            _Route("https://fonts.googleapis.com/css2?family=Inter", "GET", "stylesheet"),
        ]
        await _run(_Page(routes))
        assert [r.done for r in routes] == ["abort", "abort", "continue", "continue"]

    async def test_a_fetch_error_aborts_without_failing_the_form(self) -> None:
        class _Down(_Route):
            async def fetch(self, **_kw: Any) -> _Response:
                raise ConnectionError("reset")

        route = _Down(_BASE + "api/join", "POST")
        result = await _run(_Page([route]))
        assert route.done == "abort"
        assert result.forms[0].status == "unchecked"

    async def test_an_off_site_websocket_is_closed_and_a_same_origin_one_connects(self) -> None:
        class _Socket:
            def __init__(self, url: str) -> None:
                self.url = url
                self.done = ""

            async def close(self) -> None:
                self.done = "close"

            async def connect(self) -> None:
                self.done = "connect"

        sockets = [
            _Socket("wss://chat.example.net/ws?e=x"),
            _Socket("wss://abc.preview.example.com/ws"),
        ]

        class _WsContext(_Context):
            async def route_web_socket(self, _match: Any, handler: Any) -> None:
                self.ws_handler = handler

        class _WsPage(_Page):
            async def evaluate(self, js: str, arg: Any = None) -> Any:
                if js is play_test._SUBMIT_FORM_JS:
                    for sock in sockets:
                        await self.context.ws_handler(sock)
                return await super().evaluate(js, arg)

        page = _WsPage([])
        page.context = _WsContext()
        await _run(page)
        assert [s.done for s in sockets] == ["close", "connect"]
