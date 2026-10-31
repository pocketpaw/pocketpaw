# tests/ee/sites/test_play_test_forms.py — play_test_site submits every form on the draft
# with fake data before the agent calls the site ready (pocketpaw_ee.sites.play_test
# ``judge_form`` / ``exercise_page_forms``, the ``exercise_forms`` tool arg).
#
# The yoga mailing list shipped as a GET form: the email landed in the address bar and
# nothing was saved. ``judge_form`` must call that a failure, and the same for an error
# answer, a send with no confirmation, and a submit that sent nothing; it must pass a
# send that confirms, and refuse to guess on a validation refusal or a post to another
# service. The loop's routing keeps test entries out of the owner's real data (gateway
# stubbed, other hosts aborted). The tool side (recording failures, never exercising a
# live or server-less draft) is in tests/ee/agent/test_sites_mcp_server/test_play_test_site.py.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.sites import play_test

_BASE = "https://abc.preview.example.com/"


def _info(**kw: Any) -> dict[str, Any]:
    return {"name": "join", "fields": ["email"], "invalid": [], "external": "", "text": "", **kw}


class TestJudge:
    def test_get_to_the_same_page_is_a_failure(self) -> None:
        sent = [{"kind": "navigation", "method": "GET", "url": _BASE + "?email=x", "status": 200}]
        out = play_test.judge_form(_info(), {"text": "", "state": None}, sent, _BASE)
        assert out.status == "failed"
        assert "address bar" in out.outcome

    def test_an_error_answer_is_a_failure(self) -> None:
        sent = [{"kind": "site", "method": "POST", "url": _BASE + "api/join", "status": 500}]
        out = play_test.judge_form(_info(), {"text": "", "state": None}, sent, _BASE)
        assert (out.status, out.outcome) == ("failed", "the site answered with an error (HTTP 500)")

    def test_a_send_without_a_confirmation_is_a_failure(self) -> None:
        sent = [{"kind": "site", "method": "POST", "url": _BASE + "api/join", "status": 200}]
        out = play_test.judge_form(_info(), {"text": "Join us", "state": None}, sent, _BASE)
        assert out.status == "failed"
        assert "no thank-you" in out.outcome

    def test_nothing_sent_is_a_failure(self) -> None:
        out = play_test.judge_form(_info(), {"text": "", "state": None}, [], _BASE)
        assert (out.status, out.detail) == ("failed", "no request was sent")

    @pytest.mark.parametrize(
        ("state", "sent"),
        [
            (
                {"text": "Thanks! You're on the list.", "state": None},
                [{"kind": "site", "method": "POST", "url": _BASE + "api/j", "status": 201}],
            ),
            (
                {"text": "", "state": "success"},
                [{"kind": "gateway", "method": "POST", "url": "https://api/x", "status": 200}],
            ),
            (
                {"text": "", "state": None},
                [
                    {"kind": "site", "method": "POST", "url": _BASE, "status": 303},
                    {"kind": "navigation", "method": "GET", "url": _BASE + "thanks", "status": 200},
                ],
            ),
        ],
    )
    def test_a_confirmed_send_passes(self, state: dict, sent: list) -> None:
        out = play_test.judge_form(_info(text="Join"), state, sent, _BASE)
        assert (out.status, out.outcome) == ("ok", "sent and confirmed")

    def test_a_success_word_already_on_the_page_does_not_count(self) -> None:
        sent = [{"kind": "site", "method": "POST", "url": _BASE + "api/j", "status": 200}]
        page = "Thank you for visiting"
        out = play_test.judge_form(_info(text=page), {"text": page, "state": None}, sent, _BASE)
        assert out.status == "failed"

    def test_validation_and_other_services_are_unchecked(self) -> None:
        invalid = play_test.judge_form(_info(invalid=["phone: wrong format"]), None, [], _BASE)
        assert invalid.status == "unchecked"
        ext = [{"kind": "external", "method": "POST", "url": "https://formspree.io/f/x"}]
        out = play_test.judge_form(_info(), None, ext, _BASE)
        assert out.status == "unchecked"
        assert "formspree.io" in out.outcome


class _Route:
    def __init__(self, url: str, method: str) -> None:
        self.request = type("R", (), {"url": url, "method": method})()
        self.done = ""

    async def continue_(self) -> None:
        self.done = "continue"

    async def fulfill(self, **_kw: Any) -> None:
        self.done = "fulfill"

    async def abort(self) -> None:
        self.done = "abort"


class _Page:
    """A page with one form whose submit posts ``posts`` and then shows ``after``."""

    def __init__(self, posts: list[tuple[str, str]], after: str = "") -> None:
        self.posts = posts
        self.after = after
        self.router: Any = None
        self.routes: list[_Route] = []
        self.listeners: list[Any] = []

    async def route(self, _pattern: str, handler: Any) -> None:
        self.router = handler

    def on(self, _event: str, handler: Any) -> None:
        self.listeners.append(handler)

    async def goto(self, *_a: Any, **_k: Any) -> None:
        await self.router(_Route(_BASE + "style.css", "GET"))

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def wait_for_load_state(self, *_a: Any, **_k: Any) -> None:
        return None

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js is play_test._FILL_FORM_JS:
            return _info(count=1) if arg[0] == 0 else None
        if js is play_test._SUBMIT_FORM_JS:
            for method, url in self.posts:
                route = _Route(url, method)
                self.routes.append(route)
                await self.router(route)
            return True
        return {"text": self.after, "state": None}


class TestExercise:
    async def _run(self, page: _Page) -> play_test.PlayResult:
        result = play_test.PlayResult(url=_BASE)
        import time

        await play_test.exercise_page_forms(page, _BASE, result, time.monotonic() + 60)
        return result

    async def test_test_entries_never_leave_the_draft(self) -> None:
        page = _Page(
            [
                ("POST", "https://api.pocketpaw.xyz/sites/s1/x/newsletter/subscribe"),
                ("POST", "https://formspree.io/f/abc"),
                ("POST", _BASE + "api/join"),
            ],
            after="Thanks, you're on the list",
        )
        result = await self._run(page)
        assert [r.done for r in page.routes] == ["fulfill", "abort", "continue"]
        (form,) = result.forms
        assert form.status == "ok"

    async def test_a_page_without_forms_says_so(self) -> None:
        class _Empty(_Page):
            async def evaluate(self, js: str, arg: Any = None) -> Any:
                return None

        result = await self._run(_Empty([]))
        assert result.forms == []
        assert result.forms_note == "this page has no forms"
